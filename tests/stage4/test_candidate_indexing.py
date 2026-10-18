"""PG authority, bounded indexing and exact candidate retry orchestration."""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError
from stage3.test_candidate_services import NOW

from atlasrag.domain.errors import (
    CanonicalDataError,
    ConfigurationError,
    InvariantViolationError,
)
from atlasrag.domain.indexing.failures import EmbeddingOutputError
from atlasrag.providers.index.projection import MilvusRevisionProjection
from atlasrag.repositories.postgres.records import IngestionStatus
from atlasrag.services.ingestion.mapping import map_chunks_from_stored
from stage4.candidate_fakes import Embedding, Index, candidate


def service() -> ModuleType:
    try:
        return importlib.import_module("atlasrag.services.indexing.candidate")
    except ModuleNotFoundError:
        pytest.fail("T4 persisted candidate indexing service is missing", pytrace=False)


@pytest.mark.asyncio
async def test_persisted_canonical_order_batches_and_short_transaction(
    tmp_path: Path,
) -> None:
    indexing = service()
    factory, context, config = candidate(tmp_path)
    embedding = Embedding(factory, config)
    index = Index(factory, embedding)
    original = factory.database.documents[context.document_id]
    await indexing.index_candidate(
        factory,
        context,
        config=config,
        embedding=embedding,
        index=index,
        clock=lambda: NOW,
        batch_size=3,
    )
    stored = factory.database.documents[context.document_id]
    assert stored.ingestion_status is IngestionStatus.INDEXING
    assert stored.current_revision_id == original.current_revision_id
    assert stored.parse_metadata == original.parse_metadata
    assert stored.updated_at == NOW
    assert factory.commit_count == 1
    assert len(factory.unit_of_works) == 1
    assert "elements.list_for_revision" in factory.calls
    assert "chunks.list_for_revision" in factory.calls
    chunks = map_chunks_from_stored(
        factory.database.chunks[(context.document_id, context.revision_id)]
    )
    expected = [chunk for chunk in chunks if chunk.parent_id is not None]
    assert [r.chunk_id for r in index.rows] == [chunk.chunk_id for chunk in expected]
    assert [len(batch) for batch in embedding.inputs] == [3, 3, 3, 1]
    assert [text for batch in embedding.inputs for text in batch] == [
        c.raw_content for c in expected
    ]
    assert index.events[0] == "initialize"
    assert index.events.index("scan") < index.events.index("insert")
    assert index.events[-1] == "visible"
    assert index.closed == 1
    for actual, chunk in zip(index.rows, expected, strict=True):
        assert actual.content == actual.sparse_text == chunk.raw_content
        assert actual.file_name == context.source.file_name
        assert actual.source_type == context.source.source_type.value
        assert actual.parent_id == chunk.parent_id
        assert actual.structured_metadata["schema_version"] == 1


