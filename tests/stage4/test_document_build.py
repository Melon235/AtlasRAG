"""Actual offline LangGraph orchestration and durable build capabilities."""

from __future__ import annotations

import importlib
import socket
from asyncio import CancelledError
from pathlib import Path
from typing import Any, cast

import pytest
from stage3.test_candidate_services import _context, _document

from atlasrag.domain.errors import SourceMutationError
from atlasrag.repositories.postgres.records import FailedStage

NODES = (
    "prepare_candidate",
    "parse_source",
    "persist_parsed",
    "build_chunks",
    "persist_chunked",
    "index_candidate",
    "verify_candidate",
    "publish_revision",
)


def module(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"missing T8 implementation: {name}", pytrace=False)


class Steps:
    def __init__(
        self, context: Any, *, at: str = "", error: BaseException | None = None
    ):
        self.context = context
        self.events: list[str] = []
        self.at = at
        self.error = error

    async def checkpoint(self) -> None:
        pass

    async def call(self, name: str) -> None:
        self.events.append(name)
        if name == self.at:
            assert self.error is not None
            raise self.error

    async def prepare_candidate(self) -> None:
        await self.call("prepare_candidate")

    async def parse_source(self) -> Any:
        await self.call("parse_source")
        return _document(self.context, "canonical input")

    async def persist_parsed(self, document: Any) -> None:
        await self.call("persist_parsed")

    async def build_chunks(self, document: Any) -> tuple[()]:
        await self.call("build_chunks")
        return ()

    async def persist_chunked(self, document: Any, chunks: Any) -> None:
        await self.call("persist_chunked")

    async def index_candidate(self) -> None:
        await self.call("index_candidate")

    async def verify_candidate(self) -> None:
        await self.call("verify_candidate")

    async def publish_revision(self) -> None:
        await self.call("publish_revision")

    async def record_failure(self, stage: FailedStage) -> None:
        await self.call("record_failure")

    async def discard_candidate(self) -> None:
        await self.call("discard_candidate")


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline build graph attempted network access")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


async def invoke(steps: Steps) -> dict[str, Any]:
    graph = module("atlasrag.graphs.indexing.document_build")
    compiled = graph.build_document_build_graph(steps)
    assert compiled.checkpointer is None
    return cast(dict[str, Any], await compiled.ainvoke({"context": steps.context}))


@pytest.mark.asyncio
async def test_exact_eight_nodes_and_minimal_output(tmp_path: Path) -> None:
    steps = Steps(_context(tmp_path))
    output = await invoke(steps)
    assert steps.events == list(NODES)
    assert set(output) == {"result"}
    assert output["result"].outcome == "READY"
    assert output["result"].document_id == steps.context.document_id
    assert output["result"].revision_id == steps.context.revision_id
    assert output["result"].failed_stage is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage,node",
    [
        ("PARSE", "parse_source"),
        ("CHUNK", "build_chunks"),
        ("REPRESENTATION", "index_candidate"),
        ("EMBEDDING", "index_candidate"),
        ("INDEX_WRITE", "index_candidate"),
        ("INDEX_VERIFY", "verify_candidate"),
    ],
)
async def test_business_failure_records_and_ends(
    tmp_path: Path, stage: str, node: str
) -> None:
    build = module("atlasrag.services.indexing.build")
    ports = module("atlasrag.services.indexing.ports")
    error = build.BuildBusinessFailure(
        FailedStage(stage), ports.BuildFailureCode(stage)
    )
    steps = Steps(_context(tmp_path), at=node, error=error)
    output = await invoke(steps)
    assert steps.events == [*NODES[: NODES.index(node) + 1], "record_failure"]
    assert output["result"].outcome == "FAILED"
    assert output["result"].failed_stage == stage
    assert output["result"].reason_code == stage


@pytest.mark.asyncio
async def test_mutation_discards_and_ends(tmp_path: Path) -> None:
    steps = Steps(_context(tmp_path), at="parse_source", error=SourceMutationError())
    output = await invoke(steps)
    assert steps.events == ["prepare_candidate", "parse_source", "discard_candidate"]
    assert output["result"].outcome == "SOURCE_MUTATED"


