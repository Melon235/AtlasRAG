"""Local retrieval candidates, branch outcomes, and candidate pools."""

from __future__ import annotations

from typing import Self

from pydantic import field_validator, model_validator

from atlasrag.domain.base import FrozenModel
from atlasrag.domain.enums import (
    CandidateOrdering,
    RetrievalBranch,
    RetrievalBranchStatus,
    RetrievalChunkType,
    RetrievalMode,
)


def _validate_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class CandidateScores(FrozenModel):
    """Optional score slots for the current retrieval candidate."""

    dense_score: float | None = None
    bm25_score: float | None = None
    fusion_score: float | None = None
    rerank_score: float | None = None


class RetrievalCandidate(FrozenModel):
    """One retrievable TEXT_CHILD or TABLE candidate."""

    chunk_id: str
    document_id: str
    revision_id: str
    chunk_type: RetrievalChunkType
    parent_id: str | None = None
    retrieval_text: str
    scores: CandidateScores

    _nonblank_required_strings = field_validator(
        "chunk_id", "document_id", "revision_id", "retrieval_text"
    )(_validate_nonblank)

    @field_validator("parent_id")
    @classmethod
    def _nonblank_parent_if_present(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_nonblank(value)
        return value

    @model_validator(mode="after")
    def _text_child_has_parent(self) -> Self:
        if self.chunk_type is RetrievalChunkType.TEXT_CHILD and self.parent_id is None:
            raise ValueError("TEXT_CHILD candidates require parent_id")
        return self


class RetrievalBranchResult(FrozenModel):
    """Normal outcome from one retrieval capability branch."""

    branch: RetrievalBranch
    status: RetrievalBranchStatus
    candidates: tuple[RetrievalCandidate, ...]

    @model_validator(mode="after")
    def _status_matches_candidates(self) -> Self:
        if self.status is RetrievalBranchStatus.OK and not self.candidates:
            raise ValueError("OK requires non-empty candidates")
        if self.status is not RetrievalBranchStatus.OK and self.candidates:
            raise ValueError("EMPTY and UNAVAILABLE require empty candidates")
        return self


class CandidatePool(FrozenModel):
    """Ordered candidates after branch selection or fusion."""

    candidates: tuple[RetrievalCandidate, ...]
    retrieval_mode: RetrievalMode
    ordering: CandidateOrdering
    degraded: bool

    @model_validator(mode="after")
    def _mode_matches_candidates(self) -> Self:
        if self.retrieval_mode is RetrievalMode.UNAVAILABLE and self.candidates:
            raise ValueError("UNAVAILABLE requires empty candidates")
        return self