def test_caller_chunks_are_not_an_input() -> None:
    signature = inspect.signature(service().index_candidate)
    assert "chunks" not in signature.parameters
    assert "document" not in signature.parameters
    assert all(
        p.kind is not inspect.Parameter.VAR_KEYWORD
        for p in signature.parameters.values()
    )
    with pytest.raises(TypeError):
        signature.bind(None, None, chunks=("forged",))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        "missing_parent",
        "chunk_metadata",
        "chunk_schema",
        "element_schema",
        "envelope_schema",
        "envelope_owner",
        "source_lineage",
        "missing_element",
    ],
)
async def test_persisted_corruption_rejected_before_index_mutation(
    tmp_path: Path, corruption: str
) -> None:
    indexing = service()
    factory, context, config = candidate(tmp_path)
    scope = context.document_id, context.revision_id
    chunks = factory.database.chunks[scope]
    if corruption == "missing_parent":
        factory.database.chunks[scope] = tuple(
            c for c in chunks if c.parent_id is not None
        )
    elif corruption in {"chunk_metadata", "chunk_schema"}:
        metadata = dict(chunks[0].metadata)
        metadata[
            "schema_version" if corruption == "chunk_schema" else "content_hash"
        ] = 2 if corruption == "chunk_schema" else "f" * 64
        factory.database.chunks[scope] = (
            chunks[0].model_copy(update={"metadata": metadata}),
            *chunks[1:],
        )
    elif corruption.startswith("envelope"):
        stored = factory.database.documents[context.document_id]
        metadata = stored.model_dump(mode="json")["parse_metadata"]
        if corruption == "envelope_schema":
            metadata["schema_version"] = 2
        else:
            metadata["building"]["revision_id"] = stored.current_revision_id
        factory.database.documents[context.document_id] = stored.model_copy(
            update={"parse_metadata": metadata}
        )
    elif corruption == "missing_element":
        factory.database.elements[scope] = ()
    else:
        element = factory.database.elements[scope][0]
        if corruption == "source_lineage":
            element = element.model_copy(update={"content": "different source"})
        else:
            metadata = dict(element.metadata)
            metadata["schema_version"] = 2
            element = element.model_copy(update={"metadata": metadata})
        factory.database.elements[scope] = (element,)
    embedding = Embedding(factory, config)
    index = Index(factory, embedding)
    with pytest.raises((CanonicalDataError, InvariantViolationError, ValidationError)):
        await indexing.index_candidate(
            factory,
            context,
            config=config,
            embedding=embedding,
            index=index,
            clock=lambda: NOW,
        )
    assert index.deleted == [] and index.rows == [] and embedding.inputs == []
    assert factory.commit_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("model_id", "wrong"),
        ("model_revision", "wrong"),
        ("tokenizer_id", "wrong"),
        ("tokenizer_revision", "wrong"),
        ("dimension", 4),
        ("device", "cpu"),
    ],
)
async def test_model_identity_mismatch_precedes_index_mutations(
    tmp_path: Path, field: str, value: object
) -> None:
    indexing = service()
    factory, context, config = candidate(tmp_path)
    embedding = Embedding(factory, config)
    embedding.identity = embedding.identity.model_copy(update={field: value})
    index = Index(factory, embedding)
    with pytest.raises(ConfigurationError):
        await indexing.index_candidate(
            factory,
            context,
            config=config,
            embedding=embedding,
            index=index,
            clock=lambda: NOW,
        )
    assert not index.deleted and not embedding.inputs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch", ["fingerprint", "dimension", "physical_schema", "representation"]
)
async def test_runtime_and_collection_compatibility_before_candidate_writes(
    tmp_path: Path, mismatch: str
) -> None:
    indexing = service()
    factory, context, config = candidate(tmp_path)
    embedding = Embedding(factory, config)
    index = Index(factory, embedding)
    if mismatch == "dimension":
        index.embedding_dimension = 4
    elif mismatch == "physical_schema":
        index.initialization_error = ConfigurationError()
    else:
        key = (
            "representation_version"
            if mismatch == "representation"
            else "parser_behavior_version"
        )
        config = config.model_copy(
            update={"part_i": config.part_i.model_copy(update={key: "different"})}
        )
    with pytest.raises(ConfigurationError):
        await indexing.index_candidate(
            factory,
            context,
            config=config,
            embedding=embedding,
            index=index,
            clock=lambda: NOW,
        )
    assert not index.deleted and not embedding.inputs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        IngestionStatus.RECEIVED,
        IngestionStatus.PARSING,
        IngestionStatus.PARSED,
        IngestionStatus.CHUNKING,
        IngestionStatus.READY,
        IngestionStatus.FAILED,
    ],
)
async def test_only_chunked_and_same_candidate_indexing_are_allowed(
    tmp_path: Path, status: IngestionStatus
) -> None:
    indexing = service()
    factory, context, config = candidate(tmp_path)
    original = factory.database.documents[context.document_id]
    factory.database.documents[context.document_id] = original.model_copy(
        update={"ingestion_status": status}
    )
    embedding = Embedding(factory, config)
    index = Index(factory, embedding)
    with pytest.raises((InvariantViolationError, CanonicalDataError, ValidationError)):
        await indexing.index_candidate(
            factory,
            context,
            config=config,
            embedding=embedding,
            index=index,
            clock=lambda: NOW,
        )
    assert not index.deleted and factory.commit_count == 0


