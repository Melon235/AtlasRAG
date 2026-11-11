"""Reranker degradation and sufficiency boundary tests."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from atlasrag.domain.enums import (
    CandidateOrdering,
    EvidenceType,
    LocalEvidenceStatus,
    LocalExecutionStatus,
    RetrievalChunkType,
    RetrievalMode,
)
from atlasrag.domain.evidence import LocalEvidence, LocalProvenance
from atlasrag.domain.requests import LocalRetrievalRequest
from atlasrag.domain.retrieval import CandidatePool, CandidateScores, RetrievalCandidate
from atlasrag.providers.reranker.base import (
    RerankerModelIdentity,
    RerankerUnavailableError,
)
from atlasrag.services.local_evidence.profile import (
    DeterministicSufficiencyProfile,
    LocalEvidenceProfile,
)
from atlasrag.services.local_evidence.reranking import RerankerService
from atlasrag.services.local_evidence.sufficiency import (
    DeterministicEvidenceSufficiency,
)


class FakeReranker:
    identity = RerankerModelIdentity(
        model_id="fake",
        model_revision="r1",
        tokenizer_id="fake",
        tokenizer_revision="r1",
        device="cpu",
        dtype="float32",
        max_input_tokens=8192,
    )

    async def initialize(self, *, allow_download: bool = False) -> None:
        return None

    def __init__(self, scores: tuple[float, ...] | None = None) -> None:
        self.scores = scores
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def score(self, query: str, passages: Sequence[str]) -> tuple[float, ...]:
        self.calls.append((query, tuple(passages)))
        if self.scores is None:
            raise RerankerUnavailableError
        return self.scores


def candidate(cid: str, text: str, fusion: float = 0.03) -> RetrievalCandidate:
    return RetrievalCandidate(
        chunk_id=cid,
        document_id="doc-1",
        revision_id="rev-1",
        chunk_type=RetrievalChunkType.TEXT_CHILD,
        parent_id="parent-1",
        retrieval_text=text,
        scores=CandidateScores(fusion_score=fusion),
    )


def pool(
    order: CandidateOrdering = CandidateOrdering.RRF, degraded: bool = False
) -> CandidatePool:
    return CandidatePool(
        candidates=(candidate("a", "first"), candidate("b", "second")),
        retrieval_mode=RetrievalMode.HYBRID,
        ordering=order,
        degraded=degraded,
    )


def evidence() -> tuple[LocalEvidence, ...]:
    return (
        LocalEvidence(
            evidence_type=EvidenceType.TEXT_PARENT,
            context_id="parent-1",
            document_id="doc-1",
            revision_id="rev-1",
            content="canonical parent text",
            provenance=LocalProvenance(file_name="a.md", relative_source_path="a.md"),
        ),
    )


@pytest.mark.asyncio
async def test_reranker_uses_original_query_and_reorders_by_score() -> None:
    provider = FakeReranker((0.9, 0.1))
    service = RerankerService(provider=provider, profile=LocalEvidenceProfile())
    result = await service.rerank(pool(), original_query="raw user question")
    assert provider.calls == [("raw user question", ("first", "second"))]
    assert result.ordering is CandidateOrdering.RERANKER
    assert tuple(item.chunk_id for item in result.candidates) == ("a", "b")
    assert result.candidates[0].scores.rerank_score == 0.9


@pytest.mark.asyncio
async def test_reranker_failure_preserves_ordering_and_marks_degraded() -> None:
    service = RerankerService(
        provider=FakeReranker(None), profile=LocalEvidenceProfile()
    )
    before = pool()
    result = await service.rerank(before, original_query="q")
    assert result.ordering is CandidateOrdering.RRF
    assert tuple(item.chunk_id for item in result.candidates) == ("a", "b")
    assert result.degraded


@pytest.mark.asyncio
async def test_sufficiency_score_bands_and_unavailable() -> None:
    strategy = DeterministicEvidenceSufficiency(
        profile=DeterministicSufficiencyProfile(
            sufficient_rerank_score=0.7,
            ambiguous_rerank_score=0.2,
        )
    )
    request = LocalRetrievalRequest(original_query="q", retrieval_query="r")
    sufficient_pool = CandidatePool(
        candidates=(
            RetrievalCandidate(
                chunk_id="a",
                document_id="doc-1",
                revision_id="rev-1",
                chunk_type=RetrievalChunkType.TEXT_CHILD,
                parent_id="parent-1",
                retrieval_text="text",
                scores=CandidateScores(rerank_score=0.9),
            ),
        ),
        retrieval_mode=RetrievalMode.HYBRID,
        ordering=CandidateOrdering.RERANKER,
        degraded=False,
    )
    result = await strategy.assess(request, sufficient_pool, evidence())
    assert result.execution_status is LocalExecutionStatus.OK
    assert result.evidence_status is LocalEvidenceStatus.SUFFICIENT
    assert result.selected_evidence == evidence()

    ambiguous_pool = sufficient_pool.model_copy(
        update={
            "candidates": (
                sufficient_pool.candidates[0].model_copy(
                    update={"scores": CandidateScores(rerank_score=0.4)}
                ),
            )
        }
    )
    ambiguous = await strategy.assess(request, ambiguous_pool, evidence())
    assert ambiguous.evidence_status is LocalEvidenceStatus.AMBIGUOUS
    assert ambiguous.selected_evidence == ()
    assert ambiguous.best_available_evidence is None

    unavailable = CandidatePool(
        candidates=(),
        retrieval_mode=RetrievalMode.UNAVAILABLE,
        ordering=CandidateOrdering.RRF,
        degraded=True,
    )
    result = await strategy.assess(request, unavailable, ())
    assert result.execution_status is LocalExecutionStatus.UNAVAILABLE
    assert result.evidence_status is None
    assert result.selected_evidence == ()


@pytest.mark.asyncio
async def test_rrf_threshold_is_not_satisfied_by_a_single_top_rank() -> None:
    strategy = DeterministicEvidenceSufficiency(
        profile=DeterministicSufficiencyProfile(minimum_rrf_score=0.03)
    )
    request = LocalRetrievalRequest(original_query="q", retrieval_query="r")
    weak = CandidatePool(
        candidates=(
            RetrievalCandidate(
                chunk_id="a",
                document_id="doc-1",
                revision_id="rev-1",
                chunk_type=RetrievalChunkType.TEXT_CHILD,
                parent_id="parent-1",
                retrieval_text="text",
                scores=CandidateScores(fusion_score=1 / 61),
            ),
        ),
        retrieval_mode=RetrievalMode.HYBRID,
        ordering=CandidateOrdering.RRF,
        degraded=True,
    )
    result = await strategy.assess(request, weak, evidence())
    assert result.execution_status is LocalExecutionStatus.DEGRADED
    assert result.evidence_status is LocalEvidenceStatus.INSUFFICIENT
    assert result.selected_evidence == ()
    assert result.best_available_evidence == evidence()[0]
