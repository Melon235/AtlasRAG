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
    services: RetrievalGraphServices, *, execution_facts: dict[str, bool] | None = None
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
        execution = await services.rag_core.run_with_facts(state["request"])
        if execution_facts is not None:
            execution_facts["web_invoked"] = execution.web_invoked
        return {
            "rag_result": execution.result,
            "final_response": execution.result.final_response,
        }

    async def cache_write(state: RetrievalGraphState) -> dict[str, object]:
        result = state.get("rag_result")
        if result is None:
            raise ValueError("cache write requires a RagCoreResult")
        web_invoked = bool((execution_facts or {}).get("web_invoked", False))
        await services.cache.store(state["request"], result, web_invoked=web_invoked)
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