@pytest.mark.asyncio
async def test_partial_four_of_ten_retry_replaces_r2_and_preserves_r1(
    tmp_path: Path,
) -> None:
    indexing = service()
    factory, context, config = candidate(tmp_path)
    embedding = Embedding(factory, config)
    index = Index(factory, embedding)
    original = factory.database.documents[context.document_id]
    assert original.current_revision_id is not None
    current_scope = context.document_id, original.current_revision_id
    current_elements = factory.database.elements[current_scope]
    current_chunks = factory.database.chunks[current_scope]
    r1 = MilvusRevisionProjection(
        chunk_id="published",
        document_id=context.document_id,
        revision_id=original.current_revision_id,
        parent_id="published-parent",
        chunk_type="TEXT_CHILD",
        source_type="TXT",
        file_name="old.txt",
        content="old",
        sparse_text="old",
        structured_metadata={"nested": {"full": [1, 2, 3]}},
    )
    stale = r1.model_copy(
        update={"chunk_id": "stale-extra", "revision_id": context.revision_id}
    )
    index.rows = [r1, stale]
    index.failure_after = 4
    with pytest.raises(TimeoutError):
        await indexing.index_candidate(
            factory,
            context,
            config=config,
            embedding=embedding,
            index=index,
            clock=lambda: NOW,
            batch_size=3,
        )
    assert len([r for r in index.rows if r.revision_id == context.revision_id]) == 4
    assert (
        factory.database.documents[context.document_id].ingestion_status
        is IngestionStatus.INDEXING
    )
    index.rows.append(stale)
    index.failure_after = None
    await indexing.index_candidate(
        factory,
        context,
        config=config,
        embedding=embedding,
        index=index,
        clock=lambda: NOW,
        batch_size=3,
    )
    candidate_rows = [r for r in index.rows if r.revision_id == context.revision_id]
    assert len(candidate_rows) == len({r.chunk_id for r in candidate_rows}) == 10
    assert stale not in candidate_rows
    assert [r for r in index.rows if r.revision_id == original.current_revision_id] == [
        r1
    ]
    assert index.deleted == [(context.document_id, context.revision_id)] * 2
    stored = factory.database.documents[context.document_id]
    assert stored.parse_metadata == original.parse_metadata
    assert stored.current_revision_id == original.current_revision_id
    assert stored.current_content_hash == original.current_content_hash
    assert stored.current_pipeline_fingerprint == original.current_pipeline_fingerprint
    assert stored.failed_stage is None
    assert factory.database.query_cache_invalidation_required is False
    assert factory.database.elements[current_scope] == current_elements
    assert factory.database.chunks[current_scope] == current_chunks


@pytest.mark.asyncio
async def test_delete_must_be_observably_empty_and_early_iterator_is_closed(
    tmp_path: Path,
) -> None:
    indexing = service()
    factory, context, config = candidate(tmp_path)
    embedding = Embedding(factory, config)
    index = Index(factory, embedding)
    index.delete_leaves_rows = True
    index.rows = [
        MilvusRevisionProjection(
            chunk_id="residual",
            document_id=context.document_id,
            revision_id=context.revision_id,
            parent_id=None,
            chunk_type="TEXT_PARENT",
            source_type="TXT",
            file_name="candidate.txt",
            content="residue",
            sparse_text="residue",
            structured_metadata={},
        )
    ]
    with pytest.raises(InvariantViolationError):
        await indexing.index_candidate(
            factory,
            context,
            config=config,
            embedding=embedding,
            index=index,
            clock=lambda: NOW,
        )
    assert index.closed == 1
    assert embedding.inputs == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [[], [[1.0]], [[float("nan"), 1.0, 0.0]], [[1.0, 1.0, 0.0], [1.0, 1.0, 0.0]]],
)
async def test_embedding_output_is_validated_again_before_insertion(
    tmp_path: Path, output: object
) -> None:
    indexing = service()
    factory, context, config = candidate(tmp_path)
    embedding = Embedding(factory, config)
    embedding.output = output
    index = Index(factory, embedding)
    with pytest.raises(EmbeddingOutputError):
        await indexing.index_candidate(
            factory,
            context,
            config=config,
            embedding=embedding,
            index=index,
            clock=lambda: NOW,
        )
    assert index.rows == []
    assert (
        factory.database.documents[context.document_id].ingestion_status
        is IngestionStatus.INDEXING
    )


