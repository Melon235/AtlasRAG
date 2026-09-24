"""Local retrieval candidates, branch outcomes, and candidate pools."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import (
    BeforeValidator,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

from atlasrag.domain.base import (
    FrozenModel,
    StrictReal,
    validate_ordered_collection_input,
    validate_string_enum_input,
)
from atlasrag.domain.enums import (
    CandidateOrdering,
    RetrievalBranch,
    RetrievalBranchStatus,
    RetrievalChunkType,
    RetrievalMode,
)

_RetrievalChunkTypeInput = Annotated[
    RetrievalChunkType, BeforeValidator(validate_string_enum_input)
]
_RetrievalBranchInput = Annotated[
    RetrievalBranch, BeforeValidator(validate_string_enum_input)
]
_RetrievalBranchStatusInput = Annotated[
    RetrievalBranchStatus, BeforeValidator(validate_string_enum_input)
]
_RetrievalModeInput = Annotated[
    RetrievalMode, BeforeValidator(validate_string_enum_input)
]
_CandidateOrderingInput = Annotated[
    CandidateOrdering, BeforeValidator(validate_string_enum_input)
]


def _validate_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class CandidateScores(FrozenModel):
    """Optional score slots for the current retrieval candidate."""

    dense_score: StrictReal | None = None
    bm25_score: StrictReal | None = None
    fusion_score: StrictReal | None = None
    rerank_score: StrictReal | None = None


class RetrievalCandidate(FrozenModel):
    """One retrievable TEXT_CHILD or TABLE candidate."""

    chunk_id: StrictStr
    document_id: StrictStr
    revision_id: StrictStr
    chunk_type: _RetrievalChunkTypeInput
    parent_id: StrictStr | None = None
    retrieval_text: StrictStr
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

    branch: _RetrievalBranchInput
    status: _RetrievalBranchStatusInput
    candidates: Annotated[
        tuple[RetrievalCandidate, ...],
        BeforeValidator(validate_ordered_collection_input),
    ]

    @model_validator(mode="after")
    def _status_matches_candidates(self) -> Self:
        if self.status is RetrievalBranchStatus.OK and not self.candidates:
            raise ValueError("OK requires non-empty candidates")
        if self.status is not RetrievalBranchStatus.OK and self.candidates:
            raise ValueError("EMPTY and UNAVAILABLE require empty candidates")
        return self


class CandidatePool(FrozenModel):
    """Ordered candidates after branch selection or fusion."""

    candidates: Annotated[
        tuple[RetrievalCandidate, ...],
        BeforeValidator(validate_ordered_collection_input),
    ]
    retrieval_mode: _RetrievalModeInput
    ordering: _CandidateOrderingInput
    degraded: StrictBool

    @model_validator(mode="after")
    def _mode_matches_candidates(self) -> Self:
        if self.retrieval_mode is RetrievalMode.UNAVAILABLE and self.candidates:
            raise ValueError("UNAVAILABLE requires empty candidates")
        return self
