"""Local evidence sufficiency boundary and deterministic baseline strategy."""

from __future__ import annotations

from typing import Protocol

from atlasrag.domain.enums import (
    CandidateOrdering,
    LocalEvidenceStatus,
    LocalExecutionStatus,
    RetrievalMode,
)
from atlasrag.domain.evidence import LocalEvidence, LocalEvidenceResult
from atlasrag.domain.requests import LocalRetrievalRequest
from atlasrag.domain.retrieval import CandidatePool
from atlasrag.services.local_evidence.profile import DeterministicSufficiencyProfile


class LocalEvidenceSufficiencyStrategy(Protocol):
    async def assess(
        self,
        request: LocalRetrievalRequest,
        pool: CandidatePool,
        evidence: tuple[LocalEvidence, ...],
    ) -> LocalEvidenceResult: ...


class DeterministicEvidenceSufficiency:
    """Rule/score baseline; Stage 9 owns comparison with LLM strategies."""

    def __init__(self, *, profile: DeterministicSufficiencyProfile) -> None:
        self._profile = profile

    async def assess(
        self,
        request: LocalRetrievalRequest,
        pool: CandidatePool,
        evidence: tuple[LocalEvidence, ...],
    ) -> LocalEvidenceResult:
        request = LocalRetrievalRequest.model_validate(request)
        pool = CandidatePool.model_validate(pool)
        if pool.retrieval_mode is RetrievalMode.UNAVAILABLE:
            return LocalEvidenceResult(
                execution_status=LocalExecutionStatus.UNAVAILABLE,
                evidence_status=None,
                selected_evidence=(),
                best_available_evidence=None,
            )
        execution = (
            LocalExecutionStatus.DEGRADED if pool.degraded else LocalExecutionStatus.OK
        )
        if not evidence:
            return LocalEvidenceResult(
                execution_status=execution,
                evidence_status=LocalEvidenceStatus.INSUFFICIENT,
                selected_evidence=(),
                best_available_evidence=None,
            )
        top = evidence[0]
        if pool.ordering is CandidateOrdering.RERANKER:
            score = pool.candidates[0].scores.rerank_score
            if score is None:
                raise ValueError("RERANKER ordering requires a top rerank score")
            if score >= self._profile.sufficient_rerank_score:
                return LocalEvidenceResult(
                    execution_status=execution,
                    evidence_status=LocalEvidenceStatus.SUFFICIENT,
                    selected_evidence=evidence,
                    best_available_evidence=None,
                )
            if score <= self._profile.ambiguous_rerank_score:
                return LocalEvidenceResult(
                    execution_status=execution,
                    evidence_status=LocalEvidenceStatus.INSUFFICIENT,
                    selected_evidence=(),
                    best_available_evidence=top,
                )
            return LocalEvidenceResult(
                execution_status=execution,
                evidence_status=LocalEvidenceStatus.AMBIGUOUS,
                selected_evidence=(),
                best_available_evidence=None,
            )
        if pool.ordering is CandidateOrdering.RRF:
            fusion = pool.candidates[0].scores.fusion_score
            if fusion is not None and fusion >= self._profile.minimum_rrf_score:
                return LocalEvidenceResult(
                    execution_status=execution,
                    evidence_status=LocalEvidenceStatus.SUFFICIENT,
                    selected_evidence=evidence,
                    best_available_evidence=None,
                )
        return LocalEvidenceResult(
            execution_status=execution,
            evidence_status=LocalEvidenceStatus.INSUFFICIENT,
            selected_evidence=(),
            best_available_evidence=top,
        )
