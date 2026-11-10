"""T9 real parser -> chunker -> Milvus -> verifier -> PostgreSQL runner gate.

Only embeddings and explicitly injected failures are test doubles. Existing
test_stage4_publish owns the 12 real-SQL publication/rollback cases separately.
"""

from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from atlasrag.domain.indexing.failures import (
    EmbeddingOutputError,
    RepresentationInputLimitError,
)
from atlasrag.domain.ingestion.chunks import ChunkType
from atlasrag.domain.ingestion.source import CandidateBuildContext, SourceFile
from atlasrag.providers.index.models import MilvusChunkRecord
from atlasrag.providers.index.projection import (
    IndexWriteRejectedError,
    MilvusRevisionProjection,
)
from atlasrag.providers.parsing.routing import ParserRouter
from atlasrag.repositories.postgres.pool import PostgresPool
from atlasrag.repositories.postgres.records import IngestionStatus
from atlasrag.services.indexing.build import DocumentBuildServices
from atlasrag.services.indexing.publication import publish_revision
from atlasrag.services.indexing.representation import RepresentationBuilder
from atlasrag.services.ingestion.identity import build_candidate_context
from atlasrag.services.ingestion.mapping import (
    load_document_parse_metadata_envelope,
    map_chunks_from_stored,
)
from integration.stage4_helpers import (
    Factory,
    FakeEmbedding,
    assert_exact_projection,
    assert_last_good,
    clear_marker,
    marker,
    projection,
    request,
    resources,
    runner,
    snapshot,
)
from integration.test_milvus import IsolatedMilvus
from integration.test_milvus import isolated_milvus as isolated_milvus
from integration.test_stage4_publish import FailingUnit

pytestmark = pytest.mark.integration

