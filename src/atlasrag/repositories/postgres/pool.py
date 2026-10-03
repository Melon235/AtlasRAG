"""Explicit lifecycle for the canonical PostgreSQL connection pool."""

from __future__ import annotations

from asyncio import CancelledError
from collections.abc import Awaitable

from psycopg import AsyncConnection, IsolationLevel
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from atlasrag.repositories.postgres.errors import postgres_error_boundary


async def _configure_connection(connection: AsyncConnection[DictRow]) -> None:
    await connection.set_isolation_level(IsolationLevel.READ_COMMITTED)


class PostgresPool:
    """Own an unopened async Psycopg pool until startup explicitly opens it."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        timeout: float = 30.0,
    ) -> None:
        self._timeout = timeout
        self._pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            open=False,
            kwargs={"autocommit": False, "row_factory": dict_row},
            configure=_configure_connection,
        )

    async def open(self) -> None:
        with postgres_error_boundary():
            await self._pool.open(wait=True, timeout=self._timeout)

    async def close(self) -> None:
        with postgres_error_boundary():
            await self._pool.close()

    async def getconn(self) -> AsyncConnection[DictRow]:
        with postgres_error_boundary():
            return await self._pool.getconn()

    async def putconn(self, connection: AsyncConnection[DictRow]) -> None:
        with postgres_error_boundary():
            await self._pool.putconn(connection)

    async def health(self) -> None:
        with postgres_error_boundary():
            connection = await self._pool.getconn()

        primary_error: BaseException | None = None
        try:
            with postgres_error_boundary():
                await connection.execute("SELECT 1")
        except BaseException as error:
            primary_error = error

        cleanup_failures = await self._cleanup_health_connection(connection)
        self._raise_health_outcome(primary_error, cleanup_failures)

    async def _cleanup_health_connection(
        self, connection: AsyncConnection[DictRow]
    ) -> list[tuple[str, BaseException]]:
        cleanup_failures: list[tuple[str, BaseException]] = []
        connection_closed = False

        rollback_error = await self._capture_cleanup(connection.rollback())
        if rollback_error is not None:
            cleanup_failures.append(("rollback", rollback_error))
            close_error = await self._capture_cleanup(connection.close())
            if close_error is None:
                connection_closed = True
            else:
                cleanup_failures.append(("close", close_error))

        return_error = await self._capture_cleanup(self._pool.putconn(connection))
        if return_error is not None:
            cleanup_failures.append(("putconn", return_error))
            if not connection_closed:
                close_error = await self._capture_cleanup(connection.close())
                if close_error is not None:
                    cleanup_failures.append(("close", close_error))

        return cleanup_failures

    @staticmethod
    async def _capture_cleanup(operation: Awaitable[None]) -> BaseException | None:
        try:
            with postgres_error_boundary():
                await operation
        except BaseException as error:
            return error
        return None

    @staticmethod
    def _raise_health_outcome(
        primary_error: BaseException | None,
        cleanup_failures: list[tuple[str, BaseException]],
    ) -> None:
        if primary_error is not None:
            for operation, cleanup_error in cleanup_failures:
                PostgresPool._add_cleanup_note(primary_error, operation, cleanup_error)
            if isinstance(primary_error, CancelledError):
                raise primary_error

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
        if primary_error is not None:
            raise primary_error
        if cleanup_failures:
            operation, primary_cleanup_error = cleanup_failures[0]
            for later_operation, later_error in cleanup_failures[1:]:
                PostgresPool._add_cleanup_note(
                    primary_cleanup_error, later_operation, later_error
                )
            primary_cleanup_error.add_note(
                f"PostgreSQL health cleanup failure during {operation}"
            )
            raise primary_cleanup_error

    @staticmethod
    def _add_cleanup_note(
        primary: BaseException,
        operation: str,
        cleanup_error: BaseException,
    ) -> None:
        primary.add_note(
            "PostgreSQL health cleanup failure during "
            f"{operation}: {type(cleanup_error).__name__}"
        )
