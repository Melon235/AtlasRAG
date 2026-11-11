"""Deterministic Reciprocal Rank Fusion and branch-survivor selection."""

from __future__ import annotations

from atlasrag.domain.enums import (
    CandidateOrdering,
    RetrievalBranch,
    RetrievalBranchStatus,
    RetrievalMode,
)
from atlasrag.domain.errors import CanonicalDataError, InvariantViolationError
from atlasrag.domain.retrieval import (
    CandidatePool,
    CandidateScores,
    RetrievalBranchResult,
    RetrievalCandidate,
)
from atlasrag.services.local_evidence.profile import LocalEvidenceProfile


def _validate_branch(
    branch: RetrievalBranchResult, expected: RetrievalBranch
) -> RetrievalBranchResult:
    result = RetrievalBranchResult.model_validate(branch)
    if result.branch is not expected:
        raise InvariantViolationError
    return result


def _same_identity(left: RetrievalCandidate, right: RetrievalCandidate) -> bool:
    return (
        left.chunk_id,
        left.document_id,
        left.revision_id,
        left.chunk_type,
        left.parent_id,
        left.retrieval_text,
    ) == (
        right.chunk_id,
        right.document_id,
        right.revision_id,
        right.chunk_type,
        right.parent_id,
        right.retrieval_text,
    )


def _survivor(
    branch: RetrievalBranchResult,
    mode: RetrievalMode,
    ordering: CandidateOrdering,
) -> CandidatePool:
    return CandidatePool(
        candidates=branch.candidates,
        retrieval_mode=mode,
        ordering=ordering,
        degraded=True,
    )


def fuse(
    dense: RetrievalBranchResult,
    bm25: RetrievalBranchResult,
    *,
    profile: LocalEvidenceProfile,
) -> CandidatePool:
    dense = _validate_branch(dense, RetrievalBranch.DENSE)
    bm25 = _validate_branch(bm25, RetrievalBranch.BM25)
    dense_down = dense.status is RetrievalBranchStatus.UNAVAILABLE
    bm25_down = bm25.status is RetrievalBranchStatus.UNAVAILABLE
    if dense_down and bm25_down:
        return CandidatePool(
            candidates=(),
            retrieval_mode=RetrievalMode.UNAVAILABLE,
            ordering=CandidateOrdering.RRF,
            degraded=True,
        )
    if dense_down:
        return _survivor(bm25, RetrievalMode.BM25_ONLY, CandidateOrdering.BM25)
    if bm25_down:
        return _survivor(dense, RetrievalMode.DENSE_ONLY, CandidateOrdering.DENSE)

    dense_rank = {
        candidate.chunk_id: index
        for index, candidate in enumerate(dense.candidates, start=1)
    }
    bm25_rank = {
        candidate.chunk_id: index
        for index, candidate in enumerate(bm25.candidates, start=1)
    }
    dense_by_id = {candidate.chunk_id: candidate for candidate in dense.candidates}
    bm25_by_id = {candidate.chunk_id: candidate for candidate in bm25.candidates}
    if len(dense_by_id) != len(dense.candidates) or len(bm25_by_id) != len(
        bm25.candidates
    ):
        raise CanonicalDataError
    by_id: dict[str, RetrievalCandidate] = dict(dense_by_id)
    for candidate in bm25.candidates:
        existing = by_id.get(candidate.chunk_id)
        if existing is None:
            by_id[candidate.chunk_id] = candidate
        elif not _same_identity(existing, candidate):
            raise CanonicalDataError
    fused: list[tuple[float, int, str, RetrievalCandidate]] = []
    for chunk_id, candidate in by_id.items():
        dense_value = (
            1.0 / (profile.rrf_k + dense_rank[chunk_id])
            if chunk_id in dense_rank
            else 0.0
        )
        bm25_value = (
            1.0 / (profile.rrf_k + bm25_rank[chunk_id])
            if chunk_id in bm25_rank
            else 0.0
        )
        score = dense_value + bm25_value
        best_rank = min(
            rank
            for rank in (
                dense_rank.get(chunk_id),
                bm25_rank.get(chunk_id),
            )
            if rank is not None
        )
        merged = RetrievalCandidate(
            chunk_id=candidate.chunk_id,
            document_id=candidate.document_id,
            revision_id=candidate.revision_id,
            chunk_type=candidate.chunk_type,
            parent_id=candidate.parent_id,
            retrieval_text=candidate.retrieval_text,
            scores=CandidateScores(
                dense_score=(
                    dense_by_id[chunk_id].scores.dense_score
                    if chunk_id in dense_by_id
                    else None
                ),
                bm25_score=(
                    bm25_by_id[chunk_id].scores.bm25_score
                    if chunk_id in bm25_by_id
                    else None
                ),
                fusion_score=score,
            ),
        )
        fused.append((score, best_rank, chunk_id, merged))
    fused.sort(key=lambda item: (-item[0], item[1], item[2]))
    return CandidatePool(
        candidates=tuple(item[3] for item in fused),
        retrieval_mode=RetrievalMode.HYBRID,
        ordering=CandidateOrdering.RRF,
        degraded=False,
    )
