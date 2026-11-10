"""Current derived-index repair without touching PostgreSQL ownership."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from stage3.fakes import FakeUnitOfWorkFactory
from stage3.test_candidate_services import _context, _document
from stage4.candidate_fakes import config

from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.domain.indexing.models import EmbeddingModelIdentity
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.source import derive_revision_id
from atlasrag.providers.index.models import MilvusChunkRecord
from atlasrag.providers.index.projection import MilvusRevisionProjection
from atlasrag.repositories.postgres.records import IngestionStatus, StoredDocument
from atlasrag.services.ingestion.chunking import ChunkingProfile, build_chunks
from atlasrag.services.ingestion.mapping import (
    build_document_parse_metadata_slot,
    empty_document_parse_metadata_envelope,
    map_chunks_to_stored,
    map_elements_to_stored,
    replace_document_parse_metadata_slot,
    serialize_document_parse_metadata_envelope,
)
from atlasrag.services.sync.current_repair import (
    CurrentIndexRepairService,
    CurrentRepairOutcome,
)
from atlasrag.services.sync.failures import CurrentRepairBlockedError

NOW = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)


class Embedding:
    def __init__(self, runtime: object) -> None:
        models = runtime.models  # type: ignore[attr-defined]
        self.identity = EmbeddingModelIdentity(
            model_id=models.embedding_model_id,
            model_revision=models.embedding_model_revision,
            tokenizer_id=models.embedding_tokenizer_id,
            tokenizer_revision=models.embedding_tokenizer_revision,
            dimension=runtime.part_i.embedding_dimension,  # type: ignore[attr-defined]
            device="cuda:0",
        )
        self.inputs: list[tuple[str, ...]] = []

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        self.inputs.append(tuple(texts))
        return tuple((float(len(text)), 1.0, 0.0) for text in texts)


class Index:
    def __init__(self) -> None:
        self.embedding_dimension = 3
        self.analyzer = "standard"
        self.rows: list[MilvusRevisionProjection] = []
        self.inserted = 0

    async def delete_revision(self, document_id: str, revision_id: str) -> int:
        self.rows = [
            row
            for row in self.rows
            if (row.document_id, row.revision_id) != (document_id, revision_id)
        ]
        return 0

    async def initialize(self) -> None:
        pass

    async def wait_revision_visible(self, document_id: str, revision_id: str) -> None:
        pass

    async def iter_revision_projection(
        self, document_id: str, revision_id: str, *, batch_size: int
    ) -> AsyncGenerator[MilvusRevisionProjection, None]:
        for row in self.rows:
            if (row.document_id, row.revision_id) == (document_id, revision_id):
                yield row

    async def delete_document(self, document_id: str) -> int:
        before = len(self.rows)
        self.rows = [row for row in self.rows if row.document_id != document_id]
        return before - len(self.rows)

    async def iter_document_projection(
        self, document_id: str, *, batch_size: int
    ) -> AsyncGenerator[MilvusRevisionProjection, None]:
        for row in self.rows:
            if row.document_id == document_id:
                yield row

    async def iter_all_projection(
        self, *, batch_size: int
    ) -> AsyncGenerator[MilvusRevisionProjection, None]:
        for row in self.rows:
            yield row

    async def insert_revision_batch(
        self,
        records: Sequence[MilvusChunkRecord],
        *,
        document_id: str,
        revision_id: str,
    ) -> None:
        for record in records:
            payload = record.model_dump(mode="json", exclude={"dense_vector"})
            self.rows.append(MilvusRevisionProjection.model_validate(payload))
            self.inserted += 1


def published(
    tmp_path: Path, *, fingerprint: str | None = None
) -> tuple[
    FakeUnitOfWorkFactory,
    StoredDocument,
    CurrentIndexRepairService,
    DocumentContainer,
    Index,
    Embedding,
]:
    runtime = config()
    context = _context(tmp_path)
    fingerprint = fingerprint or pipeline_fingerprint(runtime)
    revision = derive_revision_id(
        context.document_id, context.content_hash, fingerprint
    )
    document = _document(context, revision_id=revision)
    envelope = empty_document_parse_metadata_envelope()
    envelope = replace_document_parse_metadata_slot(
        envelope,
        slot_name="current",
        slot=build_document_parse_metadata_slot(document),
    )
    stored = StoredDocument(
        document_id=context.document_id,
        file_name=context.source.file_name,
        relative_source_path=context.source.relative_source_path,
        source_type=context.source.source_type,
        observed_content_hash=context.content_hash,
        current_revision_id=revision,
        current_content_hash=context.content_hash,
        current_pipeline_fingerprint=fingerprint,
        ingestion_status=IngestionStatus.READY,
        parse_metadata=serialize_document_parse_metadata_envelope(envelope),  # type: ignore[arg-type]
        created_at=NOW,
        updated_at=NOW,
    )
    factory = FakeUnitOfWorkFactory()
    factory.database.documents[stored.document_id] = stored
    scope = (stored.document_id, revision)
    factory.database.elements[scope] = map_elements_to_stored(
        document.elements, created_at=NOW
    )
    profile = ChunkingProfile()
    factory.database.chunks[scope] = map_chunks_to_stored(
        build_chunks(document, profile=profile),
        chunker_behavior_version=runtime.part_i.chunker_behavior_version,
        profile_fingerprint=profile.profile_fingerprint,
        created_at=NOW,
    )
    index = Index()
    embedding = Embedding(runtime)
    service = CurrentIndexRepairService(
        factory,
        runtime=runtime,
        embedding=embedding,
        index=index,
        clock=lambda: NOW,
        batch_size=2,
    )
    return factory, stored, service, document, index, embedding


@pytest.mark.asyncio
async def test_missing_current_rows_are_rebuilt_and_pg_is_untouched(
    tmp_path: Path,
) -> None:
    factory, stored, service, _, index, embedding = published(tmp_path)
    outcome = await service.reconcile_current(stored.document_id)
    assert outcome is CurrentRepairOutcome.REPAIRED
    assert index.inserted > 0
    after = factory.database.documents[stored.document_id]
    assert after == stored
    assert after.building_revision_id is None


@pytest.mark.asyncio
async def test_clean_current_is_not_rewritten(tmp_path: Path) -> None:
    factory, stored, service, _, index, _ = published(tmp_path)
    await service.reconcile_current(stored.document_id)
    inserted = index.inserted
    outcome = await service.reconcile_current(stored.document_id)
    assert outcome is CurrentRepairOutcome.CLEAN
    assert index.inserted == inserted


@pytest.mark.asyncio
async def test_extra_current_rows_are_removed_by_exact_set_repair(
    tmp_path: Path,
) -> None:
    _, stored, service, _, index, _ = published(tmp_path)
    await service.reconcile_current(stored.document_id)
    assert index.rows
    index.rows.append(
        MilvusRevisionProjection.model_validate(
            index.rows[0].model_dump()
            | {
                "chunk_id": "chunk_" + "f" * 64,
                "content": "extra",
                "sparse_text": "extra",
            }
        )
    )
    outcome = await service.reconcile_current(stored.document_id)
    assert outcome is CurrentRepairOutcome.REPAIRED
    assert all(row.chunk_id != "chunk_" + "f" * 64 for row in index.rows)


@pytest.mark.asyncio
async def test_unreproducible_pipeline_is_blocked_before_destructive_work(
    tmp_path: Path,
) -> None:
    _, stored, service, _, index, _ = published(tmp_path, fingerprint="e" * 64)
    with pytest.raises(CurrentRepairBlockedError) as caught:
        await service.reconcile_current(stored.document_id)
    assert caught.value.code == "PIPELINE_NOT_REPRODUCIBLE"
    assert index.rows == []


@pytest.mark.asyncio
async def test_duplicate_current_rows_are_repaired_not_blocked(tmp_path: Path) -> None:
    _, stored, service, _, index, _ = published(tmp_path)
    await service.reconcile_current(stored.document_id)
    index.rows.append(index.rows[0].model_copy())
    outcome = await service.reconcile_current(stored.document_id)
    assert outcome is CurrentRepairOutcome.REPAIRED