def test_milvus_exposes_read_only_configured_dimension() -> None:
    from providers.test_milvus_provider import FakeMilvusClient, provider

    index = provider(FakeMilvusClient(), embedding_dimension=3)
    assert getattr(index, "embedding_dimension", None) == 3
    with pytest.raises(AttributeError):
        setattr(index, "embedding_dimension", 4)


def test_milvus_exposes_read_only_configured_analyzer() -> None:
    from providers.test_milvus_provider import FakeMilvusClient, provider

    index = provider(FakeMilvusClient())
    assert getattr(index, "analyzer", None) == "standard"
    with pytest.raises(AttributeError):
        setattr(index, "analyzer", "english")


@pytest.mark.asyncio
async def test_uniform_wrong_persisted_chunker_version_is_rejected(
    tmp_path: Path,
) -> None:
    indexing = service()
    factory, context, config = candidate(tmp_path)
    scope = context.document_id, context.revision_id
    factory.database.chunks[scope] = tuple(
        chunk.model_copy(
            update={
                "strategy_metadata": {
                    **chunk.strategy_metadata,
                    "chunker_behavior_version": "wrong-uniform-version",
                }
            }
        )
        for chunk in factory.database.chunks[scope]
    )
    # Existing reconstruction accepts consistent versions. T4 must bind runtime semantics.
    assert map_chunks_from_stored(factory.database.chunks[scope])
    embedding = Embedding(factory, config)
    index = Index(factory, embedding)
    with pytest.raises(CanonicalDataError):
        await indexing.index_candidate(
            factory,
            context,
            config=config,
            embedding=embedding,
            index=index,
            clock=lambda: NOW,
        )
    assert index.deleted == [] and factory.commit_count == 0


@pytest.mark.asyncio
async def test_index_analyzer_matches_runtime_before_candidate_writes(
    tmp_path: Path,
) -> None:
    indexing = service()
    factory, context, config = candidate(tmp_path)
    embedding = Embedding(factory, config)
    index = Index(factory, embedding)
    index.analyzer = "english"
    with pytest.raises(ConfigurationError):
        await indexing.index_candidate(
            factory,
            context,
            config=config,
            embedding=embedding,
            index=index,
            clock=lambda: NOW,
        )
    assert index.deleted == [] and factory.commit_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"building_revision_id": "rev_" + "f" * 64},
        {"building_content_hash": "f" * 64},
        {"building_pipeline_fingerprint": "f" * 64},
        {"observed_content_hash": "f" * 64},
        {"file_name": "different.txt"},
        {"relative_source_path": "different/candidate.txt"},
        {"source_type": "PDF"},
        {"current_revision_id": "same-candidate"},
    ],
)
async def test_persisted_candidate_ownership_must_match_context(
    tmp_path: Path, change: dict[str, object]
) -> None:
    indexing = service()
    factory, context, config = candidate(tmp_path)
    original = factory.database.documents[context.document_id]
    if change.get("current_revision_id") == "same-candidate":
        change = {
            "current_revision_id": context.revision_id,
            "current_content_hash": context.content_hash,
            "current_pipeline_fingerprint": context.pipeline_fingerprint,
        }
    factory.database.documents[context.document_id] = original.model_copy(update=change)
    embedding = Embedding(factory, config)
    index = Index(factory, embedding)
    with pytest.raises((InvariantViolationError, CanonicalDataError, ValidationError)):
        await indexing.index_candidate(
            factory,
            context,
            config=config,
            embedding=embedding,
            index=index,
            clock=lambda: NOW,
        )
    assert index.deleted == [] and embedding.inputs == []
    assert factory.commit_count == 0


