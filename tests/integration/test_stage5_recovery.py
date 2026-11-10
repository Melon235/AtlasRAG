"""Real PostgreSQL/Milvus interrupted-candidate and residue recovery."""

from __future__ import annotations

from contextlib import aclosing
from pathlib import Path
from typing import Any

import pytest

from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.domain.indexing.models import RepresentationProfileV1
from atlasrag.domain.ingestion.chunks import RetrievalRole
from atlasrag.repositories.postgres.records import IngestionStatus
from atlasrag.services.indexing.records import (
    build_retrieval_record,
    map_retrieval_record_to_milvus,
)
from atlasrag.services.indexing.representation import RepresentationBuilder
from atlasrag.services.ingestion import persistence
from atlasrag.services.ingestion.chunking import build_chunks
from atlasrag.services.ingestion.identity import build_candidate_context
from atlasrag.services.sync.models import ServingSafety, SyncOutcome
from integration.stage4_helpers import Factory, FakeEmbedding, resources
from integration.stage5_helpers import application, request, runtime_config
from integration.test_milvus import IsolatedMilvus
from integration.test_milvus import isolated_milvus as isolated_milvus

pytestmark = pytest.mark.integration


async def _candidate_to_chunked(
    factory: Factory,
    index: Any,
    root: Path,
    *,
    content: str,
    stop_at: str = "CHUNKED",
) -> Any:
    from stage3.test_candidate_services import NOW

    runtime = runtime_config()
    path = root / "guide.txt"
    path.write_text(content, encoding="utf-8")
    from atlasrag.domain.ingestion.source import SourceFile

    source = SourceFile.from_knowledge_directory(root, "guide.txt")
    context = build_candidate_context(source, pipeline_fingerprint(runtime))
    bundle = resources(factory, index)
    await persistence.prepare_candidate(factory, context, clock=lambda: NOW)
    await persistence.mark_parsing(factory, context, clock=lambda: NOW)
    document = await bundle.parsers.parser_for(source).parse(source, context)
    await persistence.persist_parsed(factory, context, document, clock=lambda: NOW)
    if stop_at == "PARSED":
        return context, document
    await persistence.mark_chunking(factory, context, clock=lambda: NOW)
    chunks = build_chunks(document, profile=bundle.chunking_profile)
    await persistence.persist_chunked(
        factory,
        context,
        document,
        chunks,
        chunker_behavior_version=runtime.part_i.chunker_behavior_version,
        profile_fingerprint=bundle.chunking_profile.profile_fingerprint,
        clock=lambda: NOW,
    )
    return context, document


async def _all_rows(index: Any) -> list[Any]:
    async with aclosing(index.iter_all_projection(batch_size=100)) as rows:
        return [row async for row in rows]


