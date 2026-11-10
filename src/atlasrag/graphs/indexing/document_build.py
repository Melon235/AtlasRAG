"""Thin, explicitly injected eight-node Part I document lifecycle."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from atlasrag.domain.errors import InvariantViolationError, SourceMutationError
from atlasrag.graphs.indexing.contracts import BuildResult
from atlasrag.graphs.indexing.state import (
    DocumentBuildInput,
    DocumentBuildOutput,
    DocumentBuildState,
)
from atlasrag.services.indexing.build import BuildBusinessFailure
from atlasrag.services.indexing.ports import DocumentBuildSteps


class _BusinessNode(Protocol):
    def __call__(self, state: DocumentBuildState) -> Awaitable[DocumentBuildState]: ...


def build_document_build_graph(
    services: DocumentBuildSteps,
) -> CompiledStateGraph[
    DocumentBuildState, None, DocumentBuildInput, DocumentBuildOutput
]:
    """Compile fresh for one invocation; never attach a checkpointer or proof."""

    async def prepare(state: DocumentBuildState) -> DocumentBuildState:
        if state["context"] != services.context:
            raise InvariantViolationError
        await services.prepare_candidate()
        return {}

    async def parse(state: DocumentBuildState) -> DocumentBuildState:
        return {"document": await services.parse_source()}

    async def persist_parsed(state: DocumentBuildState) -> DocumentBuildState:
        document = state.get("document")
        if document is None:
            raise InvariantViolationError
        await services.persist_parsed(document)
        return {}

    async def chunk(state: DocumentBuildState) -> DocumentBuildState:
        document = state.get("document")
        if document is None:
            raise InvariantViolationError
        return {"chunks": await services.build_chunks(document)}

    async def persist_chunked(state: DocumentBuildState) -> DocumentBuildState:
        document, chunks = state.get("document"), state.get("chunks")
        if document is None or chunks is None:
            raise InvariantViolationError
        await services.persist_chunked(document, chunks)
        return {"document": None, "chunks": None}

    async def index(state: DocumentBuildState) -> DocumentBuildState:
        await services.index_candidate()
        return {}

    async def verify(state: DocumentBuildState) -> DocumentBuildState:
        await services.verify_candidate()
        return {}

    def result(state: DocumentBuildState, outcome: str) -> DocumentBuildState:
        context = state["context"]
        return {
            "document": None,
            "chunks": None,
            "result": BuildResult.model_validate(
                {
                    "document_id": context.document_id,
                    "revision_id": context.revision_id,
                    "outcome": outcome,
                    "failed_stage": state.get("failed_stage"),
                    "reason_code": state.get("reason_code"),
                }
            ),
        }

    async def publish(state: DocumentBuildState) -> DocumentBuildState:
        await services.publish_revision()
        return result(state, "READY")

    async def discard(state: DocumentBuildState) -> DocumentBuildState:
        await services.discard_candidate()
        return result(state, "SOURCE_MUTATED")

    async def record(state: DocumentBuildState) -> DocumentBuildState:
        stage = state.get("failed_stage")
        if stage is None:
            raise InvariantViolationError
        await services.record_failure(stage)
        return result(state, "FAILED")

    def guarded(
        operation: Callable[[DocumentBuildState], Awaitable[DocumentBuildState]],
        *,
        business: bool = False,
        mutation: bool = False,
    ) -> _BusinessNode:
        async def node(state: DocumentBuildState) -> DocumentBuildState:
            await services.checkpoint()
            try:
                update = await operation(state)
            except BuildBusinessFailure as error:
                if not business:
                    raise
                update = {"failed_stage": error.stage, "reason_code": error.reason}
            except SourceMutationError:
                if not mutation:
                    raise
                update = {"source_mutated": True}
            await services.checkpoint()
            return update

        return node

    def route(next_node: str) -> Callable[[DocumentBuildState], str]:
        def choose(state: DocumentBuildState) -> str:
            if state.get("source_mutated"):
                return "discard_candidate"
            if state.get("failed_stage") is not None:
                return "record_failure"
            return next_node

        return choose

    graph = StateGraph(
        DocumentBuildState,
        input_schema=DocumentBuildInput,
        output_schema=DocumentBuildOutput,
    )
    graph.add_node("prepare_candidate", guarded(prepare))
    graph.add_node("parse_source", guarded(parse, business=True, mutation=True))
    graph.add_node("persist_parsed", guarded(persist_parsed))
    graph.add_node("build_chunks", guarded(chunk, business=True))
    graph.add_node("persist_chunked", guarded(persist_chunked))
    graph.add_node("index_candidate", guarded(index, business=True))
    graph.add_node("verify_candidate", guarded(verify, business=True))
    graph.add_node("publish_revision", guarded(publish))
    graph.add_node("discard_candidate", guarded(discard))
    graph.add_node("record_failure", guarded(record))
    graph.add_edge(START, "prepare_candidate")
    graph.add_edge("prepare_candidate", "parse_source")
    for source, target in (
        ("parse_source", "persist_parsed"),
        ("build_chunks", "persist_chunked"),
        ("index_candidate", "verify_candidate"),
        ("verify_candidate", "publish_revision"),
    ):
        graph.add_conditional_edges(
            source, route(target), [target, "discard_candidate", "record_failure"]
        )
    graph.add_edge("persist_parsed", "build_chunks")
    graph.add_edge("persist_chunked", "index_candidate")
    for terminal in ("publish_revision", "discard_candidate", "record_failure"):
        graph.add_edge(terminal, END)
    return graph.compile()
