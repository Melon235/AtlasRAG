"""Durable-boundary reconciliation without services."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from stage3.fakes import FakeUnitOfWorkFactory
from stage3.test_candidate_services import (
    _context,
    _document,
    _stored_document,
)

from atlasrag.domain.ingestion.source import CandidateBuildContext
from atlasrag.providers.index.projection import MilvusRevisionProjection
from atlasrag.repositories.postgres.records import (
    FailedStage,
    IngestionStatus,
    StoredDocument,
)
from atlasrag.services.ingestion.chunking import ChunkingProfile, build_chunks
from atlasrag.services.ingestion.mapping import (
    map_chunks_to_stored,
    map_elements_to_stored,
)
from atlasrag.services.sync.models import (
    ReconciliationOutcome,
    RecoveryBoundary,
)
from atlasrag.services.sync.reconciliation import StartupReconciliationService
from stage5.fakes import MemoryIndex

NOW = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)


def service(
    factory: FakeUnitOfWorkFactory,
    root: Path,
    pipeline: str,
    index: object | None = None,
) -> StartupReconciliationService:
    return StartupReconciliationService(
        factory,
        knowledge_directory=root,
        pipeline_fingerprint=pipeline,
        clock=lambda: NOW,
        index=index,
    )


def seed_boundary(
    factory: FakeUnitOfWorkFactory,
    context: CandidateBuildContext,
    *,
    status: IngestionStatus,
    current: bool = False,
    failed_stage: FailedStage | None = None,
    chunks: bool = False,
    partial_chunks: bool = False,
) -> StoredDocument:
    document = _document(context)
    stored = _stored_document(
        context,
        status=status,
        current=current,
        failed_stage=failed_stage,
        building_document=document,
    )
    factory.database.documents[stored.document_id] = stored
    scope = (stored.document_id, context.revision_id)
    factory.database.elements[scope] = map_elements_to_stored(
        document.elements, created_at=NOW
    )
    if chunks or partial_chunks:
        profile = ChunkingProfile(parent_max_chars=100, child_max_chars=10)
        built = build_chunks(document, profile=profile)
        if partial_chunks:
            built = built[:1]
        factory.database.chunks[scope] = map_chunks_to_stored(
            built,
            chunker_behavior_version=context.pipeline_fingerprint,
            profile_fingerprint=profile.profile_fingerprint,
            created_at=NOW,
        )
    return stored


@pytest.mark.asyncio
async def test_received_candidate_is_discarded_and_not_resumed(tmp_path: Path) -> None:
    context = _context(tmp_path)
    factory = FakeUnitOfWorkFactory()
    stored = seed_boundary(factory, context, status=IngestionStatus.RECEIVED)
    result = await service(factory, tmp_path, context.pipeline_fingerprint).reconcile()
    assert result.outcome is ReconciliationOutcome.REPAIRED
    assert result.recovery_targets == ()
    after = factory.database.documents[stored.document_id]
    assert after.ingestion_status is IngestionStatus.RECEIVED
    assert after.building_revision_id is None


@pytest.mark.asyncio
async def test_parsed_candidate_becomes_resume_target_without_reparse(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    factory = FakeUnitOfWorkFactory()
    stored = seed_boundary(factory, context, status=IngestionStatus.PARSED)
    result = await service(factory, tmp_path, context.pipeline_fingerprint).reconcile()
    assert result.outcome is ReconciliationOutcome.CLEAN
    assert result.recovery_targets[0].boundary is RecoveryBoundary.PARSED
    assert result.recovery_targets[0].revision_id == stored.building_revision_id


@pytest.mark.asyncio
async def test_chunking_partial_chunks_roll_back_to_parsed(tmp_path: Path) -> None:
    context = _context(tmp_path)
    factory = FakeUnitOfWorkFactory()
    stored = seed_boundary(
        factory, context, status=IngestionStatus.CHUNKING, partial_chunks=True
    )
    result = await service(factory, tmp_path, context.pipeline_fingerprint).reconcile()
    assert result.outcome is ReconciliationOutcome.REPAIRED
    assert result.recovery_targets[0].boundary is RecoveryBoundary.PARSED
    after = factory.database.documents[stored.document_id]
    assert after.ingestion_status is IngestionStatus.PARSED
    assert (
        after.document_id,
        after.building_revision_id,
    ) not in factory.database.chunks


@pytest.mark.asyncio
async def test_chunked_candidate_resumes_from_chunked(tmp_path: Path) -> None:
    context = _context(tmp_path)
    factory = FakeUnitOfWorkFactory()
    seed_boundary(factory, context, status=IngestionStatus.CHUNKED, chunks=True)
    result = await service(factory, tmp_path, context.pipeline_fingerprint).reconcile()
    assert result.outcome is ReconciliationOutcome.CLEAN
    assert result.recovery_targets[0].boundary is RecoveryBoundary.CHUNKED


@pytest.mark.asyncio
async def test_indexing_deletes_partial_milvus_and_normalizes_to_chunked(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    factory = FakeUnitOfWorkFactory()
    stored = seed_boundary(
        factory, context, status=IngestionStatus.INDEXING, chunks=True
    )
    index = MemoryIndex()
    index.add(
        (
            MilvusRevisionProjection(
                chunk_id="chunk_" + "a" * 64,
                document_id=stored.document_id,
                revision_id=context.revision_id,
                parent_id=None,
                chunk_type="TABLE",
                source_type="TXT",
                file_name="candidate.txt",
                content="partial",
                sparse_text="partial",
                structured_metadata={"schema_version": 1},
            ),
        )
    )
    result = await service(
        factory, tmp_path, context.pipeline_fingerprint, index
    ).reconcile()
    assert result.outcome is ReconciliationOutcome.REPAIRED
    assert result.recovery_targets[0].boundary is RecoveryBoundary.CHUNKED
    assert index.rows == []
    after = factory.database.documents[stored.document_id]
    assert after.ingestion_status is IngestionStatus.CHUNKED


@pytest.mark.asyncio
async def test_failed_candidate_is_preserved_and_milvus_residue_removed(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    factory = FakeUnitOfWorkFactory()
    stored = seed_boundary(
        factory,
        context,
        status=IngestionStatus.FAILED,
        failed_stage=FailedStage.INDEX_WRITE,
    )
    index = MemoryIndex()
    result = await service(
        factory, tmp_path, context.pipeline_fingerprint, index
    ).reconcile()
    assert result.outcome is ReconciliationOutcome.CLEAN
    after = factory.database.documents[stored.document_id]
    assert after.ingestion_status is IngestionStatus.FAILED
    assert after.failed_stage is FailedStage.INDEX_WRITE
    assert index.deleted == [(stored.document_id, stored.building_revision_id)]


@pytest.mark.asyncio
async def test_source_mutation_discards_candidate_but_preserves_current(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    factory = FakeUnitOfWorkFactory()
    stored = seed_boundary(
        factory, context, status=IngestionStatus.PARSED, current=True
    )
    (tmp_path / "guides" / "candidate.txt").write_bytes(b"mutated bytes")
    result = await service(factory, tmp_path, context.pipeline_fingerprint).reconcile()
    assert result.outcome is ReconciliationOutcome.REPAIRED
    after = factory.database.documents[stored.document_id]
    assert after.current_revision_id is not None
    assert after.building_revision_id is None
    assert after.ingestion_status is IngestionStatus.READY


@pytest.mark.asyncio
async def test_unknown_metadata_ownership_is_blocked(tmp_path: Path) -> None:
    context = _context(tmp_path)
    factory = FakeUnitOfWorkFactory()
    stored = seed_boundary(factory, context, status=IngestionStatus.PARSED)
    broken = stored.model_copy(update={"parse_metadata": {"schema_version": 99}})
    factory.database.documents[stored.document_id] = broken
    result = await service(factory, tmp_path, context.pipeline_fingerprint).reconcile()
    assert result.outcome is ReconciliationOutcome.BLOCKED
    assert "UNKNOWN_OR_INVALID_METADATA" in result.blocked_reasons


@pytest.mark.asyncio
async def test_knowledge_directory_file_is_blocked_before_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "not-a-directory"
    root.write_text("file", encoding="utf-8")
    factory = FakeUnitOfWorkFactory()
    context = _context(tmp_path)
    seed_boundary(factory, context, status=IngestionStatus.PARSED)
    result = await service(factory, root, context.pipeline_fingerprint).reconcile()
    assert result.outcome is ReconciliationOutcome.BLOCKED
    assert "KNOWLEDGE_DIRECTORY_UNAVAILABLE" in result.blocked_reasons