@pytest.mark.asyncio
async def test_chunked_candidate_resumes_without_parser(
    postgres_uow_factory: Factory,
    postgres_pool: Any,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    app = application(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    (tmp_path / "guide.txt").write_text("first content", encoding="utf-8")
    assert (await app.run(request(tmp_path))).outcome is SyncOutcome.SUCCESS
    async with postgres_uow_factory() as unit:
        first = (await unit.documents.list())[0]
    first_revision = first.current_revision_id
    context, _ = await _candidate_to_chunked(
        postgres_uow_factory,
        isolated_milvus.provider,
        tmp_path,
        content="second content",
    )
    async with postgres_uow_factory() as unit:
        interrupted = (await unit.documents.list())[0]
    assert interrupted.ingestion_status is IngestionStatus.CHUNKED
    assert interrupted.current_revision_id == first_revision
    result = await app.run(request(tmp_path))
    assert result.outcome is SyncOutcome.SUCCESS
    assert result.serving_safety is ServingSafety.SAFE_TO_SERVE
    async with postgres_uow_factory() as unit:
        after = (await unit.documents.list())[0]
    assert after.current_revision_id == context.revision_id
    assert after.ingestion_status is IngestionStatus.READY
    assert after.building_revision_id is None
    rows = await _all_rows(isolated_milvus.provider)
    assert rows
    assert {row.revision_id for row in rows} == {context.revision_id}


@pytest.mark.asyncio
async def test_indexing_partial_residue_is_deleted_then_reindexed(
    postgres_uow_factory: Factory,
    postgres_pool: Any,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    app = application(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    (tmp_path / "guide.txt").write_text("first content", encoding="utf-8")
    assert (await app.run(request(tmp_path))).outcome is SyncOutcome.SUCCESS
    context, document = await _candidate_to_chunked(
        postgres_uow_factory,
        isolated_milvus.provider,
        tmp_path,
        content="second content",
    )
    # Simulate a crash after one candidate batch landed in Milvus.
    await isolated_milvus.provider.initialize()
    chunk = next(
        chunk
        for chunk in build_chunks(
            document,
            profile=resources(
                postgres_uow_factory, isolated_milvus.provider
            ).chunking_profile,
        )
        if chunk.retrieval_role is RetrievalRole.RETRIEVABLE
    )
    representation = RepresentationBuilder(
        RepresentationProfileV1(), representation_version="representation-v1"
    ).build(chunk)
    embedding = FakeEmbedding(runtime_config())
    vector = (await embedding.embed((representation.dense_text,)))[0]
    record = map_retrieval_record_to_milvus(
        build_retrieval_record(document, chunk, representation, vector)
    )
    await isolated_milvus.provider.insert_revision_batch(
        [record], document_id=context.document_id, revision_id=context.revision_id
    )
    await isolated_milvus.provider.wait_revision_visible(
        context.document_id, context.revision_id
    )
    async with postgres_uow_factory() as unit:
        stored = await unit.documents.get_by_id(context.document_id)
        assert stored is not None
        await unit.documents.put(
            stored.model_copy(update={"ingestion_status": IngestionStatus.INDEXING})
        )
        await unit.commit()
    result = await app.run(request(tmp_path))
    assert result.outcome is SyncOutcome.SUCCESS
    assert result.serving_safety is ServingSafety.SAFE_TO_SERVE
    async with postgres_uow_factory() as unit:
        after = (await unit.documents.list())[0]
    assert after.current_revision_id == context.revision_id
    rows = await _all_rows(isolated_milvus.provider)
    assert {row.revision_id for row in rows} == {context.revision_id}


@pytest.mark.asyncio
async def test_parsed_candidate_rechunks_without_parser(
    postgres_uow_factory: Factory,
    postgres_pool: Any,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    app = application(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    (tmp_path / "guide.txt").write_text("first parsed content", encoding="utf-8")
    assert (await app.run(request(tmp_path))).outcome is SyncOutcome.SUCCESS
    context, _ = await _candidate_to_chunked(
        postgres_uow_factory,
        isolated_milvus.provider,
        tmp_path,
        content="second parsed content",
        stop_at="PARSED",
    )
    async with postgres_uow_factory() as unit:
        interrupted = (await unit.documents.list())[0]
    assert interrupted.ingestion_status is IngestionStatus.PARSED
    result = await app.run(request(tmp_path))
    assert result.outcome is SyncOutcome.SUCCESS
    assert result.serving_safety is ServingSafety.SAFE_TO_SERVE
    async with postgres_uow_factory() as unit:
        after = (await unit.documents.list())[0]
    assert after.current_revision_id == context.revision_id
    assert after.ingestion_status is IngestionStatus.READY


class FailingDeleteIndex:
    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    async def delete_document(self, document_id: str) -> int:
        from atlasrag.domain.errors import DependencyUnavailableError

        raise DependencyUnavailableError


@pytest.mark.asyncio
async def test_failed_required_deletion_is_unsafe_and_preserves_state(
    postgres_uow_factory: Factory,
    postgres_pool: Any,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    failing = FailingDeleteIndex(isolated_milvus.provider)
    app = application(postgres_uow_factory, postgres_pool, failing, tmp_path)
    source = tmp_path / "guide.txt"
    source.write_text("deletion must fail", encoding="utf-8")
    assert (await app.run(request(tmp_path))).outcome is SyncOutcome.SUCCESS
    source.unlink()
    result = await app.run(request(tmp_path))
    assert result.outcome is SyncOutcome.FAILED
    assert result.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "REQUIRED_DELETION_FAILED" in result.unsafe_reasons
    async with postgres_uow_factory() as unit:
        documents = await unit.documents.list()
    assert len(documents) == 1
    assert documents[0].ingestion_status is IngestionStatus.READY
    assert await _all_rows(isolated_milvus.provider)
