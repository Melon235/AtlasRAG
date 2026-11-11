"""Immutable Stage 6 retrieval/reranking/sufficiency execution profiles."""

from typing import Annotated

from pydantic import Field, StrictInt

from atlasrag.domain.base import FrozenModel, StrictReal

PositiveInt = Annotated[StrictInt, Field(ge=1)]
ScoreThreshold = Annotated[StrictReal, Field(ge=0.0, le=1.0)]


class LocalEvidenceProfile(FrozenModel):
    dense_top_k: PositiveInt = 20
    bm25_top_k: PositiveInt = 20
    rrf_k: PositiveInt = 60
    max_rerank_candidates: PositiveInt = 20
    max_evidence: PositiveInt = 5


class DeterministicSufficiencyProfile(FrozenModel):
    """Baseline rule profile; comparator selection remains Stage 9 work."""

    sufficient_rerank_score: ScoreThreshold = 0.5
    ambiguous_rerank_score: ScoreThreshold = 0.2
    minimum_rrf_score: Annotated[StrictReal, Field(ge=0.0)] = 0.03

    def model_post_init(self, __context: object) -> None:
        if self.ambiguous_rerank_score >= self.sufficient_rerank_score:
            raise ValueError(
                "ambiguous threshold must be lower than sufficient threshold"
            )