@pytest.mark.asyncio
@pytest.mark.parametrize("node", NODES)
@pytest.mark.parametrize(
    "error", [ValueError("private payload"), TimeoutError(), CancelledError()]
)
async def test_unclassified_errors_propagate_without_business_recording(
    tmp_path: Path, node: str, error: BaseException
) -> None:
    steps = Steps(_context(tmp_path), at=node, error=error)
    from langgraph.errors import NodeCancelledError

    expected = NodeCancelledError if isinstance(error, CancelledError) else type(error)
    with pytest.raises(expected) as caught:
        await invoke(steps)
    if isinstance(error, CancelledError):
        assert caught.value.__cause__ is error
    else:
        assert caught.value is error
    assert steps.events == list(NODES[: NODES.index(node) + 1])


def test_request_and_result_are_closed_immutable_contracts(tmp_path: Path) -> None:
    from pydantic import ValidationError

    contracts = module("atlasrag.graphs.indexing.contracts")
    context = _context(tmp_path)
    request = contracts.BuildRequest(
        source=context.source, expected_pipeline_fingerprint="f" * 64
    )
    with pytest.raises(ValidationError):
        request.expected_pipeline_fingerprint = "a" * 64
    with pytest.raises(ValidationError):
        contracts.BuildRequest(
            source=context.source, expected_pipeline_fingerprint="bad"
        )
    with pytest.raises(ValidationError):
        contracts.BuildResult(
            document_id=context.document_id,
            revision_id=context.revision_id,
            outcome="FAILED",
            failed_stage="PARSE",
            reason_code="secret",
        )
    with pytest.raises(ValidationError):
        contracts.BuildResult(
            document_id=context.document_id,
            revision_id=context.revision_id,
            outcome="FAILED",
        )


@pytest.mark.asyncio
async def test_services_publish_real_persisted_candidate(tmp_path: Path) -> None:
    from stage4.build_fakes import healthy, resources

    bundle, context, factory, _, index = resources(tmp_path)
    build = module("atlasrag.services.indexing.build")
    steps = build.DocumentBuildServices(bundle, context, checkpoint=healthy)
    graph = module(
        "atlasrag.graphs.indexing.document_build"
    ).build_document_build_graph(steps)
    states = [
        state
        async for state in graph.astream({"context": context}, stream_mode="values")
    ]
    assert states[-1]["result"].outcome == "READY"
    stored = factory.database.documents[context.document_id]
    assert stored.current_revision_id == context.revision_id
    assert stored.building_revision_id is None
    assert factory.database.query_cache_invalidation_required
    assert index.rows
    assert states[-1]["document"] is None and states[-1]["chunks"] is None
    assert set(states[-1]) <= {
        "context",
        "document",
        "chunks",
        "source_mutated",
        "failed_stage",
        "reason_code",
        "result",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["parse", "mutation", "embedding", "technical"])
