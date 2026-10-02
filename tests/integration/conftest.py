"""Isolated, explicit fixtures for real Stage 2 infrastructure."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Sequence
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import SplitResult, urlsplit
from uuid import uuid4

import pytest
import pytest_asyncio
from psycopg import AsyncConnection, sql
from redis.asyncio import Redis
from sqlalchemy.engine import make_url

from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.repositories.postgres.pool import PostgresPool

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_CONFIG = REPOSITORY_ROOT / "alembic.ini"
POSTGRES_DSN_ENV = "ATLASRAG_POSTGRES_DSN"
REDIS_URL_ENV = "ATLASRAG_REDIS_URL"
TEST_REDIS_URL_ENV = "ATLASRAG_TEST_REDIS_URL"
MILVUS_URI_ENV = "ATLASRAG_MILVUS_URI"
REDIS_LEASE_KEY = "atlasrag:test:exclusive-lease"
REDIS_LEASE_SECONDS = 600
REDIS_SCAN_COUNT = 100
REDIS_MAX_SCAN_PAGES = 10_000
MIGRATION_TIMEOUT_SECONDS = 60.0

_DATABASE_NAME = re.compile(r"\Aatlasrag_test_[a-z0-9_]+\Z")
_RELEASE_LEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
""".strip()
_UNLINK_WITH_LEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return -1
end
if #KEYS == 1 then
    return 0
