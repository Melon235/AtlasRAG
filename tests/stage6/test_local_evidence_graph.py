"""Thin graph topology and business-output tests."""

from __future__ import annotations

from typing import Any, cast

import pytest

from atlasrag.application.local_evidence import LocalEvidenceRunner
from atlasrag.domain.enums import (
    CandidateOrdering,
    EvidenceType,
    LocalEvidenceStatus,
    LocalExecutionStatus,
    RetrievalBranch,
    RetrievalBranchStatus,
    RetrievalChunkType,
    RetrievalMode,
)
from atlasrag.domain.evidence import (
    EvidenceRef,
    LocalEvidence,
    LocalEvidenceResult,
    LocalProvenance,
)
from atlasrag.domain.requests import LocalRetrievalRequest
from atlasrag.domain.retrieval import (
    CandidatePool,
    CandidateScores,
    RetrievalBranchResult,
    RetrievalCandidate,
)
from atlasrag.services.local_evidence.profile import LocalEvidenceProfile
from atlasrag.services.local_evidence.retrieval import HybridRetrieval
from atlasrag.services.local_evidence.service import LocalEvidenceServices

CANDIDATE = RetrievalCandidate(
    chunk_id="chunk_" + "c" * 64,
    document_id="doc_" + "a" * 64,
    revision_id="rev_" + "b" * 64,
    chunk_type=RetrievalChunkType.TEXT_CHILD,
    parent_id="chunk_" + "d" * 64,
    retrieval_text="child text",
    scores=CandidateScores(dense_score=0.8, bm25_score=1.0),
)
REF = EvidenceRef(
    evidence_type=EvidenceType.TEXT_PARENT,
    context_id=CANDIDATE.parent_id or "",
    document_id=CANDIDATE.document_id,
    revision_id=CANDIDATE.revision_id,
)
EVIDENCE = LocalEvidence(
    evidence_type=EvidenceType.TEXT_PARENT,
    context_id=REF.context_id,
    document_id=REF.document_id,
    revision_id=REF.revision_id,
    content="complete canonical parent text",
    provenance=LocalProvenance(file_name="a.md", relative_source_path="a.md"),
)


class FakeRetrieval:
    def __init__(self) -> None:
        self.calls = 0

    async def retrieve(self, request: LocalRetrievalRequest) -> HybridRetrieval:
        self.calls += 1
        return HybridRetrieval(
            dense=RetrievalBranchResult(
                branch=RetrievalBranch.DENSE,
                status=RetrievalBranchStatus.OK,
                candidates=(CANDIDATE,),
            ),
            bm25=RetrievalBranchResult(
                branch=RetrievalBranch.BM25,
                status=RetrievalBranchStatus.OK,
                candidates=(CANDIDATE,),
            ),
        )


class FakeReranker:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def rerank(
        self, pool: CandidatePool, *, original_query: str
    ) -> CandidatePool:
        self.queries.append(original_query)
        return CandidatePool(
            candidates=pool.candidates,
            retrieval_mode=RetrievalMode.HYBRID,
            ordering=CandidateOrdering.RERANKER,
            degraded=False,
        )


class FakeSelector:
    def select(self, pool: CandidatePool) -> tuple[EvidenceRef, ...]:
        return (REF,)


class FakeParent:
    async def expand(self, refs: tuple[EvidenceRef, ...]) -> tuple[LocalEvidence, ...]:
        return (EVIDENCE,)


class FakeSufficiency:
    async def assess(
        self,
        request: LocalRetrievalRequest,
        pool: CandidatePool,
        evidence: tuple[LocalEvidence, ...],
    ) -> LocalEvidenceResult:
        return LocalEvidenceResult(
            execution_status=LocalExecutionStatus.OK,
            evidence_status=LocalEvidenceStatus.SUFFICIENT,
            selected_evidence=(EVIDENCE,),
        )


def services() -> LocalEvidenceServices:
    return LocalEvidenceServices(
        retrieval=cast(Any, FakeRetrieval()),
        reranker=cast(Any, FakeReranker()),
        selector=cast(Any, FakeSelector()),
        parent_context=cast(Any, FakeParent()),
        sufficiency=cast(Any, FakeSufficiency()),
        profile=LocalEvidenceProfile(),
    )


@pytest.mark.asyncio
async def test_graph_returns_only_business_result_and_calls_one_retrieval() -> None:
    bundle = services()
    runner = LocalEvidenceRunner(bundle)
    result = await runner.run(
        LocalRetrievalRequest(original_query="q", retrieval_query="r")
    )
    assert result.evidence_status is LocalEvidenceStatus.SUFFICIENT
    assert result.selected_evidence == (EVIDENCE,)
    assert isinstance(bundle.retrieval, FakeRetrieval)
    assert bundle.retrieval.calls == 1
    assert isinstance(bundle.reranker, FakeReranker)
    assert bundle.reranker.queries == ["q"]


@pytest.mark.asyncio
async def test_graph_reranks_with_original_query_not_retrieval_query() -> None:
    bundle = services()
    runner = LocalEvidenceRunner(bundle)
    await runner.run(
        LocalRetrievalRequest(original_query="original", retrieval_query="rewritten")
    )
    assert isinstance(bundle.reranker, FakeReranker)
    assert bundle.reranker.queries == ["original"]


def test_graph_topology_is_exactly_the_six_thin_nodes() -> None:
    from atlasrag.graphs.local_evidence.subgraph import build_local_evidence_subgraph
    from atlasrag.services.local_evidence.service import LocalEvidenceServices

    graph = build_local_evidence_subgraph(services())
    diagram = graph.get_graph()
    node_names = set(diagram.nodes)
    assert node_names == {
        "__start__",
        "retrieve_candidates",
        "fuse_candidates",
        "rerank_candidates",
        "select_evidence",
        "expand_context",
        "assess_sufficiency",
        "__end__",
    }
    assert all(
        annotation is not None
        for annotation in LocalEvidenceServices.__dataclass_fields__.values()
    )
