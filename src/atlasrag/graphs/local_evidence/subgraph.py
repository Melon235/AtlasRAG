"""Thin local_evidence_subgraph nodes; all technical work lives in services."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from atlasrag.graphs.state.local_evidence import (
    LocalEvidenceState,
    LocalEvidenceSubgraphInput,
    LocalEvidenceSubgraphOutput,
)
from atlasrag.services.local_evidence.fusion import fuse
from atlasrag.services.local_evidence.service import LocalEvidenceServices


def build_local_evidence_subgraph(
    services: LocalEvidenceServices,
) -> CompiledStateGraph[
    LocalEvidenceState, None, LocalEvidenceSubgraphInput, LocalEvidenceSubgraphOutput
]:
    async def retrieve_candidates(state: LocalEvidenceState) -> dict[str, object]:
        result = await services.retrieval.retrieve(state["request"])
        return {
            "dense_branch": result.dense,
            "bm25_branch": result.bm25,
        }

    async def fuse_candidates(state: LocalEvidenceState) -> dict[str, object]:
        pool = fuse(
            state["dense_branch"],
            state["bm25_branch"],
            profile=services.profile,
        )
        return {"candidate_pool": pool}

    async def rerank_candidates(state: LocalEvidenceState) -> dict[str, object]:
        pool = await services.reranker.rerank(
            state["candidate_pool"],
            original_query=state["request"].original_query,
        )
        return {"candidate_pool": pool}

    async def select_evidence(state: LocalEvidenceState) -> dict[str, object]:
        refs = services.selector.select(state["candidate_pool"])
        return {"evidence_refs": refs}

    async def expand_context(state: LocalEvidenceState) -> dict[str, object]:
        evidence = await services.parent_context.expand(state["evidence_refs"])
        return {"local_evidence": evidence}

    async def assess_sufficiency(state: LocalEvidenceState) -> dict[str, object]:
        result = await services.sufficiency.assess(
            state["request"],
            state["candidate_pool"],
            state["local_evidence"],
        )
        return {"result": result}

    graph = StateGraph(
        LocalEvidenceState,
        input_schema=LocalEvidenceSubgraphInput,
        output_schema=LocalEvidenceSubgraphOutput,
    )
    graph.add_node("retrieve_candidates", retrieve_candidates)
    graph.add_node("fuse_candidates", fuse_candidates)
    graph.add_node("rerank_candidates", rerank_candidates)
    graph.add_node("select_evidence", select_evidence)
    graph.add_node("expand_context", expand_context)
    graph.add_node("assess_sufficiency", assess_sufficiency)
    graph.add_edge(START, "retrieve_candidates")
    graph.add_edge("retrieve_candidates", "fuse_candidates")
    graph.add_edge("fuse_candidates", "rerank_candidates")
    graph.add_edge("rerank_candidates", "select_evidence")
    graph.add_edge("select_evidence", "expand_context")
    graph.add_edge("expand_context", "assess_sufficiency")
    graph.add_edge("assess_sufficiency", END)
    return graph.compile()
