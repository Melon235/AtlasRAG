"""Reranker service with explicit degradation, never query rewriting."""

from __future__ import annotations

from atlasrag.domain.enums import CandidateOrdering, RetrievalMode
from atlasrag.domain.errors import DependencyError, ProviderResponseError
from atlasrag.domain.retrieval import (
    CandidatePool,
    CandidateScores,
    RetrievalCandidate,
)
from atlasrag.providers.reranker.base import (
    RerankerInputLimitError,
    RerankerProvider,
)
from atlasrag.services.local_evidence.profile import LocalEvidenceProfile


def _with_score(candidate: RetrievalCandidate, score: float) -> RetrievalCandidate:
    return RetrievalCandidate(
        chunk_id=candidate.chunk_id,
        document_id=candidate.document_id,
        revision_id=candidate.revision_id,
        chunk_type=candidate.chunk_type,
        parent_id=candidate.parent_id,
        retrieval_text=candidate.retrieval_text,
        scores=CandidateScores(
            dense_score=candidate.scores.dense_score,
            bm25_score=candidate.scores.bm25_score,
            fusion_score=candidate.scores.fusion_score,
            rerank_score=score,
        ),
    )


class RerankerService:
    def __init__(
        self,
        *,
        provider: RerankerProvider,
        profile: LocalEvidenceProfile,
    ) -> None:
        self._provider = provider
        self._profile = profile

    async def rerank(
        self,
        pool: CandidatePool,
        *,
        original_query: str,
    ) -> CandidatePool:
        pool = CandidatePool.model_validate(pool)
        if pool.retrieval_mode is RetrievalMode.UNAVAILABLE or not pool.candidates:
            return pool
        candidates = pool.candidates[: self._profile.max_rerank_candidates]
        tail = pool.candidates[len(candidates) :]
        try:
            scores = await self._provider.score(
                original_query,
                tuple(candidate.retrieval_text for candidate in candidates),
            )
        except (DependencyError, ProviderResponseError, RerankerInputLimitError):
            return pool.model_copy(update={"degraded": True})
        if len(scores) != len(candidates):
            raise ProviderResponseError
        ranked = sorted(
            zip(candidates, scores, range(len(candidates)), strict=True),
            key=lambda item: (-item[1], item[2], item[0].chunk_id),
        )
        reranked = tuple(
            _with_score(candidate, score) for candidate, score, _ in ranked
        )
        return CandidatePool(
            candidates=reranked + tail,
            retrieval_mode=pool.retrieval_mode,
            ordering=CandidateOrdering.RERANKER,
            degraded=pool.degraded,
        )
