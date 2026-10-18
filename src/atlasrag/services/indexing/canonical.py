"""Reconstruct candidate authority from a complete persisted PostgreSQL boundary."""

from dataclasses import dataclass

from atlasrag.domain.errors import CanonicalDataError, InvariantViolationError
from atlasrag.domain.ingestion.chunks import ChunkRecord
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.source import CandidateBuildContext, CanonicalSource
from atlasrag.domain.ingestion.validation import ValidationOutcome
from atlasrag.repositories.postgres.records import IngestionStatus, StoredDocument
from atlasrag.services.indexing.ports import IndexingUnitOfWork
from atlasrag.services.ingestion.mapping import (
    ChunkStrategyMetadataV1,
    map_chunks_from_stored,
    map_document_from_stored,
)
from atlasrag.services.ingestion.validation import validate_chunk_set


@dataclass(frozen=True)
class CanonicalCandidate:
    """Immutable canonical facts including Parents and the owning building triple."""

    context: CandidateBuildContext
    stored_document: StoredDocument
    document: DocumentContainer
    chunks: tuple[ChunkRecord, ...]


async def load_canonical_candidate(
    unit_of_work: IndexingUnitOfWork,
    context: CandidateBuildContext,
    *,
    expected_chunker_behavior_version: str,
) -> CanonicalCandidate:
    """Read all elements/chunks and validate their actual source and full lineage.

    The caller owns the short transaction and the whole-build document lease.
    This function neither commits nor performs derived-index/provider work.
    """
    canonical = CandidateBuildContext.model_validate(context)
    stored = await unit_of_work.documents.get_by_id(canonical.document_id)
    if stored is None:
        raise InvariantViolationError
    stored = StoredDocument.model_validate(stored)
    source = canonical.source
    if (
        stored.ingestion_status
        not in {IngestionStatus.CHUNKED, IngestionStatus.INDEXING}
        or stored.document_id != canonical.document_id
        or stored.relative_source_path != source.relative_source_path
        or stored.file_name != source.file_name
        or stored.source_type is not source.source_type
        or stored.observed_content_hash != canonical.content_hash
        or stored.building_revision_id != canonical.revision_id
        or stored.building_content_hash != canonical.content_hash
        or stored.building_pipeline_fingerprint != canonical.pipeline_fingerprint
        or stored.current_revision_id == canonical.revision_id
    ):
        raise InvariantViolationError
    elements = await unit_of_work.elements.list_for_revision(
        canonical.document_id, canonical.revision_id
    )
    persisted_chunks = await unit_of_work.chunks.list_for_revision(
        canonical.document_id, canonical.revision_id
    )
    # The frozen mapper validates schema versions and current/building slot ownership.
    document = map_document_from_stored(
        stored, elements, revision_id=canonical.revision_id
    )
    chunks = map_chunks_from_stored(persisted_chunks)
    if any(
        ChunkStrategyMetadataV1.model_validate(
            chunk.strategy_metadata
        ).chunker_behavior_version
        != expected_chunker_behavior_version
        for chunk in persisted_chunks
    ):
        raise CanonicalDataError
    if (
        document.source != CanonicalSource.from_source_file(source)
        or validate_chunk_set(document, chunks).outcome is ValidationOutcome.FAIL
    ):
        raise CanonicalDataError
    return CanonicalCandidate(canonical, stored, document, chunks)
