"""Thin local_evidence_subgraph nodes; all technical work lives in services."""

from __future__ import annotations

from collections.abc import Sequence
from time import monotonic

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from atlasrag.domain.observation import (
    ObservationStage,
    ObservedCandidate,
    ProductionObservation,
)
from atlasrag.domain.retrieval import RetrievalCandidate
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
    def _observe(
        stage: ObservationStage,
        status: str,
        *,
        candidates: tuple[ObservedCandidate, ...] = (),
        selected: tuple[str, ...] = (),
        latency_ms: float | None = None,
        degraded: bool = False,
    ) -> None:
        services.observer.observe(
            ProductionObservation(
                stage=stage,
                status=status,
                candidates=candidates,
                selected_identities=selected,
                latency_ms=latency_ms,
                degraded=degraded,
            )
        )

    def _candidates(
        values: Sequence[RetrievalCandidate],
    ) -> tuple[ObservedCandidate, ...]:
        return tuple(
            ObservedCandidate(
                identity=candidate.chunk_id,
                rank=index,
                document_id=candidate.document_id,
                score=(
                    candidate.scores.rerank_score
                    if candidate.scores.rerank_score is not None
                    else candidate.scores.fusion_score
                    if candidate.scores.fusion_score is not None
                    else candidate.scores.dense_score
                    if candidate.scores.dense_score is not None
                    else candidate.scores.bm25_score
                ),
            )
            for index, candidate in enumerate(values, start=1)
        )

    async def retrieve_candidates(state: LocalEvidenceState) -> dict[str, object]:
        started = monotonic()
        result = await services.retrieval.retrieve(state["request"])
        elapsed = (monotonic() - started) * 1000
        _observe(
            ObservationStage.DENSE,
            result.dense.status.value,
            candidates=_candidates(result.dense.candidates),
            latency_ms=elapsed,
            degraded=result.dense.status.value != "OK",
        )
        _observe(
            ObservationStage.BM25,
            result.bm25.status.value,
            candidates=_candidates(result.bm25.candidates),
            latency_ms=elapsed,
            degraded=result.bm25.status.value != "OK",
        )
        return {
            "dense_branch": result.dense,
            "bm25_branch": result.bm25,
        }

    async def fuse_candidates(state: LocalEvidenceState) -> dict[str, object]:
        started = monotonic()
        pool = fuse(
            state["dense_branch"],
            state["bm25_branch"],
            profile=services.profile,
        )
        _observe(
            ObservationStage.FUSION,
            "OK" if pool.candidates else "EMPTY",
            candidates=_candidates(pool.candidates),
            latency_ms=(monotonic() - started) * 1000,
        )
        return {"candidate_pool": pool}

    async def rerank_candidates(state: LocalEvidenceState) -> dict[str, object]:
        started = monotonic()
        pool = await services.reranker.rerank(
            state["candidate_pool"],
            original_query=state["request"].original_query,
        )
        _observe(
            ObservationStage.RERANKER,
            "DEGRADED" if pool.degraded else "OK",
            candidates=_candidates(pool.candidates),
            latency_ms=(monotonic() - started) * 1000,
            degraded=pool.degraded,
        )
        return {"candidate_pool": pool}

    async def select_evidence(state: LocalEvidenceState) -> dict[str, object]:
        refs = services.selector.select(state["candidate_pool"])
        _observe(
            ObservationStage.EVIDENCE,
            "OK",
            selected=tuple(ref.context_id for ref in refs),
        )
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
        _observe(
            ObservationStage.SUFFICIENCY,
            result.evidence_status.value if result.evidence_status else "UNAVAILABLE",
            selected=tuple(item.context_id for item in result.selected_evidence),
            degraded=result.execution_status.value != "OK",
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
