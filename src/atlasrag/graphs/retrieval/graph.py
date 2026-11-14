"""Frozen retrieval_graph wrapper: cache lookup around RagCore."""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from atlasrag.application.rag_core import RagCoreRunner
from atlasrag.graphs.state.retrieval import (
    RetrievalGraphInput,
    RetrievalGraphOutput,
    RetrievalGraphState,
)
from atlasrag.services.cache.session import SessionQueryCache


@dataclass(frozen=True)
class RetrievalGraphServices:
    cache: SessionQueryCache
    rag_core: RagCoreRunner


def build_retrieval_graph(
    services: RetrievalGraphServices,
) -> CompiledStateGraph[
    RetrievalGraphState, None, RetrievalGraphInput, RetrievalGraphOutput
]:
    async def cache_lookup(state: RetrievalGraphState) -> dict[str, object]:
        cached = await services.cache.lookup(state["request"])
        if cached is None:
            return {}
        return {"final_response": cached}

    def after_lookup(state: RetrievalGraphState) -> str:
        return "hit" if "final_response" in state else "miss"

    async def rag_core(state: RetrievalGraphState) -> dict[str, object]:
        result = await services.rag_core.run(state["request"])
        return {
            "rag_result": result,
            "final_response": result.final_response,
        }

    async def cache_write(state: RetrievalGraphState) -> dict[str, object]:
        result = state.get("rag_result")
        if result is None:
            raise ValueError("cache write requires a RagCoreResult")
        await services.cache.store(state["request"], result)
        return {}

    graph = StateGraph(
        RetrievalGraphState,
        input_schema=RetrievalGraphInput,
        output_schema=RetrievalGraphOutput,
    )
    graph.add_node("cache_lookup", cache_lookup)
    graph.add_node("rag_core", rag_core)
    graph.add_node("cache_write", cache_write)
    graph.add_edge(START, "cache_lookup")
    graph.add_conditional_edges(
        "cache_lookup", after_lookup, {"hit": END, "miss": "rag_core"}
    )
    graph.add_edge("rag_core", "cache_write")
    graph.add_edge("cache_write", END)
    return graph.compile()
