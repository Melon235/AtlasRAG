"""Typed document persistence bound to one caller-owned transaction."""

from __future__ import annotations

from collections.abc import Callable

from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from atlasrag.repositories.postgres.errors import (
    marks_uow_failed,
    postgres_error_boundary,
    require_canonical_id,
    validate_canonical_row,
)
from atlasrag.repositories.postgres.records import StoredDocument

_COLUMNS = """
    document_id,
    file_name,
    relative_source_path,
    source_type,
    observed_content_hash,
    current_revision_id,
    current_content_hash,
    current_pipeline_fingerprint,
    building_revision_id,
    building_content_hash,
    building_pipeline_fingerprint,
    ingestion_status,
    failed_stage,
    parse_metadata,
    created_at,
    updated_at
"""

_UPSERT = f"""
    INSERT INTO documents ({_COLUMNS})
    VALUES ({", ".join(["%s"] * 16)})
    ON CONFLICT (document_id) DO UPDATE SET
        file_name = EXCLUDED.file_name,
        relative_source_path = EXCLUDED.relative_source_path,
        source_type = EXCLUDED.source_type,
        observed_content_hash = EXCLUDED.observed_content_hash,
        current_revision_id = EXCLUDED.current_revision_id,
        current_content_hash = EXCLUDED.current_content_hash,
        current_pipeline_fingerprint = EXCLUDED.current_pipeline_fingerprint,
        building_revision_id = EXCLUDED.building_revision_id,
        building_content_hash = EXCLUDED.building_content_hash,
        building_pipeline_fingerprint = EXCLUDED.building_pipeline_fingerprint,
        ingestion_status = EXCLUDED.ingestion_status,
        failed_stage = EXCLUDED.failed_stage,
        parse_metadata = EXCLUDED.parse_metadata,
        updated_at = EXCLUDED.updated_at
"""


def _document_parameters(document: StoredDocument) -> tuple[object, ...]:
    serialized = document.model_dump(mode="json")
    return (
        document.document_id,
        document.file_name,
        document.relative_source_path,
        document.source_type.value,
        document.observed_content_hash,
        document.current_revision_id,
        document.current_content_hash,
        document.current_pipeline_fingerprint,
        document.building_revision_id,
        document.building_content_hash,
        document.building_pipeline_fingerprint,
        document.ingestion_status.value,
        document.failed_stage.value if document.failed_stage is not None else None,
        Jsonb(serialized["parse_metadata"]),
        document.created_at,
        document.updated_at,
    )


class DocumentRepository:
    """Store and load immutable document records without transaction control."""

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
    async def put(self, document: StoredDocument) -> None:
        self._guard()
        with postgres_error_boundary():
            await self._connection.execute(_UPSERT, _document_parameters(document))

    @marks_uow_failed
    async def get_by_id(self, document_id: str) -> StoredDocument | None:
        return await self._get_one("document_id", require_canonical_id(document_id))

    @marks_uow_failed
    async def get_by_relative_source_path(
        self, relative_source_path: str
    ) -> StoredDocument | None:
        return await self._get_one("relative_source_path", relative_source_path)

    @marks_uow_failed
    async def list(self) -> tuple[StoredDocument, ...]:
        self._guard()
        with postgres_error_boundary():
            cursor = await self._connection.execute(
                f"""
                SELECT {_COLUMNS}
                FROM documents
                ORDER BY relative_source_path, document_id
                """
            )
            rows = await cursor.fetchall()
        return tuple(validate_canonical_row(StoredDocument, row) for row in rows)

    async def _get_one(self, column: str, value: str) -> StoredDocument | None:
        self._guard()
        if column not in {"document_id", "relative_source_path"}:
            raise AssertionError("unsupported document lookup column")
        with postgres_error_boundary():
            cursor = await self._connection.execute(
                f"SELECT {_COLUMNS} FROM documents WHERE {column} = %s",
                (value,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return validate_canonical_row(StoredDocument, row)

    def _guard(self) -> None:
        if self._ensure_active is not None:
            self._ensure_active()

    def _repository_failed(self) -> None:
        if self._mark_failed is not None:
            self._mark_failed()
