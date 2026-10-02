"""Service-free regression tests for integration-fixture failure cleanup."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest

CONFTST_PATH = Path(__file__).parent / "integration" / "conftest.py"
LEASE_KEY = "atlasrag:test:exclusive-lease"


def load_integration_fixtures() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "atlasrag_integration_fixture_safety",
        CONFTST_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeLeaseRedis:
    def __init__(self, values: dict[str, bytes] | None = None) -> None:
        self.values = dict(values or {})
        self.scan_results: list[tuple[int, list[bytes | str]]] = []
        self.scan_calls: list[int] = []
        self.eval_calls: list[tuple[str, int, tuple[str, ...]]] = []
        self.atomic_unlink_batches: list[tuple[str, ...]] = []
        self.direct_unlink_called = False
        self.closed = False

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int,
        nx: bool,
    ) -> bool | None:
        assert ex > 0 and nx
        if key in self.values:
            return None
        self.values[key] = value.encode("utf-8")
        return True

    async def dbsize(self) -> int:
        return len(self.values)

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def eval(
        self,
        script: str,
        key_count: int,
        *items: str,
    ) -> int:
        self.eval_calls.append((script, key_count, items))
        keys = items[:key_count]
        arguments = items[key_count:]
        assert keys and len(arguments) == 1
        token = arguments[0].encode("utf-8")
        if self.values.get(keys[0]) != token:
            return -1 if "UNLINK" in script else 0
        if "UNLINK" not in script:
            del self.values[keys[0]]
            return 1

        batch = keys[1:]
        self.atomic_unlink_batches.append(batch)
        deleted = 0
        for key in batch:
            if key in self.values:
                deleted += 1
                del self.values[key]
        return deleted

    async def scan(
        self,
        *,
        cursor: int,
        count: int,
    ) -> tuple[int, list[bytes | str]]:
        assert count > 0
        self.scan_calls.append(cursor)
        if self.scan_results:
            return self.scan_results.pop(0)
        return 0, [key.encode("utf-8") for key in self.values]

    async def unlink(self, *_keys: str) -> int:
        self.direct_unlink_called = True
        raise AssertionError("cleanup must use token-scoped atomic UNLINK")

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_database_is_dropped_when_create_admin_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration_fixtures = load_integration_fixtures()
    dropped: list[tuple[str, str]] = []

    async def create(_dsn: str, _name: str) -> None:
        raise RuntimeError("administrator close failed")

    async def drop(dsn: str, name: str) -> None:
        dropped.append((dsn, name))

    monkeypatch.setattr(integration_fixtures, "_create_database", create)
    monkeypatch.setattr(integration_fixtures, "_drop_database", drop)

    with pytest.raises(RuntimeError, match="administrator close failed"):
        async with integration_fixtures._isolated_postgres_database(
            "postgresql://127.0.0.1/atlasrag",
            "atlasrag_test_safe",
        ):
            pytest.fail("fixture must not yield after setup failure")

    assert dropped == [("postgresql://127.0.0.1/atlasrag", "atlasrag_test_safe")]


@pytest.mark.asyncio
async def test_database_is_dropped_after_body_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration_fixtures = load_integration_fixtures()
    calls: list[str] = []

    async def create(_dsn: str, _name: str) -> None:
        calls.append("create")

    async def drop(_dsn: str, _name: str) -> None:
        calls.append("drop")

    monkeypatch.setattr(integration_fixtures, "_create_database", create)
    monkeypatch.setattr(integration_fixtures, "_drop_database", drop)

    with pytest.raises(ValueError, match="body failure"):
        async with integration_fixtures._isolated_postgres_database(
            "postgresql://127.0.0.1/atlasrag",
            "atlasrag_test_body",
        ):
            raise ValueError("body failure")

    assert calls == ["create", "drop"]


@pytest.mark.asyncio
async def test_primary_cancellation_survives_database_drop_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration_fixtures = load_integration_fixtures()
    entered = asyncio.Event()
    drop_called = asyncio.Event()

    async def create(_dsn: str, _name: str) -> None:
        pass

    async def drop(_dsn: str, _name: str) -> None:
        drop_called.set()
        raise OSError("drop failure")

    monkeypatch.setattr(integration_fixtures, "_create_database", create)
    monkeypatch.setattr(integration_fixtures, "_drop_database", drop)

    async def exercise() -> None:
        async with integration_fixtures._isolated_postgres_database(
            "postgresql://127.0.0.1/atlasrag",
            "atlasrag_test_cancel",
        ):
            entered.set()
            await asyncio.Event().wait()

    operation = asyncio.create_task(exercise())
    await entered.wait()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert drop_called.is_set()


@pytest.mark.asyncio
async def test_pool_open_failure_still_closes_pool() -> None:
    integration_fixtures = load_integration_fixtures()
    calls: list[str] = []

    class FailingPool:
        async def open(self) -> None:
            calls.append("open")
            raise OSError("open failed")

        async def close(self) -> None:
            calls.append("close")

    with pytest.raises(OSError, match="open failed"):
        async with integration_fixtures._managed_postgres_pool(FailingPool()):
            pytest.fail("fixture must not yield after setup failure")

    assert calls == ["open", "close"]


@pytest.mark.asyncio
async def test_migration_cancellation_waits_for_owned_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration_fixtures = load_integration_fixtures()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def invoke(_dsn: str) -> None:
        started.set()
        release.wait(timeout=2.0)
        finished.set()

    async def forbidden_subprocess(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("migration must be owned by the settled worker")

    monkeypatch.setattr(
        integration_fixtures,
        "_invoke_migrations",
        invoke,
        raising=False,
    )
    monkeypatch.setattr(
        integration_fixtures.asyncio,
        "create_subprocess_exec",
        forbidden_subprocess,
    )

    operation = asyncio.create_task(
        integration_fixtures._run_migrations("postgresql://127.0.0.1/atlasrag")
    )
    assert await asyncio.to_thread(started.wait, 1.0)
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_point",
    ["ping", "lease", "initial_cleanup", "teardown"],
)
async def test_redis_failure_paths_always_close_client(
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    integration_fixtures = load_integration_fixtures()
    calls: list[str] = []

    class FakeRedis:
        async def ping(self) -> bool:
            calls.append("ping")
            if failure_point == "ping":
                raise OSError("ping failed")
            return True

        async def aclose(self) -> None:
            calls.append("close")

    clear_calls = 0

    async def assert_lease(_client: object, _token: str) -> None:
        calls.append("lease")
        if failure_point == "lease":
            raise OSError("lease failed")

    async def clear(_client: object, _token: str) -> None:
        nonlocal clear_calls
        clear_calls += 1
        calls.append(f"clear-{clear_calls}")
        if failure_point == "initial_cleanup" and clear_calls == 1:
            raise OSError("initial cleanup failed")
        if failure_point == "teardown" and clear_calls == 2:
            raise OSError("teardown failed")

    monkeypatch.setattr(integration_fixtures, "_assert_redis_lease", assert_lease)
    monkeypatch.setattr(integration_fixtures, "_clear_test_redis", clear)
    client = FakeRedis()

    if failure_point in {"ping", "lease", "initial_cleanup"}:
        with pytest.raises(OSError):
            async with integration_fixtures._managed_redis_client(
                client,
                "lease-token",
            ):
                pytest.fail("fixture must not yield after setup failure")
    else:
        with pytest.raises(OSError, match="teardown failed"):
            async with integration_fixtures._managed_redis_client(
                client,
                "lease-token",
            ):
                calls.append("body")

    assert calls[-1] == "close"


@pytest.mark.asyncio
async def test_redis_set_nx_conflict_preserves_other_lease() -> None:
    integration_fixtures = load_integration_fixtures()
    client = FakeLeaseRedis({LEASE_KEY: b"other-token"})

    with pytest.raises(RuntimeError, match="already leased"):
        await integration_fixtures._assert_redis_lease(client, "owned-token")

    assert client.values == {LEASE_KEY: b"other-token"}


@pytest.mark.asyncio
async def test_nonempty_redis_database_fails_closed_without_deleting_data() -> None:
    integration_fixtures = load_integration_fixtures()
    client = FakeLeaseRedis({"unrelated:key": b"keep"})

    with pytest.raises(RuntimeError, match="not empty"):
        await integration_fixtures._assert_redis_lease(client, "owned-token")

    assert client.values == {"unrelated:key": b"keep"}
    assert not client.direct_unlink_called


@pytest.mark.asyncio
async def test_lost_redis_lease_prevents_scan_and_cleanup() -> None:
    integration_fixtures = load_integration_fixtures()
    client = FakeLeaseRedis(
        {
            LEASE_KEY: b"other-token",
            "atlasrag:query:session:runtime:request": b"keep",
        }
    )

    with pytest.raises(RuntimeError, match="ownership was lost"):
        await integration_fixtures._clear_test_redis(client, "owned-token")

    assert client.scan_calls == []
    assert client.values["atlasrag:query:session:runtime:request"] == b"keep"


@pytest.mark.asyncio
async def test_redis_cleanup_batches_atomic_unlink_and_excludes_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration_fixtures = load_integration_fixtures()
    monkeypatch.setattr(integration_fixtures, "REDIS_SCAN_COUNT", 2)
    client = FakeLeaseRedis(
        {
            LEASE_KEY: b"owned-token",
            "key-one": b"1",
            "key-two": b"2",
            "key-three": b"3",
            "key-four": b"4",
        }
    )
    client.scan_results = [
        (7, [LEASE_KEY, "key-one", "key-two", "key-three"]),
        (0, ["key-four"]),
    ]

    await integration_fixtures._clear_test_redis(client, "owned-token")

    assert client.atomic_unlink_batches == [
        ("key-one", "key-two"),
        ("key-three",),
        ("key-four",),
    ]
    assert client.values == {LEASE_KEY: b"owned-token"}
    assert not client.direct_unlink_called


@pytest.mark.asyncio
async def test_lease_loss_after_scan_does_not_delete_scanned_key() -> None:
    integration_fixtures = load_integration_fixtures()

    class LeaseChangesAfterScan(FakeLeaseRedis):
        async def scan(
            self,
            *,
            cursor: int,
            count: int,
        ) -> tuple[int, list[bytes | str]]:
            page = await super().scan(cursor=cursor, count=count)
            self.values[LEASE_KEY] = b"new-owner-token"
            return page

    client = LeaseChangesAfterScan(
        {
            LEASE_KEY: b"owned-token",
            "scanned-key": b"keep",
        }
    )

    with pytest.raises(RuntimeError, match="ownership was lost"):
        await integration_fixtures._clear_test_redis(client, "owned-token")

    assert client.values["scanned-key"] == b"keep"
    assert client.values[LEASE_KEY] == b"new-owner-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["repeated_cursor", "page_bound"])
async def test_redis_cleanup_rejects_unbounded_scan_progress(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    integration_fixtures = load_integration_fixtures()
    client = FakeLeaseRedis(
        {
            LEASE_KEY: b"owned-token",
            "late-page-key": b"keep",
        }
    )
    if failure == "repeated_cursor":
        client.scan_results = [(7, []), (7, ["late-page-key"])]
        expected = "cursor repeated"
    else:
        monkeypatch.setattr(integration_fixtures, "REDIS_MAX_SCAN_PAGES", 1)
        client.scan_results = [(7, []), (0, ["late-page-key"])]
        expected = "page bound"

    with pytest.raises(RuntimeError, match=expected):
        await integration_fixtures._clear_test_redis(client, "owned-token")
    assert client.values["late-page-key"] == b"keep"


@pytest.mark.asyncio
async def test_body_cancellation_and_release_failure_still_close_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration_fixtures = load_integration_fixtures()
    client = FakeLeaseRedis()
    cancellation = asyncio.CancelledError()

    async def clear(_client: object, _token: str) -> None:
        pass

    async def release(_client: object, _token: str) -> None:
        raise OSError("release failure")

    monkeypatch.setattr(integration_fixtures, "_clear_test_redis", clear)
    monkeypatch.setattr(integration_fixtures, "_release_redis_lease", release)

    with pytest.raises(asyncio.CancelledError) as raised:
        async with integration_fixtures._managed_redis_client(
            client,
            "owned-token",
        ):
            raise cancellation

    assert raised.value is cancellation
    assert client.closed


@pytest.mark.asyncio
async def test_cleanup_cancellation_supersedes_ordinary_primary_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration_fixtures = load_integration_fixtures()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    class FakeRedis:
        async def ping(self) -> bool:
            raise ValueError("primary failure")

        async def aclose(self) -> None:
            cleanup_started.set()
            await release_cleanup.wait()

    client = FakeRedis()
    operation = asyncio.create_task(
        integration_fixtures._exercise_managed_redis_client_for_test(
            client,
            "lease-token",
        )
    )
    await cleanup_started.wait()
    operation.cancel()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await operation


def test_redis_urls_require_explicit_distinct_test_database() -> None:
    integration_fixtures = load_integration_fixtures()
    assert (
        integration_fixtures._validate_redis_test_url(
            "redis://127.0.0.1:6379/0",
            "redis://127.0.0.1:6379/15",
        )
        == "redis://127.0.0.1:6379/15"
    )
    for invalid in (
        "redis://127.0.0.1:6379/0",
        "redis://127.0.0.1:6379/14",
        "redis://127.0.0.1:6380/15",
    ):
        with pytest.raises(ValueError):
            integration_fixtures._validate_redis_test_url(
                "redis://127.0.0.1:6379/0",
                invalid,
            )


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://atlasrag:password@example.com:5432/atlasrag",
        "postgresql://atlasrag:password@10.0.0.8:5432/atlasrag",
        "postgresql://atlasrag:password@localhost.example:5432/atlasrag",
    ],
)
def test_destructive_postgres_fixture_rejects_non_loopback_dsn(dsn: str) -> None:
    integration_fixtures = load_integration_fixtures()

    with pytest.raises(ValueError, match="loopback"):
        integration_fixtures._database_dsn(dsn, "atlasrag_test_safe")


@pytest.mark.parametrize(
    ("application_url", "test_url"),
    [
        ("redis://example.com:6379/0", "redis://example.com:6379/15"),
        ("redis://10.0.0.8:6379/0", "redis://10.0.0.8:6379/15"),
        (
            "redis://localhost.example:6379/0",
            "redis://localhost.example:6379/15",
        ),
    ],
)
def test_destructive_redis_fixture_rejects_non_loopback_urls(
    application_url: str,
    test_url: str,
) -> None:
    integration_fixtures = load_integration_fixtures()

    with pytest.raises(ValueError, match="loopback"):
        integration_fixtures._validate_redis_test_url(application_url, test_url)


@pytest.mark.parametrize(
    "uri",
    [
        "http://example.com:19530",
        "http://10.0.0.8:19530",
        "http://localhost.example:19530",
    ],
)
def test_destructive_milvus_fixture_rejects_non_loopback_uri(uri: str) -> None:
    integration_fixtures = load_integration_fixtures()

    with pytest.raises(ValueError, match="loopback"):
        integration_fixtures._validate_milvus_uri(uri)


@pytest.mark.asyncio
async def test_redis_lease_release_is_atomic_and_token_scoped() -> None:
    integration_fixtures = load_integration_fixtures()
    calls: list[tuple[str, int, str, str]] = []

    class FakeRedis:
        async def eval(
            self,
            script: str,
            key_count: int,
            key: str,
            token: str,
        ) -> int:
            calls.append((script, key_count, key, token))
            return 1

    await integration_fixtures._release_redis_lease(
        FakeRedis(),
        "owned-token",
    )

    assert len(calls) == 1
    script, key_count, key, token = calls[0]
    assert "GET" in script and "DEL" in script
    assert (key_count, key, token) == (
        1,
        integration_fixtures.REDIS_LEASE_KEY,
        "owned-token",
    )
