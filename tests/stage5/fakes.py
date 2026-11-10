"""Small transaction-aware Stage 5 derived-index fake."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence

from atlasrag.providers.index.projection import MilvusRevisionProjection


class MemoryIndex:
    def __init__(self) -> None:
        self.rows: list[MilvusRevisionProjection] = []
        self.deleted: list[tuple[str, str]] = []
        self.events: list[str] = []

    def add(self, rows: Sequence[MilvusRevisionProjection]) -> None:
        self.rows.extend(rows)

    async def delete_revision(self, document_id: str, revision_id: str) -> int:
        self.events.append("delete")
        self.deleted.append((document_id, revision_id))
        self.rows = [
            row
            for row in self.rows
            if (row.document_id, row.revision_id) != (document_id, revision_id)
        ]
        return 0

    async def wait_revision_visible(self, document_id: str, revision_id: str) -> None:
        self.events.append("wait")

    async def iter_revision_projection(
        self, document_id: str, revision_id: str, *, batch_size: int
    ) -> AsyncGenerator[MilvusRevisionProjection, None]:
        self.events.append("revision")
        for row in self.rows:
            if (row.document_id, row.revision_id) == (document_id, revision_id):
                yield row

    async def delete_document(self, document_id: str) -> int:
        self.events.append("delete_document")
        before = len(self.rows)
        self.rows = [row for row in self.rows if row.document_id != document_id]
        return before - len(self.rows)

    async def iter_document_projection(
        self, document_id: str, *, batch_size: int
    ) -> AsyncGenerator[MilvusRevisionProjection, None]:
        self.events.append("document")
        for row in self.rows:
            if row.document_id == document_id:
                yield row

    async def iter_all_projection(
        self, *, batch_size: int
    ) -> AsyncGenerator[MilvusRevisionProjection, None]:
        self.events.append("all")
        for row in self.rows:
            yield row