MARKDOWN = "# Inventory\n\nCanonical paragraph.\n\n| Item | Count |\n| --- | --- |\n| Alpha | 2 |\n| Beta | 3 |\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix,content",
    [("txt", "A" + " " * 4000 + "B"), ("md", MARKDOWN)],
    ids=["txt-whitespace", "markdown-table"],
)
async def test_real_runner_publishes_exact_canonical_records(
    postgres_uow_factory: Factory,
    postgres_pool: PostgresPool,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
    suffix: str,
    content: str,
) -> None:
    bundle = resources(postgres_uow_factory, isolated_milvus.provider)
    build_request = request(tmp_path, bundle, content, suffix)
    context = build_candidate_context(
        build_request.source, build_request.expected_pipeline_fingerprint
    )
    result = await runner(bundle, postgres_pool).build(build_request)
    assert result.outcome == "READY"
    value = await snapshot(
        postgres_uow_factory, isolated_milvus.provider, result.document_id
    )
    document = value.document
    assert document.ingestion_status is IngestionStatus.READY
    assert (
        document.current_revision_id,
        document.current_content_hash,
        document.current_pipeline_fingerprint,
    ) == (context.revision_id, context.content_hash, context.pipeline_fingerprint)
    assert (
        document.building_revision_id,
        document.building_content_hash,
        document.building_pipeline_fingerprint,
    ) == (None, None, None)
    envelope = load_document_parse_metadata_envelope(document)
    assert envelope.current is not None and envelope.building is None
    assert document.failed_stage is None
    assert await marker(postgres_uow_factory) is True
    assert_exact_projection(value)
    chunks = map_chunks_from_stored(value.chunks)
    if suffix == "txt":
        children = tuple(
            chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD
        )
        assert len(children) == 3
        assert children[1].raw_content == " " * 1500
        assert value.rows[children[1].chunk_id]["content"] == " " * 1500
        assert value.rows[children[1].chunk_id]["sparse_text"] == " " * 1500
        assert "".join(child.raw_content for child in children) == content
        assert isinstance(bundle.embedding, FakeEmbedding)
        assert (" " * 1500,) in bundle.embedding.inputs
    else:
        tables = tuple(chunk for chunk in chunks if chunk.chunk_type is ChunkType.TABLE)
        assert len(tables) == 1
        table = tables[0].table_slice
        assert table is not None
        assert table.headers == ("Item", "Count")
        assert table.rows == (("Alpha", "2"), ("Beta", "3"))
        assert (table.row_start, table.row_end, table.sheet_name) == (0, 2, None)
        assert (
            table.source_anchor is not None
            and table.source_anchor.heading == "Inventory"
        )
        metadata = value.rows[tables[0].chunk_id]["structured_metadata"]
        assert metadata["provenance"]["row_start"] == 0
        assert metadata["provenance"]["row_end"] == 2
        assert metadata["provenance"]["sheet_name"] is None
        assert metadata["source_anchor"]["heading"] == "Inventory"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        "representation",
        "embedding",
        "write",
        "verify",
        "technical_embed",
        "technical_write",
        "technical_verify",
        "publish",
    ],
)
async def test_r2_fault_preserves_real_r1_and_classifies_only_deterministic_failure(
    postgres_uow_factory: Factory,
    postgres_pool: PostgresPool,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    factory, index = postgres_uow_factory, isolated_milvus.provider
    bundle = resources(factory, index)
    build_request = request(tmp_path, bundle, "Original last-good source.")
    first = await runner(bundle, postgres_pool).build(build_request)
    before = await snapshot(factory, index, first.document_id)
    await clear_marker(factory)
    build_request.source.absolute_path.write_text(
        "R2 changed text " * 300, encoding="utf-8"
    )
    context = build_candidate_context(
        build_request.source, build_request.expected_pipeline_fingerprint
    )
    assert context.revision_id != first.revision_id
    expected_stage = {
        "representation": "REPRESENTATION",
        "embedding": "EMBEDDING",
        "write": "INDEX_WRITE",
        "verify": "INDEX_VERIFY",
    }.get(phase)
    original_insert = index.insert_revision_batch
    original_projection = index.iter_revision_projection
    original_verify = DocumentBuildServices.verify_candidate
    verification_reads: list[str] = []
    publication_entered: list[str] = []

    def rejected_representation(*args: Any, **kwargs: Any) -> Any:
        raise RepresentationInputLimitError(actual_bytes=2, max_bytes=1)

    async def faulty_insert(
        records: Sequence[MilvusChunkRecord], *, document_id: str, revision_id: str
    ) -> None:
        if phase == "write":
            raise IndexWriteRejectedError
        if phase == "technical_write":
            await original_insert(
                records, document_id=document_id, revision_id=revision_id
            )
            raise TimeoutError("injected write transport failure")
        # A reachable, actually persisted metadata mismatch must be INDEX_VERIFY.
        tampered = []
        for record in records:
            values = record.model_dump(mode="json")
            values["structured_metadata"]["section_path"] = ["tampered"]
            tampered.append(MilvusChunkRecord.model_validate(values))
        await original_insert(
            tampered, document_id=document_id, revision_id=revision_id
        )

    async def failed_projection(
        document_id: str, revision_id: str, *, batch_size: int
    ) -> AsyncGenerator[MilvusRevisionProjection, None]:
        async with aclosing(
            original_projection(document_id, revision_id, batch_size=batch_size)
        ) as rows:
            row = await anext(rows)
            verification_reads.append(row.chunk_id)
            yield row
            raise TimeoutError("injected verification transport failure")

    async def failing_formal_verify(services: DocumentBuildServices) -> None:
        # Restrict the injection to the actual formal verifier. T4's earlier
        # exact-set deletion scans and visibility checks remain real and intact.
        with monkeypatch.context() as scoped:
            scoped.setattr(index, "iter_revision_projection", failed_projection)
            await original_verify(services)

    async def failed_publish(*args: Any, **kwargs: Any) -> None:
        publication_entered.append(context.revision_id)
        # Existing real-SQL fault wrapper raises only AFTER document promotion
        # and marker SQL writes, so rollback is exercised through the runner.
        await publish_revision(
            lambda: FailingUnit(factory(), "marker"), *args[1:], **kwargs
        )

    if phase == "representation":
        monkeypatch.setattr(RepresentationBuilder, "build", rejected_representation)
    elif phase in {"embedding", "technical_embed"}:
        assert isinstance(bundle.embedding, FakeEmbedding)
        bundle.embedding.error = (
            EmbeddingOutputError("injected malformed output")
            if phase == "embedding"
            else TimeoutError("injected embedding transport failure")
        )
    elif phase in {"write", "technical_write", "verify"}:
        monkeypatch.setattr(index, "insert_revision_batch", faulty_insert)
    elif phase == "technical_verify":
        monkeypatch.setattr(
            DocumentBuildServices, "verify_candidate", failing_formal_verify
        )
    else:
        monkeypatch.setattr(
            "atlasrag.services.indexing.build.publish_revision", failed_publish
        )
    if expected_stage is not None:
        result = await runner(bundle, postgres_pool).build(build_request)
        assert result.outcome == "FAILED"
        assert (
            result.failed_stage is not None
            and result.failed_stage.value == expected_stage
        )
        assert (
            result.reason_code is not None
            and result.reason_code.value == expected_stage
        )
    else:
        with pytest.raises(TimeoutError, match="injected|after actual marker write"):
            await runner(bundle, postgres_pool).build(build_request)
    if phase == "technical_verify":
        assert len(verification_reads) == 1
    if phase == "publish":
        assert publication_entered == [context.revision_id]
    after = await assert_last_good(factory, index, before)
    assert after.building_revision_id == context.revision_id
    if expected_stage is not None:
        assert after.ingestion_status is IngestionStatus.FAILED
        assert (
            after.failed_stage is not None
            and after.failed_stage.value == expected_stage
        )
    else:
        assert after.ingestion_status is not IngestionStatus.FAILED
        assert after.failed_stage is None
    assert await marker(factory) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_source_mutation_discards_only_building_without_index_or_publish(
    postgres_uow_factory: Factory,
    postgres_pool: PostgresPool,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
    existing: bool,
) -> None:
    factory, index = postgres_uow_factory, isolated_milvus.provider
    bundle = resources(factory, index)
    build_request = request(tmp_path, bundle, "Original source.")
    before = None
    if existing:
        first = await runner(bundle, postgres_pool).build(build_request)
        before = await snapshot(factory, index, first.document_id)
    await clear_marker(factory)
    build_request.source.absolute_path.write_text(
        "Candidate before mutation.", encoding="utf-8"
    )
    parser = bundle.parsers.parser_for(build_request.source)

    class MutatingParser:
        async def parse(
            self, source: SourceFile, context: CandidateBuildContext
        ) -> Any:
            document = await parser.parse(source, context)
            source.absolute_path.write_text(
                "Source changed during parse.", encoding="utf-8"
            )
            return document

    mutating = MutatingParser()
    bundle = replace(
        bundle, parsers=ParserRouter(mutating, mutating, mutating, mutating)
    )
    assert isinstance(bundle.embedding, FakeEmbedding)
    calls = list(bundle.embedding.inputs)
    result = await runner(bundle, postgres_pool).build(build_request)
    assert result.outcome == "SOURCE_MUTATED" and result.failed_stage is None
    async with factory() as unit:
        after = await unit.documents.get_by_id(result.document_id)
        assert after is not None
        assert after.ingestion_status is (
            IngestionStatus.READY if existing else IngestionStatus.RECEIVED
        )
        assert (
            after.building_revision_id,
            after.building_content_hash,
            after.building_pipeline_fingerprint,
        ) == (None, None, None)
        assert after.failed_stage is None
        assert after.parse_metadata["building"] is None
        if not existing:
            assert (
                after.current_revision_id,
                after.current_content_hash,
                after.current_pipeline_fingerprint,
            ) == (None, None, None)
            assert after.parse_metadata["current"] is None
    assert bundle.embedding.inputs == calls
    # Projection is only possible after initialization when no earlier build ran.
    await index.initialize()
    assert await projection(index, result.document_id, result.revision_id) == {}
    if before is not None:
        await assert_last_good(factory, index, before)
    assert await marker(factory) is False


@pytest.mark.asyncio
async def test_partial_real_insert_retry_deletes_candidate_leftovers_and_rewrites_exact_set(
    postgres_uow_factory: Factory,
    postgres_pool: PostgresPool,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, index = postgres_uow_factory, isolated_milvus.provider
    bundle = resources(factory, index)
    build_request = request(tmp_path, bundle, "Published R1.")
    first = await runner(bundle, postgres_pool).build(build_request)
    before = await snapshot(factory, index, first.document_id)
    await clear_marker(factory)
    build_request.source.absolute_path.write_text(
        "A" + " " * 4000 + "B", encoding="utf-8"
    )
    context = build_candidate_context(
        build_request.source, build_request.expected_pipeline_fingerprint
    )
    insert = index.insert_revision_batch
    delete = index.delete_revision
    captured: list[MilvusChunkRecord] = []
    events: list[str] = []

    async def partial(
        records: Sequence[MilvusChunkRecord], *, document_id: str, revision_id: str
    ) -> None:
        captured.extend(records)
        await insert(records, document_id=document_id, revision_id=revision_id)
        raise TimeoutError("after first real insert")

    monkeypatch.setattr(index, "insert_revision_batch", partial)
    with pytest.raises(TimeoutError, match="first real insert"):
        await runner(bundle, postgres_pool).build(build_request)
    assert len(captured) == 1
    stale = MilvusChunkRecord.model_validate(
        {**captured[0].model_dump(mode="json"), "chunk_id": "owned-stale-extra"}
    )
    await insert(
        [stale], document_id=context.document_id, revision_id=context.revision_id
    )
    assert set(await projection(index, context.document_id, context.revision_id)) == {
        captured[0].chunk_id,
        stale.chunk_id,
    }
    after_failure = await assert_last_good(factory, index, before)
    assert after_failure.ingestion_status is not IngestionStatus.FAILED
    assert await marker(factory) is False

    async def counted_delete(document_id: str, revision_id: str) -> int:
        assert (document_id, revision_id) == (context.document_id, context.revision_id)
        result = await delete(document_id, revision_id)
        events.append("delete")
        assert await projection(index, document_id, revision_id) == {}
        return result

    async def counted_insert(
        records: Sequence[MilvusChunkRecord], *, document_id: str, revision_id: str
    ) -> None:
        assert events and events[0] == "delete"
        events.append("insert")
        await insert(records, document_id=document_id, revision_id=revision_id)

    monkeypatch.setattr(index, "delete_revision", counted_delete)
    monkeypatch.setattr(index, "insert_revision_batch", counted_insert)
    result = await runner(bundle, postgres_pool).build(build_request)
    assert result.outcome == "READY" and result.revision_id == context.revision_id
    value = await snapshot(factory, index, result.document_id)
    assert_exact_projection(value)
    assert len(value.rows) == 3 and stale.chunk_id not in value.rows
    assert events == ["delete", "insert", "insert", "insert"]
    # Promotion must not clean the former current revision in either store.
    async with factory() as unit:
        assert (
            await unit.elements.list_for_revision(first.document_id, first.revision_id)
            == before.elements
        )
        assert (
            await unit.chunks.list_for_revision(first.document_id, first.revision_id)
            == before.chunks
        )
    assert await projection(index, first.document_id, first.revision_id) == before.rows
    assert await marker(factory) is True