@pytest.mark.asyncio
async def test_self_consistent_forged_chunks_rejected_against_actual_pg_source(
    tmp_path: Path,
) -> None:
    from stage3.test_candidate_services import _document

    from atlasrag.services.ingestion.chunking import ChunkingProfile, build_chunks
    from atlasrag.services.ingestion.mapping import map_chunks_to_stored

    indexing = service()
    factory, context, config = candidate(tmp_path)
    forged = build_chunks(_document(context, "entirely fabricated content"))
    stored = map_chunks_to_stored(
        forged,
        chunker_behavior_version=config.part_i.chunker_behavior_version,
        profile_fingerprint=ChunkingProfile().profile_fingerprint,
        created_at=NOW,
    )
    assert map_chunks_from_stored(stored) == forged
    factory.database.chunks[(context.document_id, context.revision_id)] = stored
    embedding = Embedding(factory, config)
    index = Index(factory, embedding)
    with pytest.raises(CanonicalDataError):
        await indexing.index_candidate(
            factory,
            context,
            config=config,
            embedding=embedding,
            index=index,
            clock=lambda: NOW,
        )
    assert index.deleted == [] and embedding.inputs == []
    assert factory.commit_count == 0


@pytest.mark.asyncio
async def test_actual_milvus_schema_is_checked_even_when_claimed_dimension_matches(
    tmp_path: Path,
) -> None:
    from providers.test_milvus_provider import (
        FakeMilvusClient,
        call_names,
        collection_description,
        provider,
    )

    indexing = service()
    factory, context, config = candidate(tmp_path)
    embedding = Embedding(factory, config)
    client = FakeMilvusClient()
    client.collection_description = collection_description(embedding_dimension=4)
    index = provider(client, embedding_dimension=3)
    with pytest.raises(ConfigurationError):
        await indexing.index_candidate(
            factory,
            context,
            config=config,
            embedding=embedding,
            index=index,
            clock=lambda: NOW,
        )
    assert "describe_collection" in call_names(client)
    assert "delete" not in call_names(client) and "insert" not in call_names(client)
    assert embedding.inputs == []


@pytest.mark.asyncio
async def test_commit_failure_prevents_all_external_index_work(tmp_path: Path) -> None:
    indexing = service()
    factory, context, config = candidate(tmp_path)
    original = factory.database.documents[context.document_id]
    factory.fail_after("uow.commit", TimeoutError("commit failed"))
    embedding = Embedding(factory, config)
    index = Index(factory, embedding)
    with pytest.raises(TimeoutError):
        await indexing.index_candidate(
            factory,
            context,
            config=config,
            embedding=embedding,
            index=index,
            clock=lambda: NOW,
        )
    assert factory.database.documents[context.document_id] == original
    assert index.events == [] and embedding.inputs == []
    assert factory.rollback_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("device", ["cuda", "cuda:0", "cuda:1", "cpu"])
async def test_explicit_device_identity_and_cuda_resolution_are_accepted(
    tmp_path: Path, device: str
) -> None:
    indexing = service()
    factory, context, config = candidate(tmp_path)
    config = config.model_copy(
        update={"models": config.models.model_copy(update={"model_device": device})}
    )
    embedding = Embedding(factory, config)
    embedding.identity = embedding.identity.model_copy(
        update={"device": "cuda:0" if device == "cuda" else device}
    )
    index = Index(factory, embedding)
    await indexing.index_candidate(
        factory,
        context,
        config=config,
        embedding=embedding,
        index=index,
        clock=lambda: NOW,
    )
    assert len(index.rows) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("offline"),
        RuntimeError("program bug"),
        ValueError("ordinary error"),
    ],
)
async def test_ordinary_embedding_errors_propagate_without_business_failed(
    tmp_path: Path, error: BaseException
) -> None:
    indexing = service()
    factory, context, config = candidate(tmp_path)
    embedding = Embedding(factory, config)
    embedding.error = error
    index = Index(factory, embedding)
    with pytest.raises(type(error)) as caught:
        await indexing.index_candidate(
            factory,
            context,
            config=config,
            embedding=embedding,
            index=index,
            clock=lambda: NOW,
        )
    assert caught.value is error
    stored = factory.database.documents[context.document_id]
    assert stored.ingestion_status is IngestionStatus.INDEXING
    assert stored.failed_stage is None


