"""Real PostgreSQL/Milvus Stage 5 directory sync lifecycle."""

from __future__ import annotations

from contextlib import aclosing
from pathlib import Path
from typing import Any

import pytest

from atlasrag.domain.enums import RetrievalChunkType, SourceType
from atlasrag.repositories.postgres.pool import PostgresPool
from atlasrag.repositories.postgres.records import IngestionStatus
from atlasrag.services.sync.models import ServingSafety, SyncOutcome
from integration.stage4_helpers import Factory
from integration.stage5_helpers import application, request
from integration.test_milvus import IsolatedMilvus
from integration.test_milvus import isolated_milvus as isolated_milvus

pytestmark = pytest.mark.integration


async def _all_rows(index: Any) -> list[Any]:
    async with aclosing(index.iter_all_projection(batch_size=100)) as rows:
        return [row async for row in rows]


@pytest.mark.asyncio
async def test_empty_kb_is_successful_and_safe(
    postgres_uow_factory: Factory,
    postgres_pool: PostgresPool,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    app = application(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    result = await app.run(request(tmp_path))
    assert result.outcome is SyncOutcome.SUCCESS
    assert result.serving_safety is ServingSafety.SAFE_TO_SERVE
    assert await _all_rows(isolated_milvus.provider) == []


@pytest.mark.asyncio
async def test_build_unchanged_change_rename_and_delete(
    postgres_uow_factory: Factory,
    postgres_pool: PostgresPool,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    app = application(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    source = tmp_path / "docs" / "guide.txt"
    source.parent.mkdir()
    source.write_text("first canonical content", encoding="utf-8")

    first = await app.run(request(tmp_path))
    assert first.outcome is SyncOutcome.SUCCESS
    assert first.serving_safety is ServingSafety.SAFE_TO_SERVE
    assert first.ready_builds == 1
    async with postgres_uow_factory() as unit:
        documents = await unit.documents.list()
    assert len(documents) == 1
    document = documents[0]
    assert document.ingestion_status is IngestionStatus.READY
    assert document.current_revision_id is not None
    first_revision = document.current_revision_id
    assert await _all_rows(isolated_milvus.provider)

    second = await app.run(request(tmp_path))
    assert second.outcome is SyncOutcome.SUCCESS
    assert second.planned_builds == 0
    assert second.serving_safety is ServingSafety.SAFE_TO_SERVE

    source.write_text("second changed canonical content", encoding="utf-8")
    third = await app.run(request(tmp_path))
    assert third.outcome is SyncOutcome.SUCCESS
    assert third.ready_builds == 1
    async with postgres_uow_factory() as unit:
        changed = (await unit.documents.list())[0]
    assert changed.current_revision_id != first_revision
    assert changed.ingestion_status is IngestionStatus.READY

    moved = tmp_path / "archive" / "guide.txt"
    moved.parent.mkdir()
    source.rename(moved)
    fourth = await app.run(request(tmp_path))
    assert fourth.outcome is SyncOutcome.SUCCESS
    assert fourth.serving_safety is ServingSafety.SAFE_TO_SERVE
    assert fourth.deleted_documents == 1
    assert fourth.ready_builds == 1
    async with postgres_uow_factory() as unit:
        after_move = await unit.documents.list()
    assert len(after_move) == 1
    assert after_move[0].relative_source_path == "archive/guide.txt"

    moved.unlink()
    fifth = await app.run(request(tmp_path))
    assert fifth.outcome is SyncOutcome.SUCCESS
    assert fifth.deleted_documents == 1
    async with postgres_uow_factory() as unit:
        assert await unit.documents.list() == ()
        assert (await unit.runtime_metadata.get()).query_cache_invalidation_required
    assert await _all_rows(isolated_milvus.provider) == []


@pytest.mark.asyncio
async def test_current_index_missing_is_repaired_by_reconciliation(
    postgres_uow_factory: Factory,
    postgres_pool: PostgresPool,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    app = application(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    source = tmp_path / "guide.txt"
    source.write_text("repairable canonical content", encoding="utf-8")
    first = await app.run(request(tmp_path))
    assert first.outcome is SyncOutcome.SUCCESS, first.model_dump()
    async with postgres_uow_factory() as unit:
        document = (await unit.documents.list())[0]
    revision = document.current_revision_id
    assert revision is not None
    await isolated_milvus.provider.delete_revision(document.document_id, revision)
    assert await _all_rows(isolated_milvus.provider) == []

    repaired = await app.run(request(tmp_path))
    assert repaired.outcome is SyncOutcome.SUCCESS
    assert repaired.serving_safety is ServingSafety.SAFE_TO_SERVE
    assert await _all_rows(isolated_milvus.provider)


@pytest.mark.asyncio
async def test_unknown_searchable_orphan_is_cleaned_before_final_safety(
    postgres_uow_factory: Factory,
    postgres_pool: PostgresPool,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    from atlasrag.domain.ingestion.source import derive_document_id, derive_revision_id
    from atlasrag.providers.index.models import MilvusChunkRecord

    app = application(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    (tmp_path / "guide.txt").write_text("canonical content", encoding="utf-8")
    assert (await app.run(request(tmp_path))).outcome is SyncOutcome.SUCCESS
    document_id = derive_document_id("orphan.txt")
    revision_id = derive_revision_id(document_id, "a" * 64, "b" * 64)
    orphan = MilvusChunkRecord(
        chunk_id="chunk_" + "c" * 64,
        document_id=document_id,
        revision_id=revision_id,
        file_name="orphan.txt",
        parent_id=None,
        chunk_type=RetrievalChunkType.TABLE,
        source_type=SourceType.TXT,
        content="orphan",
        sparse_text="orphan",
        dense_vector=(1.0, 0.0, 0.0),
        structured_metadata={},
    )
    await isolated_milvus.provider.insert_revision_batch(
        [orphan], document_id=document_id, revision_id=revision_id
    )
    await isolated_milvus.provider.wait_revision_visible(document_id, revision_id)
    assert len(await _all_rows(isolated_milvus.provider)) == 2
    result = await app.run(request(tmp_path))
    assert result.outcome is SyncOutcome.SUCCESS
    assert result.serving_safety is ServingSafety.SAFE_TO_SERVE
    rows = await _all_rows(isolated_milvus.provider)
    assert orphan.chunk_id not in {row.chunk_id for row in rows}
