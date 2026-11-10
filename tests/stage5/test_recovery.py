"""Resume durable CHUNKED/PARSED candidates without parser reruns."""

from __future__ import annotations

from pathlib import Path

import pytest
from stage3.test_candidate_services import NOW
from stage4.candidate_fakes import Embedding, Index, candidate

from atlasrag.domain.errors import SourceMutationError
from atlasrag.repositories.postgres.records import IngestionStatus
from atlasrag.services.sync.models import RecoveryBoundary, RecoveryTarget
from atlasrag.services.sync.recovery import DocumentRecoveryService


def target_for(context: object, boundary: RecoveryBoundary) -> RecoveryTarget:
    return RecoveryTarget(
        document_id=context.document_id,  # type: ignore[attr-defined]
        revision_id=context.revision_id,  # type: ignore[attr-defined]
        relative_source_path=context.source.relative_source_path,  # type: ignore[attr-defined]
        boundary=boundary,
        content_hash=context.content_hash,  # type: ignore[attr-defined]
        pipeline_fingerprint=context.pipeline_fingerprint,  # type: ignore[attr-defined]
    )


def service(
    factory: object, runtime: object, embedding: object, index: object
) -> DocumentRecoveryService:
    return DocumentRecoveryService(
        factory,  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
        embedding=embedding,  # type: ignore[arg-type]
        index=index,  # type: ignore[arg-type]
        clock=lambda: NOW,
        batch_size=2,
        verification_batch_size=4,
    )


@pytest.mark.asyncio
async def test_chunked_resume_indexes_verifies_and_publishes(tmp_path: Path) -> None:
    factory, context, runtime = candidate(tmp_path)
    embedding = Embedding(factory, runtime)
    index = Index(factory, embedding)
    before = factory.database.documents[context.document_id]
    await service(factory, runtime, embedding, index).resume(
        target_for(context, RecoveryBoundary.CHUNKED), context.source
    )
    after = factory.database.documents[context.document_id]
    assert after.ingestion_status is IngestionStatus.READY
    assert after.current_revision_id == context.revision_id
    assert after.building_revision_id is None
    assert after.current_revision_id != before.current_revision_id
    assert factory.database.query_cache_invalidation_required is True


@pytest.mark.asyncio
async def test_parsed_resume_rechunks_persisted_elements_without_parser(
    tmp_path: Path,
) -> None:
    factory, context, runtime = candidate(tmp_path)
    embedding = Embedding(factory, runtime)
    index = Index(factory, embedding)
    stored = factory.database.documents[context.document_id]
    factory.database.documents[context.document_id] = stored.model_copy(
        update={"ingestion_status": IngestionStatus.PARSED}
    )
    factory.database.chunks.clear()
    await service(factory, runtime, embedding, index).resume(
        target_for(context, RecoveryBoundary.PARSED), context.source
    )
    after = factory.database.documents[context.document_id]
    assert after.current_revision_id == context.revision_id
    assert after.ingestion_status is IngestionStatus.READY


@pytest.mark.asyncio
async def test_source_mutation_discards_candidate_and_preserves_current(
    tmp_path: Path,
) -> None:
    factory, context, runtime = candidate(tmp_path)
    embedding = Embedding(factory, runtime)
    index = Index(factory, embedding)
    before = factory.database.documents[context.document_id]
    (tmp_path / "guides" / "candidate.txt").write_bytes(b"changed bytes")
    with pytest.raises(SourceMutationError):
        await service(factory, runtime, embedding, index).resume(
            target_for(context, RecoveryBoundary.CHUNKED), context.source
        )
    after = factory.database.documents[context.document_id]
    assert after.current_revision_id == before.current_revision_id
    assert after.building_revision_id is None
    assert after.ingestion_status is IngestionStatus.READY


@pytest.mark.asyncio
async def test_stale_candidate_reset_preserves_current_and_clears_residue(
    tmp_path: Path,
) -> None:
    factory, context, runtime = candidate(tmp_path)
    embedding = Embedding(factory, runtime)
    index = Index(factory, embedding)
    index.rows = []
    before = factory.database.documents[context.document_id]
    await service(factory, runtime, embedding, index).reset_stale_candidate(
        context.document_id, "rev_" + "9" * 64
    )
    after = factory.database.documents[context.document_id]
    assert after.current_revision_id == before.current_revision_id
    assert after.building_revision_id is None
    assert (context.document_id, context.revision_id) not in factory.database.chunks
    assert (context.document_id, context.revision_id) not in factory.database.elements
