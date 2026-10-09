"""Revision-scoped canonical element persistence primitives."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from atlasrag.domain.errors import InvariantViolationError
from atlasrag.repositories.postgres.errors import (
    marks_uow_failed,
    postgres_error_boundary,
    require_canonical_id,
    validate_canonical_row,
    validate_count_row,
    validate_identity_rows,
    validate_rowcount,
)
from atlasrag.repositories.postgres.records import StoredElement

_COLUMNS = """
    element_id,
    document_id,
    revision_id,
    element_type,
    order_index,
    content,
    section_path,
    source_anchor,
    structured_content,
    metadata,
    created_at
"""

_INSERT = f"""
    INSERT INTO document_elements ({_COLUMNS})
    VALUES ({", ".join(["%s"] * 11)})
"""


def _element_parameters(element: StoredElement) -> tuple[object, ...]:
    serialized: dict[str, object] = element.model_dump(mode="json")
    source_anchor = serialized["source_anchor"]
    return (
        element.element_id,
        element.document_id,
        element.revision_id,
        element.element_type,
        element.order_index,
        element.content,
        Jsonb(serialized["section_path"]),
        Jsonb(source_anchor) if source_anchor is not None else None,
        Jsonb(serialized["structured_content"]),
        Jsonb(serialized["metadata"]),
        element.created_at,
    )


class ElementRepository:
    """Store exact revision element sets without owning their transaction."""

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
        elements: Iterable[StoredElement],
    ) -> None:
        self._guard()
        canonical_document_id = require_canonical_id(document_id)
        canonical_revision_id = require_canonical_id(revision_id)
        records = tuple(elements)
        self._validate_set(canonical_document_id, canonical_revision_id, records)

        with postgres_error_boundary():
            await self._connection.execute(
                """
                DELETE FROM document_elements
                WHERE document_id = %s AND revision_id = %s
                """,
                (canonical_document_id, canonical_revision_id),
            )
            for record in sorted(
                records, key=lambda item: (item.order_index, item.element_id)
            ):
                await self._connection.execute(_INSERT, _element_parameters(record))

    @marks_uow_failed
    async def count_revision(self, document_id: str, revision_id: str) -> int:
        self._guard()
        scope = self._scope(document_id, revision_id)
        with postgres_error_boundary():
            cursor = await self._connection.execute(
                """
                SELECT COUNT(*) AS record_count
                FROM document_elements
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
                SELECT element_id
                FROM document_elements
                WHERE document_id = %s AND revision_id = %s
                ORDER BY element_id
                """,
                scope,
            )
            rows = await cursor.fetchall()
        return validate_identity_rows(rows, "element_id")

    @marks_uow_failed
    async def delete_revision(self, document_id: str, revision_id: str) -> int:
        self._guard()
        scope = self._scope(document_id, revision_id)
        with postgres_error_boundary():
            cursor = await self._connection.execute(
                """
                DELETE FROM document_elements
                WHERE document_id = %s AND revision_id = %s
                """,
                scope,
            )
        return validate_rowcount(cursor.rowcount)

    @marks_uow_failed
    async def list_for_revision(
        self, document_id: str, revision_id: str
    ) -> tuple[StoredElement, ...]:
        self._guard()
        scope = self._scope(document_id, revision_id)
        with postgres_error_boundary():
            cursor = await self._connection.execute(
                f"""
                SELECT {_COLUMNS}
                FROM document_elements
                WHERE document_id = %s AND revision_id = %s
                ORDER BY order_index, element_id
                """,
                scope,
            )
            rows = await cursor.fetchall()
        return tuple(validate_canonical_row(StoredElement, row) for row in rows)

    @staticmethod
    def _validate_set(
        document_id: str,
        revision_id: str,
        records: tuple[StoredElement, ...],
    ) -> None:
        identities: set[str] = set()
        for record in records:
            if not isinstance(record, StoredElement):
                raise InvariantViolationError
            if (
                record.document_id != document_id
                or record.revision_id != revision_id
                or record.element_id in identities
            ):
                raise InvariantViolationError
            identities.add(record.element_id)

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
