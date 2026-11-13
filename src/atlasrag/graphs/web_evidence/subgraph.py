"""Thin three-node web_evidence_subgraph."""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from atlasrag.domain.enums import WebExecutionStatus
from atlasrag.domain.errors import DependencyError
from atlasrag.domain.web import WebEvidenceResult
from atlasrag.graphs.state.web_evidence import (
    WebEvidenceState,
    WebEvidenceSubgraphInput,
    WebEvidenceSubgraphOutput,
)
from atlasrag.services.web_evidence.ports import SearchProvider
from atlasrag.services.web_evidence.profile import WebEvidenceProfile
from atlasrag.services.web_evidence.service import (
    WebEvidenceStrategy,
    WebPreparationService,
    preparation_degraded,
)


@dataclass(frozen=True)
class WebEvidenceServices:
    search: SearchProvider
    preparation: WebPreparationService
    strategy: WebEvidenceStrategy
    profile: WebEvidenceProfile


def build_web_evidence_subgraph(
    services: WebEvidenceServices,
) -> CompiledStateGraph[
    WebEvidenceState, None, WebEvidenceSubgraphInput, WebEvidenceSubgraphOutput
]:
    async def search_web(state: WebEvidenceState) -> dict[str, object]:
        try:
            results = await services.search.search(state["request"].original_query)
        except DependencyError:
            return {
                "search_results": (),
                "result": WebEvidenceResult(
                    execution_status=WebExecutionStatus.UNAVAILABLE, evidence=()
                ),
            }
        if not results:
            return {
                "search_results": (),
                "result": WebEvidenceResult(
                    execution_status=WebExecutionStatus.EMPTY, evidence=()
                ),
            }
        return {"search_results": results}

    async def prepare_web_sources(state: WebEvidenceState) -> dict[str, object]:
        if "result" in state:
            return {}
        outcome = await services.preparation.prepare(state["search_results"])
        if not outcome.sources:
            return {
                "prepared_sources": (),
                "result": WebEvidenceResult(
                    execution_status=WebExecutionStatus.EMPTY, evidence=()
                ),
            }
        return {"prepared_sources": outcome.sources}

    async def build_web_evidence(state: WebEvidenceState) -> dict[str, object]:
        if "result" in state:
            return {}
        sources = state["prepared_sources"]
        degraded = preparation_degraded(
            state["search_results"],
            sources,
            total_budget=services.profile.total_content_chars,
        )
        result = await services.strategy.build(
            state["request"], sources, degraded=degraded
        )
        return {"result": result}

    graph = StateGraph(
        WebEvidenceState,
        input_schema=WebEvidenceSubgraphInput,
        output_schema=WebEvidenceSubgraphOutput,
    )
    graph.add_node("search_web", search_web)
    graph.add_node("prepare_web_sources", prepare_web_sources)
    graph.add_node("build_web_evidence", build_web_evidence)
    graph.add_edge(START, "search_web")
    graph.add_edge("search_web", "prepare_web_sources")
    graph.add_edge("prepare_web_sources", "build_web_evidence")
    graph.add_edge("build_web_evidence", END)
    return graph.compile()
