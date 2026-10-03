"""Typed runtime metadata persistence within a caller-owned transaction."""

from __future__ import annotations

from collections.abc import Callable

from psycopg import AsyncConnection
from psycopg.rows import DictRow

from atlasrag.domain.errors import CanonicalDataError, InvariantViolationError
from atlasrag.repositories.postgres.errors import (
    marks_uow_failed,
    postgres_error_boundary,
    validate_canonical_row,
)
from atlasrag.repositories.postgres.records import RuntimeMetadata

_SINGLETON_KEY = "runtime"


class RuntimeMetadataRepository:
    """Read and update the required singleton cache-correctness marker."""

    def __init__(
        self,
        connection: AsyncConnection[DictRow],
        *,
        ensure_active: Callable[[], None] | None = None,
        mark_failed: Callable[[], None] | None = None,
    ) -> None:
        self._connection = connection
        self._ensure_active = ensure_active
        self._mark_failed = mark_failed

    @marks_uow_failed
    async def get(self) -> RuntimeMetadata:
        self._guard()
        with postgres_error_boundary():
            cursor = await self._connection.execute(
                """
                SELECT query_cache_invalidation_required
                FROM runtime_metadata
                WHERE metadata_key = %s
                """,
                (_SINGLETON_KEY,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise CanonicalDataError
        return validate_canonical_row(RuntimeMetadata, row)

    @marks_uow_failed
    async def set_query_cache_invalidation_required(
        self, required: bool
    ) -> RuntimeMetadata:
        self._guard()
        if not isinstance(required, bool):
            raise InvariantViolationError
        with postgres_error_boundary():
            cursor = await self._connection.execute(
                """
                UPDATE runtime_metadata
                SET query_cache_invalidation_required = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE metadata_key = %s
                RETURNING query_cache_invalidation_required
                """,
                (required, _SINGLETON_KEY),
            )
            row = await cursor.fetchone()
        if row is None:
            raise CanonicalDataError
        return validate_canonical_row(RuntimeMetadata, row)

    def _guard(self) -> None:
        if self._ensure_active is not None:
            self._ensure_active()

    def _repository_failed(self) -> None:
        if self._mark_failed is not None:
            self._mark_failed()