end
return redis.call('UNLINK', unpack(KEYS, 2, #KEYS))
""".strip()


@pytest.fixture(autouse=True)
def _require_explicit_integration_opt_in() -> None:
    if os.environ.get("ATLASRAG_RUN_INTEGRATION") != "1":
        pytest.fail(
            "integration tests require ATLASRAG_RUN_INTEGRATION=1",
            pytrace=False,
        )


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required for integration tests")
    return value


def _require_loopback_host(host: str | None, service: str) -> str:
    if host is None:
        raise ValueError(f"integration {service} endpoint must use a loopback host")
    if host.casefold() == "localhost":
        return host
    try:
        address = ip_address(host)
    except ValueError:
        raise ValueError(
            f"integration {service} endpoint must use a loopback host"
        ) from None
    if not address.is_loopback:
        raise ValueError(f"integration {service} endpoint must use a loopback host")
    return host


def _add_cleanup_note(
    primary: BaseException,
    operation: str,
    cleanup_error: BaseException,
) -> None:
    primary.add_note(
        "Integration fixture cleanup failure during "
        f"{operation}: {type(cleanup_error).__name__}"
    )


async def _capture_cleanup(
    operation: Awaitable[object],
) -> BaseException | None:
    """Settle one cleanup operation while preserving cancellation priority."""

    task = asyncio.ensure_future(operation)
    interrupted: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if interrupted is None:
                interrupted = error
        except BaseException:
            break

    try:
        task.result()
    except asyncio.CancelledError as error:
        if interrupted is None:
            interrupted = error
    except BaseException as error:
        if interrupted is not None:
            _add_cleanup_note(interrupted, "settled operation", error)
            return interrupted
        return error
    return interrupted


def _raise_resource_outcome(
    primary: BaseException | None,
    cleanup_failures: Sequence[tuple[str, BaseException]],
) -> None:
    """Raise by priority: primary cancellation, cleanup cancellation, errors."""

    if isinstance(primary, asyncio.CancelledError):
        for operation, cleanup_error in cleanup_failures:
            _add_cleanup_note(primary, operation, cleanup_error)
        raise primary

    cleanup_cancellation = next(
        (
            error
            for _, error in cleanup_failures
            if isinstance(error, asyncio.CancelledError)
        ),
        None,
    )
    if cleanup_cancellation is not None:
        if primary is not None:
            cleanup_cancellation.add_note(
                "Integration fixture primary failure superseded by cleanup "
                f"cancellation: {type(primary).__name__}"
            )
        for operation, cleanup_error in cleanup_failures:
            if cleanup_error is not cleanup_cancellation:
                _add_cleanup_note(
                    cleanup_cancellation,
                    operation,
                    cleanup_error,
                )
        raise cleanup_cancellation

    if primary is not None:
        for operation, cleanup_error in cleanup_failures:
            _add_cleanup_note(primary, operation, cleanup_error)
        raise primary

    if cleanup_failures:
        operation, cleanup_error = cleanup_failures[0]
        cleanup_error.add_note(
            f"Integration fixture cleanup failure during {operation}"
        )
        for later_operation, later_error in cleanup_failures[1:]:
            _add_cleanup_note(cleanup_error, later_operation, later_error)
        raise cleanup_error


def _database_dsn(dsn: str, database_name: str) -> str:
    if _DATABASE_NAME.fullmatch(database_name) is None and database_name != "postgres":
        raise ValueError("unsafe integration database name")
    url = make_url(dsn)
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("integration PostgreSQL DSN must use PostgreSQL")
    _require_loopback_host(url.host, "PostgreSQL")
    return url.set(database=database_name).render_as_string(hide_password=False)


async def _execute_admin_statement(
    dsn: str,
    statement: sql.SQL | sql.Composed,
) -> None:
    connection: AsyncConnection[tuple[object, ...]] | None = None
    primary: BaseException | None = None
    try:
        connection = await AsyncConnection.connect(
            _database_dsn(dsn, "postgres"),
            autocommit=True,
        )
        await connection.execute(statement)
    except BaseException as error:
        primary = error

    cleanup_failures: list[tuple[str, BaseException]] = []
    if connection is not None:
        close_error = await _capture_cleanup(connection.close())
        if close_error is not None:
            cleanup_failures.append(("administrator connection close", close_error))
    _raise_resource_outcome(primary, cleanup_failures)


async def _create_database(dsn: str, database_name: str) -> None:
    await _execute_admin_statement(
        dsn,
        sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)),
    )


async def _drop_database(dsn: str, database_name: str) -> None:
    await _execute_admin_statement(
        dsn,
        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(database_name)
        ),
    )


@asynccontextmanager
async def _isolated_postgres_database(
    base_dsn: str,
    database_name: str,
) -> AsyncIterator[str]:
    isolated_dsn = _database_dsn(base_dsn, database_name)
    primary: BaseException | None = None
    try:
        await _create_database(base_dsn, database_name)
        yield isolated_dsn
    except BaseException as error:
        primary = error

    cleanup_failures: list[tuple[str, BaseException]] = []
    drop_error = await _capture_cleanup(_drop_database(base_dsn, database_name))
    if drop_error is not None:
        cleanup_failures.append(("database drop", drop_error))
    _raise_resource_outcome(primary, cleanup_failures)


def _invoke_migrations(dsn: str) -> None:
    environment = dict(os.environ)
    environment[POSTGRES_DSN_ENV] = dsn
    try:
        subprocess.run(
            (
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(ALEMBIC_CONFIG),
                "upgrade",
                "head",
            ),
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=MIGRATION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Alembic migration timed out") from None
    except subprocess.CalledProcessError:
        raise RuntimeError("Alembic migration failed") from None


async def _run_migrations(dsn: str) -> None:
    operation = asyncio.create_task(asyncio.to_thread(_invoke_migrations, dsn))
    primary: asyncio.CancelledError | None = None
    try:
        await asyncio.shield(operation)
        return
    except asyncio.CancelledError as error:
        primary = error

    cleanup_failures: list[tuple[str, BaseException]] = []
    settle_error = await _capture_cleanup(operation)
    if settle_error is not None:
        cleanup_failures.append(("migration worker settlement", settle_error))
    _raise_resource_outcome(primary, cleanup_failures)


@asynccontextmanager
async def _managed_postgres_pool(
    pool: PostgresPool,
) -> AsyncIterator[PostgresPool]:
    primary: BaseException | None = None
    try:
        await pool.open()
        yield pool
    except BaseException as error:
        primary = error

    cleanup_failures: list[tuple[str, BaseException]] = []
    close_error = await _capture_cleanup(pool.close())
    if close_error is not None:
        cleanup_failures.append(("PostgreSQL pool close", close_error))
    _raise_resource_outcome(primary, cleanup_failures)


def _redis_endpoint(url: SplitResult) -> tuple[str, str, int]:
    if url.scheme not in {"redis", "rediss"} or url.hostname is None:
        raise ValueError("integration Redis URL must be a TCP Redis URL")
    host = _require_loopback_host(url.hostname, "Redis")
    try:
        port = url.port or 6379
    except ValueError:
        raise ValueError("integration Redis URL has an invalid port") from None
    return url.scheme, host.casefold(), port


def _validate_milvus_uri(uri: str) -> str:
    url = urlsplit(uri)
    if url.scheme not in {"http", "https"} or url.hostname is None:
        raise ValueError("integration Milvus URI must be an HTTP(S) URI")
    try:
        url.port
    except ValueError:
        raise ValueError("integration Milvus URI has an invalid port") from None
    _require_loopback_host(url.hostname, "Milvus")
    return uri


def _redis_database(url: SplitResult) -> int:
    if url.query or url.fragment or not re.fullmatch(r"/\d+", url.path):
        raise ValueError("integration Redis URL must contain one database path")
    database = int(url.path[1:])
    if not 0 <= database <= 15:
        raise ValueError("integration Redis database must be between 0 and 15")
    return database


def _validate_redis_test_url(application_url: str, test_url: str) -> str:
    application = urlsplit(application_url)
    test = urlsplit(test_url)
    if _redis_endpoint(application) != _redis_endpoint(test):
        raise ValueError(
            "integration Redis must share the configured application endpoint"
        )
    application_database = _redis_database(application)
    test_database = _redis_database(test)
    if test_database != 15 or test_database == application_database:
        raise ValueError(
            "ATLASRAG_TEST_REDIS_URL must select dedicated Redis database 15"
        )
    return test_url


def _redis_token_matches(value: object, token: str) -> bool:
    return value == token or value == token.encode("utf-8")


async def _release_redis_lease(client: Redis, token: str) -> None:
    released = await client.eval(
        _RELEASE_LEASE_SCRIPT,
        1,
        REDIS_LEASE_KEY,
        token,
    )
    if type(released) is not int or not 0 <= released <= 1:
        raise RuntimeError("Redis integration lease returned an invalid result")


async def _unlink_redis_keys(
    client: Redis,
    token: str,
    keys: Sequence[str],
) -> None:
    if not keys:
        return
    deleted = await client.eval(
        _UNLINK_WITH_LEASE_SCRIPT,
        len(keys) + 1,
        REDIS_LEASE_KEY,
        *keys,
        token,
    )
    if deleted == -1:
        raise RuntimeError("Redis integration lease ownership was lost")
    if type(deleted) is not int or not 0 <= deleted <= len(keys):
        raise RuntimeError("Redis integration UNLINK returned an invalid result")


async def _assert_redis_lease(client: Redis, token: str) -> None:
    primary: BaseException | None = None
    try:
        acquired = await client.set(
            REDIS_LEASE_KEY,
            token,
            ex=REDIS_LEASE_SECONDS,
            nx=True,
        )
        if acquired is not True:
            raise RuntimeError("Redis integration database is already leased")
        database_size = await client.dbsize()
        if type(database_size) is not int or database_size != 1:
            raise RuntimeError(
                "Redis integration database is not empty; refusing cleanup"
            )
        return
    except BaseException as error:
        primary = error

    cleanup_failures: list[tuple[str, BaseException]] = []
    release_error = await _capture_cleanup(_release_redis_lease(client, token))
    if release_error is not None:
        cleanup_failures.append(("Redis lease release", release_error))
    _raise_resource_outcome(primary, cleanup_failures)


def _redis_key(raw_key: object) -> str:
    if isinstance(raw_key, bytes):
        try:
            return raw_key.decode("utf-8")
        except UnicodeDecodeError:
            raise RuntimeError(
                "Redis integration database has a non-UTF-8 key"
            ) from None
    if isinstance(raw_key, str):
        return raw_key
    raise RuntimeError("Redis integration SCAN returned an invalid key")


async def _clear_test_redis(client: Redis, token: str) -> None:
    lease = await client.get(REDIS_LEASE_KEY)
    if not _redis_token_matches(lease, token):
        raise RuntimeError("Redis integration lease ownership was lost")

    cursor = 0
    seen_cursors: set[int] = set()
    pages = 0
    while True:
        raw_page = await client.scan(cursor=cursor, count=REDIS_SCAN_COUNT)
        if not isinstance(raw_page, (list, tuple)) or len(raw_page) != 2:
            raise RuntimeError("Redis integration SCAN returned an invalid page")
        raw_cursor, raw_keys = raw_page
        if type(raw_cursor) is not int or raw_cursor < 0:
            raise RuntimeError("Redis integration SCAN returned an invalid cursor")
        if not isinstance(raw_keys, (list, tuple)):
            raise RuntimeError("Redis integration SCAN returned invalid keys")
        pages += 1
        if pages > REDIS_MAX_SCAN_PAGES:
            raise RuntimeError("Redis integration cleanup exceeded its page bound")
        if raw_cursor != 0 and raw_cursor in seen_cursors:
            raise RuntimeError("Redis integration SCAN cursor repeated")

        keys = [
            key
            for key in (_redis_key(raw_key) for raw_key in raw_keys)
            if key != REDIS_LEASE_KEY
        ]
        for offset in range(0, len(keys), REDIS_SCAN_COUNT):
            await _unlink_redis_keys(
                client,
                token,
                keys[offset : offset + REDIS_SCAN_COUNT],
            )

        if raw_cursor == 0:
            break
        seen_cursors.add(raw_cursor)
        cursor = raw_cursor

    lease = await client.get(REDIS_LEASE_KEY)
    if not _redis_token_matches(lease, token):
        raise RuntimeError("Redis integration lease ownership was lost")


@asynccontextmanager
async def _managed_redis_client(
    client: Redis,
    lease_token: str,
) -> AsyncIterator[Redis]:
    primary: BaseException | None = None
    safe_to_clear = False
    try:
        healthy = await client.ping()
        if healthy is not True:
            raise RuntimeError("Redis integration health response was invalid")
        await _assert_redis_lease(client, lease_token)
        safe_to_clear = True
        await _clear_test_redis(client, lease_token)
        yield client
    except BaseException as error:
        primary = error

    cleanup_failures: list[tuple[str, BaseException]] = []
    if safe_to_clear:
        clear_error = await _capture_cleanup(_clear_test_redis(client, lease_token))
        if clear_error is not None:
            cleanup_failures.append(("Redis key cleanup", clear_error))
        release_error = await _capture_cleanup(
            _release_redis_lease(client, lease_token)
        )
        if release_error is not None:
            cleanup_failures.append(("Redis lease release", release_error))
    close_error = await _capture_cleanup(client.aclose())
    if close_error is not None:
        cleanup_failures.append(("Redis client close", close_error))
    _raise_resource_outcome(primary, cleanup_failures)


async def _exercise_managed_redis_client_for_test(
    client: Redis,
    lease_token: str,
) -> None:
    async with _managed_redis_client(client, lease_token):
        pass


@pytest_asyncio.fixture
async def postgres_dsn() -> AsyncIterator[str]:
    base_dsn = _required_environment(POSTGRES_DSN_ENV)
    database_name = f"atlasrag_test_{uuid4().hex}"
    async with _isolated_postgres_database(
        base_dsn,
        database_name,
    ) as isolated_dsn:
        await _run_migrations(isolated_dsn)
        yield isolated_dsn


@pytest_asyncio.fixture
async def postgres_pool(postgres_dsn: str) -> AsyncIterator[PostgresPool]:
    pool = PostgresPool(
        postgres_dsn,
        min_size=1,
        max_size=4,
        timeout=2.0,
    )
    async with _managed_postgres_pool(pool) as managed_pool:
        yield managed_pool


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Redis]:
    application_url = _required_environment(REDIS_URL_ENV)
    test_url = _validate_redis_test_url(
        application_url,
        _required_environment(TEST_REDIS_URL_ENV),
    )
    client = Redis.from_url(
        test_url,
        decode_responses=False,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
    )
    async with _managed_redis_client(client, uuid4().hex) as managed_client:
        yield managed_client


@pytest.fixture
def milvus_uri() -> str:
    return _validate_milvus_uri(_required_environment(MILVUS_URI_ENV))


@pytest.fixture
def redis_cache(redis_client: Redis) -> RedisCacheProvider:
    return RedisCacheProvider(redis_client, scan_batch_size=2)