async def test_services_controlled_failures_and_mutation(
    tmp_path: Path, failure: str
) -> None:
    from atlasrag.domain.indexing.failures import EmbeddingOutputError
    from stage4.build_fakes import healthy, resources

    bundle, context, factory, parser, _ = resources(tmp_path)
    error = ValueError("programming error with private data")
    if failure == "parse":
        parser.failed = True
    elif failure == "mutation":
        parser.mutate = True
    elif failure == "embedding":
        bundle.embedding.error = EmbeddingOutputError("private payload")
    else:
        parser.error = error
    build = module("atlasrag.services.indexing.build")
    steps = build.DocumentBuildServices(bundle, context, checkpoint=healthy)
    graph = module(
        "atlasrag.graphs.indexing.document_build"
    ).build_document_build_graph(steps)
    if failure == "technical":
        with pytest.raises(ValueError) as caught:
            await graph.ainvoke({"context": context})
        assert caught.value is error
        assert (
            factory.database.documents[context.document_id].ingestion_status.value
            == "PARSING"
        )
        return
    output = await graph.ainvoke({"context": context})
    result = output["result"]
    assert "private" not in result.model_dump_json()
    stored = factory.database.documents[context.document_id]
    if failure == "mutation":
        assert result.outcome == "SOURCE_MUTATED"
        assert stored.ingestion_status.value == "RECEIVED"
        assert stored.building_revision_id is None
    else:
        assert result.outcome == "FAILED"
        assert result.failed_stage.value == failure.upper()
        assert stored.ingestion_status.value == "FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["CHUNK", "REPRESENTATION", "INDEX_WRITE", "INDEX_VERIFY"]
)
async def test_services_classify_exact_lower_boundary_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from atlasrag.domain.indexing.failures import RepresentationInputLimitError
    from atlasrag.domain.ingestion.failures import ChunkInputLimitError
    from atlasrag.providers.index.projection import IndexWriteRejectedError
    from stage4.build_fakes import healthy, resources

    bundle, context, factory, _, index = resources(tmp_path)
    build = module("atlasrag.services.indexing.build")

    def chunk(*args: object) -> None:
        raise ChunkInputLimitError("private")

    def representation(*args: object) -> None:
        raise RepresentationInputLimitError(actual_bytes=100, max_bytes=1)

    async def rejected(*args: object, **kwargs: object) -> None:
        raise IndexWriteRejectedError()

    if failure == "CHUNK":
        monkeypatch.setattr(build, "build_chunks", chunk)
    elif failure == "REPRESENTATION":
        monkeypatch.setattr(
            module("atlasrag.services.indexing.representation").RepresentationBuilder,
            "build",
            representation,
        )
    elif failure == "INDEX_WRITE":
        monkeypatch.setattr(index, "insert_revision_batch", rejected)
    else:
        original = index.iter_revision_projection
        scans = 0

        async def corrupted(*args: Any, **kwargs: Any) -> Any:
            nonlocal scans
            scans += 1
            async for row in original(*args, **kwargs):
                if scans > 1:
                    yield row.model_copy(update={"content": "wrong but well-formed"})
                else:
                    yield row

        monkeypatch.setattr(index, "iter_revision_projection", corrupted)
    steps = build.DocumentBuildServices(bundle, context, checkpoint=healthy)
    graph = module(
        "atlasrag.graphs.indexing.document_build"
    ).build_document_build_graph(steps)
    output = await graph.ainvoke({"context": context})
    assert output["result"].outcome == "FAILED"
    assert output["result"].failed_stage.value == failure
    assert output["result"].reason_code.value == failure
    assert factory.database.documents[context.document_id].failed_stage.value == failure


@pytest.mark.asyncio
async def test_persisted_authority_is_reloaded_before_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlasrag.domain.errors import CanonicalDataError
    from stage4.build_fakes import healthy, resources

    bundle, context, factory, _, index = resources(tmp_path)
    build = module("atlasrag.services.indexing.build")
    steps = build.DocumentBuildServices(bundle, context, checkpoint=healthy)
    original = steps.persist_chunked

    async def damaged(document: Any, chunks: Any) -> None:
        await original(document, chunks)
        factory.database.chunks[(context.document_id, context.revision_id)] = ()

    monkeypatch.setattr(steps, "persist_chunked", damaged)
    graph = module(
        "atlasrag.graphs.indexing.document_build"
    ).build_document_build_graph(steps)
    with pytest.raises(CanonicalDataError):
        await graph.ainvoke({"context": context})
    assert "insert" not in index.events
    assert (
        factory.database.documents[context.document_id].ingestion_status.value
        != "FAILED"
    )


@pytest.mark.asyncio
async def test_warn_parse_remains_publishable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlasrag.domain.ingestion.document import DocumentContainer, ParseInfo
    from atlasrag.domain.ingestion.enums import ParseReliability
    from atlasrag.domain.ingestion.validation import ValidationOutcome
    from atlasrag.services.ingestion.validation import validate_parsed_document
    from stage4.build_fakes import healthy, resources

    bundle, context, factory, parser, _ = resources(tmp_path)
    document = _document(context, "usable partial parse")
    parse_info = ParseInfo.model_validate(
        {
            **document.parse_info.model_dump(),
            "reliability": ParseReliability.PARTIAL,
            "warning_codes": ("PARTIAL_SOURCE",),
        }
    )
    document = DocumentContainer.model_validate(
        {**document.model_dump(), "parse_info": parse_info}
    )
    assert validate_parsed_document(document).outcome is ValidationOutcome.WARN

    async def parse(source: Any, context: Any) -> Any:
        return document

    monkeypatch.setattr(parser, "parse", parse)
    steps = module("atlasrag.services.indexing.build").DocumentBuildServices(
        bundle, context, checkpoint=healthy
    )
    output = (
        await module("atlasrag.graphs.indexing.document_build")
        .build_document_build_graph(steps)
        .ainvoke({"context": context})
    )
    assert output["result"].outcome == "READY"
    assert (
        factory.database.documents[context.document_id].current_revision_id
        == context.revision_id
    )