def test_explicit_table_mapping_preserves_headers_rows_sheet_and_anchor() -> None:
    from stage3.test_table_chunking import document, table_data, table_element

    from atlasrag.domain.indexing.models import RepresentationProfileV1
    from atlasrag.services.indexing.records import (
        build_retrieval_record,
        map_retrieval_record_to_milvus,
    )
    from atlasrag.services.indexing.representation import RepresentationBuilder
    from atlasrag.services.ingestion.chunking import build_chunks

    data = table_data()
    canonical = document((table_element(data),))
    (chunk,) = build_chunks(canonical)
    representation = RepresentationBuilder(
        RepresentationProfileV1(), representation_version="representation-v1"
    ).build(chunk)
    record = build_retrieval_record(canonical, chunk, representation, (1.0, 0.0, 0.0))
    indexed = map_retrieval_record_to_milvus(record)
    payload = indexed.model_dump(mode="json")
    assert payload["chunk_type"] == "TABLE" and payload["parent_id"] is None
    assert payload["source_type"] == "XLSX" and payload["file_name"] == "report.xlsx"
    assert payload["content"] == payload["sparse_text"] == chunk.raw_content
    assert all(header in payload["content"] for header in data.headers)
    assert all(cell in payload["content"] for row in data.rows for cell in row)
    assert payload[
        "structured_metadata"
    ] == representation.structured_metadata.model_dump(mode="json")
    metadata = payload["structured_metadata"]
    assert metadata["schema_version"] == 1
    assert metadata["section_path"] == ["Metrics"]
    assert metadata["provenance"]["sheet_name"] == "Metrics"
    assert metadata["provenance"]["row_start"] == 0
    assert metadata["provenance"]["row_end"] == 4
    assert (
        metadata["provenance"]["source_element_id"] == canonical.elements[0].element_id
    )
    assert metadata["source_anchor"]["cell_range"] == "A1:B5"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["representation", "embedding", "write"])
async def test_classified_exceptions_propagate_for_later_failure_recorder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    from atlasrag.domain.indexing.failures import (
        EmbeddingInputLimitError,
        RepresentationInputLimitError,
    )
    from atlasrag.providers.index.projection import IndexWriteRejectedError
    from atlasrag.services.indexing.representation import RepresentationBuilder

    indexing = service()
    factory, context, config = candidate(tmp_path)
    embedding = Embedding(factory, config)
    index = Index(factory, embedding)
    error: BaseException
    if stage == "representation":
        error = RepresentationInputLimitError(actual_bytes=100, max_bytes=99)

        def reject(*args: object, **kwargs: object) -> None:
            raise error

        monkeypatch.setattr(RepresentationBuilder, "build", reject)
    elif stage == "embedding":
        error = EmbeddingInputLimitError(actual_tokens=100, max_tokens=99)
        embedding.error = error
    else:
        error = IndexWriteRejectedError()

        async def reject_write(*args: object, **kwargs: object) -> None:
            raise error

        monkeypatch.setattr(index, "insert_revision_batch", reject_write)
    with pytest.raises(type(error)) as caught:
        await indexing.index_candidate(
            factory,
            context,
            config=config,
            embedding=embedding,
            index=index,
            clock=lambda: NOW,
        )
    assert caught.value is error
    stored = factory.database.documents[context.document_id]
    assert stored.ingestion_status is IngestionStatus.INDEXING
    assert stored.failed_stage is None


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", [0, -1, True, 16_385])
async def test_invalid_batch_bounds_fail_before_work(
    tmp_path: Path, batch_size: int
) -> None:
    indexing = service()
    factory, context, config = candidate(tmp_path)
    embedding = Embedding(factory, config)
    index = Index(factory, embedding)
    with pytest.raises(ConfigurationError):
        await indexing.index_candidate(
            factory,
            context,
            config=config,
            embedding=embedding,
            index=index,
            clock=lambda: NOW,
            batch_size=batch_size,
        )
    assert not factory.unit_of_works and not embedding.inputs and not index.events
