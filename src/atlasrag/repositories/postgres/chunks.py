"""Revision-scoped canonical chunk persistence primitives."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from atlasrag.domain.errors import CanonicalDataError, InvariantViolationError
from atlasrag.repositories.postgres.errors import (
    marks_uow_failed,
    postgres_error_boundary,
    require_canonical_id,
    validate_canonical_row,
    validate_count_row,
    validate_identity_rows,
    validate_rowcount,
)
from atlasrag.repositories.postgres.records import StoredChunk, StoredChunkType

_COLUMNS = """
    chunk_id,
    document_id,
    revision_id,
    chunk_type,
    parent_id,
    content,
    section_path,
    source_anchor,
    sheet_name,
    metadata,
    strategy_metadata,
    created_at
"""

_INSERT = f"""
    INSERT INTO chunks ({_COLUMNS})
    VALUES ({", ".join(["%s"] * 12)})
"""


def _chunk_parameters(chunk: StoredChunk) -> tuple[object, ...]:
    serialized: dict[str, object] = chunk.model_dump(mode="json")
    source_anchor = serialized["source_anchor"]
    return (
        chunk.chunk_id,
        chunk.document_id,
        chunk.revision_id,
        chunk.chunk_type.value,
        chunk.parent_id,
        chunk.content,
        Jsonb(serialized["section_path"]),
        Jsonb(source_anchor) if source_anchor is not None else None,
        chunk.sheet_name,
        Jsonb(serialized["metadata"]),
        Jsonb(serialized["strategy_metadata"]),
        chunk.created_at,
    )


class ChunkRepository:
    """Store exact revision chunk sets without owning their transaction."""

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
    async def replace_revision_set(
        self,
        document_id: str,
        revision_id: str,
        chunks: Iterable[StoredChunk],
    ) -> None:
        self._guard()
        canonical_document_id = require_canonical_id(document_id)
        canonical_revision_id = require_canonical_id(revision_id)
        records = tuple(chunks)
        self._validate_set(canonical_document_id, canonical_revision_id, records)

        with postgres_error_boundary():
            await self._connection.execute(
                """
                DELETE FROM chunks
                WHERE document_id = %s AND revision_id = %s
                """,
                (canonical_document_id, canonical_revision_id),
            )
            for record in sorted(records, key=lambda item: item.chunk_id):
                await self._connection.execute(_INSERT, _chunk_parameters(record))

    @marks_uow_failed
    async def count_revision(self, document_id: str, revision_id: str) -> int:
        self._guard()
        scope = self._scope(document_id, revision_id)
        with postgres_error_boundary():
            cursor = await self._connection.execute(
                """
                SELECT COUNT(*) AS record_count
                FROM chunks
                WHERE document_id = %s AND revision_id = %s
                """,
                scope,
            )
            row = await cursor.fetchone()
        return validate_count_row(row)

    @marks_uow_failed
    async def list_revision_ids(
        self, document_id: str, revision_id: str
    ) -> tuple[str, ...]:
        self._guard()
        scope = self._scope(document_id, revision_id)
        with postgres_error_boundary():
            cursor = await self._connection.execute(
                """
                SELECT chunk_id
                FROM chunks
                WHERE document_id = %s AND revision_id = %s
                ORDER BY chunk_id
                """,
                scope,
            )
            rows = await cursor.fetchall()
        return validate_identity_rows(rows, "chunk_id")

    @marks_uow_failed
    async def delete_revision(self, document_id: str, revision_id: str) -> int:
        self._guard()
        scope = self._scope(document_id, revision_id)
        with postgres_error_boundary():
            cursor = await self._connection.execute(
                """
                DELETE FROM chunks
                WHERE document_id = %s AND revision_id = %s
                """,
                scope,
            )
        return validate_rowcount(cursor.rowcount)

    @marks_uow_failed
    async def list_for_revision(
        self, document_id: str, revision_id: str
    ) -> tuple[StoredChunk, ...]:
        self._guard()
        scope = self._scope(document_id, revision_id)
        with postgres_error_boundary():
            cursor = await self._connection.execute(
                f"""
                SELECT {_COLUMNS}
                FROM chunks
                WHERE document_id = %s AND revision_id = %s
                ORDER BY chunk_id
                """,
                scope,
            )
            rows = await cursor.fetchall()
        return tuple(validate_canonical_row(StoredChunk, row) for row in rows)

    @marks_uow_failed
    async def get_by_id(self, chunk_id: str) -> StoredChunk | None:
        self._guard()
        canonical_chunk_id = require_canonical_id(chunk_id)
        with postgres_error_boundary():
            cursor = await self._connection.execute(
                f"SELECT {_COLUMNS} FROM chunks WHERE chunk_id = %s",
                (canonical_chunk_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return validate_canonical_row(StoredChunk, row)

    @marks_uow_failed
    async def get_canonical_context(
        self, document_id: str, revision_id: str, chunk_id: str
    ) -> StoredChunk | None:
        self._guard()
        scope = (
            require_canonical_id(document_id),
            require_canonical_id(revision_id),
            require_canonical_id(chunk_id),
        )
        with postgres_error_boundary():
            cursor = await self._connection.execute(
                f"""
                SELECT {_COLUMNS}
                FROM chunks
                WHERE document_id = %s AND revision_id = %s AND chunk_id = %s
                """,
                scope,
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        chunk = validate_canonical_row(StoredChunk, row)
        if chunk.chunk_type not in {StoredChunkType.TEXT_PARENT, StoredChunkType.TABLE}:
            raise CanonicalDataError
        return chunk

    @staticmethod
    def _validate_set(
        document_id: str,
        revision_id: str,
        records: tuple[StoredChunk, ...],
    ) -> None:
        identities: set[str] = set()
        for record in records:
            if not isinstance(record, StoredChunk):
                raise InvariantViolationError
            if (
                record.document_id != document_id
                or record.revision_id != revision_id
                or record.chunk_id in identities
            ):
                raise InvariantViolationError
            identities.add(record.chunk_id)

    @staticmethod
    def _scope(document_id: str, revision_id: str) -> tuple[str, str]:
        return (
            require_canonical_id(document_id),
            require_canonical_id(revision_id),
        )

    def _guard(self) -> None:
        if self._ensure_active is not None:
            self._ensure_active()

    def _repository_failed(self) -> None:
        if self._mark_failed is not None:
            self._mark_failed()
