"""Explicit-commit PostgreSQL Unit of Work."""

from __future__ import annotations

from asyncio import CancelledError
from collections.abc import Awaitable
from enum import Enum, auto
from types import TracebackType
from typing import Protocol, Self

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow

from atlasrag.domain.errors import InvariantViolationError
from atlasrag.repositories.postgres.chunks import ChunkRepository
from atlasrag.repositories.postgres.documents import DocumentRepository
from atlasrag.repositories.postgres.elements import ElementRepository
from atlasrag.repositories.postgres.errors import postgres_error_boundary
from atlasrag.repositories.postgres.runtime_metadata import RuntimeMetadataRepository


class _ConnectionPool(Protocol):
    async def getconn(self) -> AsyncConnection[DictRow]: ...

    async def putconn(self, connection: AsyncConnection[DictRow]) -> None: ...


class _State(Enum):
    NEW = auto()
    ACTIVE = auto()
    COMMITTING = auto()
    COMMITTED = auto()
    FAILED = auto()
    FINISHED = auto()


class PostgresUnitOfWork:
    """Bind repositories to one transaction and roll back unless committed."""

    def __init__(self, pool: _ConnectionPool) -> None:
        self._pool = pool
        self._state = _State.NEW
        self._connection: AsyncConnection[DictRow] | None = None
        self._documents: DocumentRepository | None = None
        self._elements: ElementRepository | None = None
        self._chunks: ChunkRepository | None = None
        self._runtime_metadata: RuntimeMetadataRepository | None = None

    @property
    def documents(self) -> DocumentRepository:
        self._assert_active()
        assert self._documents is not None
        return self._documents

    @property
    def elements(self) -> ElementRepository:
        self._assert_active()
        assert self._elements is not None
        return self._elements

    @property
    def chunks(self) -> ChunkRepository:
        self._assert_active()
        assert self._chunks is not None
        return self._chunks

    @property
    def runtime_metadata(self) -> RuntimeMetadataRepository:
        self._assert_active()
        assert self._runtime_metadata is not None
        return self._runtime_metadata

    async def __aenter__(self) -> Self:
        if self._state is not _State.NEW:
            raise InvariantViolationError
        try:
            with postgres_error_boundary():
                connection = await self._pool.getconn()
        except BaseException:
            self._state = _State.FINISHED
            raise
        self._connection = connection
        self._documents = DocumentRepository(
            connection,
            ensure_active=self._assert_active,
            mark_failed=self._mark_failed,
        )
        self._elements = ElementRepository(
            connection,
            ensure_active=self._assert_active,
            mark_failed=self._mark_failed,
        )
        self._chunks = ChunkRepository(
            connection,
            ensure_active=self._assert_active,
            mark_failed=self._mark_failed,
        )
        self._runtime_metadata = RuntimeMetadataRepository(
            connection,
            ensure_active=self._assert_active,
            mark_failed=self._mark_failed,
        )
        self._state = _State.ACTIVE
        return self

    async def commit(self) -> None:
        self._assert_active()
        connection = self._require_connection()
        if connection.info.transaction_status not in {
            TransactionStatus.IDLE,
            TransactionStatus.INTRANS,
        }:
            self._state = _State.FAILED
            raise InvariantViolationError
        self._state = _State.COMMITTING
        try:
            with postgres_error_boundary():
                await connection.commit()
        except BaseException:
            self._state = _State.FAILED
            raise
        self._state = _State.COMMITTED

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self._require_connection()
        cleanup_failures: list[tuple[str, BaseException]] = []
        connection_closed = False

        if self._state in {_State.ACTIVE, _State.FAILED}:
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

        self._state = _State.FINISHED
        if exc_value is not None:
            for operation, cleanup_error in cleanup_failures:
                self._add_cleanup_note(exc_value, operation, cleanup_error)
            if isinstance(exc_value, CancelledError):
                return

        cancellation = next(
            (
                failure
                for _, failure in cleanup_failures
                if isinstance(failure, CancelledError)
            ),
            None,
        )
        if cancellation is not None:
            raise cancellation
        if exc_value is not None:
            return
        if cleanup_failures:
            operation, primary_cleanup_error = cleanup_failures[0]
            for later_operation, later_error in cleanup_failures[1:]:
                self._add_cleanup_note(
                    primary_cleanup_error, later_operation, later_error
                )
            primary_cleanup_error.add_note(
                f"PostgreSQL cleanup failure during {operation}"
            )
            raise primary_cleanup_error

    def _assert_active(self) -> None:
        if self._state is not _State.ACTIVE:
            raise InvariantViolationError

    def _mark_failed(self) -> None:
        if self._state is _State.ACTIVE:
            self._state = _State.FAILED

    @staticmethod
    async def _capture_cleanup(operation: Awaitable[None]) -> BaseException | None:
        try:
            with postgres_error_boundary():
                await operation
        except BaseException as error:
            return error
        return None

    @staticmethod
    def _add_cleanup_note(
        primary: BaseException,
        operation: str,
        cleanup_error: BaseException,
    ) -> None:
        primary.add_note(
            "PostgreSQL cleanup failure during "
            f"{operation}: {type(cleanup_error).__name__}"
        )

    def _require_connection(self) -> AsyncConnection[DictRow]:
        if self._connection is None:
            raise InvariantViolationError
        return self._connection
