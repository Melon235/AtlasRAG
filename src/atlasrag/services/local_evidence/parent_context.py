"""Batch canonical Parent/Table expansion inside the published revision."""

from __future__ import annotations

from typing import cast

from atlasrag.domain.enums import EvidenceType
from atlasrag.domain.errors import CanonicalDataError
from atlasrag.domain.evidence import EvidenceRef, LocalEvidence, LocalProvenance
from atlasrag.domain.ingestion.chunks import (
    ChunkType,
    TableProvenance,
    TextParentProvenance,
)
from atlasrag.repositories.postgres.records import StoredChunk, StoredDocument
from atlasrag.services.ingestion.mapping import map_chunk_from_stored
from atlasrag.services.local_evidence.ports import (
    ChunkBatchReader,
    LocalEvidenceUnitOfWorkFactory,
)


class ParentContextService:
    def __init__(self, uow_factory: LocalEvidenceUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def expand(self, refs: tuple[EvidenceRef, ...]) -> tuple[LocalEvidence, ...]:
        if not refs:
            return ()
        chunk_ids = tuple(ref.context_id for ref in refs)
        if len(set(chunk_ids)) != len(chunk_ids):
            raise CanonicalDataError
        async with self._uow_factory() as unit:
            reader = cast(ChunkBatchReader, unit.chunks)
            stored_chunks = await reader.list_by_ids(chunk_ids)
            by_id = {chunk.chunk_id: chunk for chunk in stored_chunks}
            if len(by_id) != len(chunk_ids):
                raise CanonicalDataError
            documents: dict[str, StoredDocument] = {}
            for ref in refs:
                if ref.document_id not in documents:
                    document = await unit.documents.get_by_id(ref.document_id)
                    if document is None:
                        raise CanonicalDataError
                    documents[ref.document_id] = document
        evidence: list[LocalEvidence] = []
        for ref in refs:
            stored = by_id[ref.context_id]
            document = documents[ref.document_id]
            evidence.append(self._expand_one(ref, stored, document))
        return tuple(evidence)

    def _expand_one(
        self, ref: EvidenceRef, stored: StoredChunk, document: StoredDocument
    ) -> LocalEvidence:
        try:
            chunk = map_chunk_from_stored(stored)
        except (CanonicalDataError, ValueError):
            raise CanonicalDataError from None
        if (
            document.document_id != ref.document_id
            or document.current_revision_id != ref.revision_id
            or chunk.document_id != ref.document_id
            or chunk.revision_id != ref.revision_id
            or chunk.chunk_id != ref.context_id
            or chunk.parent_id is not None
        ):
            raise CanonicalDataError
        if ref.evidence_type is EvidenceType.TEXT_PARENT:
            if (
                chunk.chunk_type is not ChunkType.TEXT_PARENT
                or not isinstance(chunk.provenance, TextParentProvenance)
                or chunk.table_slice is not None
            ):
                raise CanonicalDataError
            anchor = None
        elif ref.evidence_type is EvidenceType.TABLE:
            if (
                chunk.chunk_type is not ChunkType.TABLE
                or not isinstance(chunk.provenance, TableProvenance)
                or chunk.table_slice is None
            ):
                raise CanonicalDataError
            anchor = chunk.table_slice.source_anchor
        else:
            raise CanonicalDataError
        return LocalEvidence(
            evidence_type=ref.evidence_type,
            context_id=ref.context_id,
            document_id=ref.document_id,
            revision_id=ref.revision_id,
            content=chunk.raw_content,
            provenance=LocalProvenance(
                file_name=document.file_name,
                relative_source_path=document.relative_source_path,
                section_path=chunk.section_path,
                source_anchor=anchor,
            ),
        )
