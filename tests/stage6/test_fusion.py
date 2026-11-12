"""Deterministic RRF and branch-survivor tests."""

from __future__ import annotations

import pytest

from atlasrag.domain.enums import (
    CandidateOrdering,
    RetrievalBranch,
    RetrievalBranchStatus,
    RetrievalChunkType,
    RetrievalMode,
)
from atlasrag.domain.retrieval import (
    CandidateScores,
    RetrievalBranchResult,
    RetrievalCandidate,
)
from atlasrag.services.local_evidence.fusion import fuse
from atlasrag.services.local_evidence.profile import LocalEvidenceProfile


def candidate(
    chunk_id: str,
    text: str = "text",
    score: float | None = None,
    *,
    bm25: float | None = None,
) -> RetrievalCandidate:
    return RetrievalCandidate(
        chunk_id=chunk_id,
        document_id="document-1",
        revision_id="revision-1",
        chunk_type=RetrievalChunkType.TEXT_CHILD,
        parent_id="parent-1",
        retrieval_text=text,
        scores=CandidateScores(dense_score=score, bm25_score=bm25),
    )


def branch(
    kind: RetrievalBranch,
    status: RetrievalBranchStatus,
    items: tuple[RetrievalCandidate, ...] = (),
) -> RetrievalBranchResult:
    return RetrievalBranchResult(branch=kind, status=status, candidates=items)


def test_hybrid_rrf_merges_duplicate_chunk_and_is_deterministic() -> None:
    profile = LocalEvidenceProfile()
    pool = fuse(
        branch(
            RetrievalBranch.DENSE,
            RetrievalBranchStatus.OK,
            (candidate("a", score=0.9), candidate("b", score=0.8)),
        ),
        branch(
            RetrievalBranch.BM25,
            RetrievalBranchStatus.OK,
            (candidate("b", bm25=1.0), candidate("a", bm25=0.5)),
        ),
        profile=profile,
    )
    assert pool.retrieval_mode is RetrievalMode.HYBRID
    assert pool.ordering is CandidateOrdering.RRF
    assert not pool.degraded
    assert tuple(item.chunk_id for item in pool.candidates) == ("a", "b")
    assert pool.candidates[0].scores.fusion_score == pytest.approx(1 / 61 + 1 / 62)
    assert pool.candidates[1].scores.fusion_score == pytest.approx(1 / 62 + 1 / 61)
    assert all(item.scores.dense_score is not None for item in pool.candidates)
    assert all(item.scores.bm25_score is not None for item in pool.candidates)


def test_single_surviving_branch_keeps_its_ordering_without_fake_rrf() -> None:
    pool = fuse(
        branch(RetrievalBranch.DENSE, RetrievalBranchStatus.UNAVAILABLE),
        branch(RetrievalBranch.BM25, RetrievalBranchStatus.OK, (candidate("b"),)),
        profile=LocalEvidenceProfile(),
    )
    assert pool.retrieval_mode is RetrievalMode.BM25_ONLY
    assert pool.ordering is CandidateOrdering.BM25
    assert pool.degraded
    assert pool.candidates[0].chunk_id == "b"
    assert pool.candidates[0].scores.fusion_score is None


def test_both_unavailable_is_unavailable() -> None:
    pool = fuse(
        branch(RetrievalBranch.DENSE, RetrievalBranchStatus.UNAVAILABLE),
        branch(RetrievalBranch.BM25, RetrievalBranchStatus.UNAVAILABLE),
        profile=LocalEvidenceProfile(),
    )
    assert pool.retrieval_mode is RetrievalMode.UNAVAILABLE
    assert pool.candidates == ()
    assert pool.degraded
