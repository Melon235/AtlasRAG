"""Dedicated PostgreSQL session advisory-lock leases."""

from __future__ import annotations

from asyncio import CancelledError
from collections.abc import Awaitable, Mapping
from hashlib import sha256
from typing import NoReturn, Protocol

from psycopg import AsyncConnection
from psycopg.rows import DictRow

from atlasrag.domain.errors import CanonicalDataError, InvariantViolationError
from atlasrag.repositories.postgres.errors import (
    postgres_error_boundary,
    require_canonical_id,
)


class _ConnectionPool(Protocol):
    async def getconn(self) -> AsyncConnection[DictRow]: ...

    async def putconn(self, connection: AsyncConnection[DictRow]) -> None: ...


def advisory_lock_key(document_id: str) -> int:
    """Map one canonical document identifier to a stable signed int64 key."""

    canonical_document_id = require_canonical_id(document_id)
    digest_prefix = sha256(canonical_document_id.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest_prefix, byteorder="big", signed=True)


def _strict_lock_result(row: object, column: str) -> bool:
    if not isinstance(row, Mapping):
        raise CanonicalDataError
    result = row.get(column)
    if type(result) is not bool:
        raise CanonicalDataError
    return result


async def _capture_cleanup(operation: Awaitable[None]) -> BaseException | None:
    try:
        with postgres_error_boundary():
            await operation
    except BaseException as error:
        return error
    return None


def _add_cleanup_note(
    primary: BaseException,
    operation: str,
    cleanup_error: BaseException,
) -> None:
    primary.add_note(
        "PostgreSQL advisory-lock cleanup failure during "
        f"{operation}: {type(cleanup_error).__name__}"
    )


def _raise_after_cleanup(
    primary: BaseException,
    cleanup_failures: list[tuple[str, BaseException]],
) -> NoReturn:
    for operation, cleanup_error in cleanup_failures:
        _add_cleanup_note(primary, operation, cleanup_error)
    if isinstance(primary, CancelledError):
        raise primary
    cleanup_cancellation = next(
        (
            failure
            for _, failure in cleanup_failures
            if isinstance(failure, CancelledError)
        ),
        None,
    )
    if cleanup_cancellation is not None:
        raise cleanup_cancellation
    raise primary


async def _discard_connection(
    pool: _ConnectionPool,
    connection: AsyncConnection[DictRow],
    primary: BaseException,
) -> NoReturn:
    """Close a possibly lock-bearing session before reconciling it with its pool."""

    cleanup_failures: list[tuple[str, BaseException]] = []
    close_error = await _capture_cleanup(connection.close())
    if close_error is None:
        return_error = await _capture_cleanup(pool.putconn(connection))
        if return_error is not None:
            cleanup_failures.append(("putconn", return_error))
    else:
        cleanup_failures.append(("close", close_error))
    _raise_after_cleanup(primary, cleanup_failures)


async def _return_connection(
    pool: _ConnectionPool,
    connection: AsyncConnection[DictRow],
) -> None:
    """Return a known-unlocked connection, closing it if pool return fails."""

    try:
        with postgres_error_boundary():
            await pool.putconn(connection)
    except BaseException as primary:
        cleanup_failures: list[tuple[str, BaseException]] = []
        close_error = await _capture_cleanup(connection.close())
        if close_error is not None:
            cleanup_failures.append(("close", close_error))
        _raise_after_cleanup(primary, cleanup_failures)


class AdvisoryLockLease:
    """Own one dedicated session connection until explicit release."""

    def __init__(
        self,
        pool: _ConnectionPool,
        connection: AsyncConnection[DictRow],
        key: int,
    ) -> None:
        self._pool = pool
        self._connection: AsyncConnection[DictRow] | None = connection
        self._key = key

    async def release(self) -> None:
        """Release at most once and never return a possibly locked session."""

        connection = self._connection
        if connection is None:
            return
        self._connection = None

        try:
            with postgres_error_boundary():
                cursor = await connection.execute(
                    "SELECT pg_advisory_unlock(%s) AS released",
                    (self._key,),
                )
                row = await cursor.fetchone()
            if not _strict_lock_result(row, "released"):
                raise InvariantViolationError
            with postgres_error_boundary():
                await connection.rollback()
        except BaseException as primary:
            await _discard_connection(self._pool, connection, primary)

        await _return_connection(self._pool, connection)


class PostgresAdvisoryLockManager:
    """Acquire per-document session locks on dedicated pooled connections."""

    def __init__(self, pool: _ConnectionPool) -> None:
        self._pool = pool

    async def try_acquire(self, document_id: str) -> AdvisoryLockLease | None:
        """Return a lease, or ``None`` only when another session holds the lock."""

        key = advisory_lock_key(document_id)
        with postgres_error_boundary():
            connection = await self._pool.getconn()

        try:
            with postgres_error_boundary():
                cursor = await connection.execute(
                    "SELECT pg_try_advisory_lock(%s) AS acquired",
                    (key,),
                )
                row = await cursor.fetchone()
            acquired = _strict_lock_result(row, "acquired")
            with postgres_error_boundary():
                await connection.rollback()
        except BaseException as primary:
            await _discard_connection(self._pool, connection, primary)

        if not acquired:
            await _return_connection(self._pool, connection)
            return None
        return AdvisoryLockLease(self._pool, connection, key)
