"""Evidence-to-Answer boundary and answer-output contracts."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import (
    BeforeValidator,
    Field,
    StrictStr,
    field_validator,
    model_validator,
)

from atlasrag.domain.base import (
    FrozenModel,
    validate_ordered_collection_input,
    validate_string_enum_input,
)
from atlasrag.domain.enums import (
    LocalAnswerEvidenceStatus,
    OutputReviewDecision,
    OutputReviewReasonCode,
)
from atlasrag.domain.evidence import LocalEvidence
from atlasrag.domain.web import WebEvidence

_LocalCitationId = Annotated[StrictStr, Field(pattern=r"^L[1-9][0-9]*$")]
_WebCitationId = Annotated[StrictStr, Field(pattern=r"^W[1-9][0-9]*$")]
_CitationId = Annotated[StrictStr, Field(pattern=r"^(?:L|W)[1-9][0-9]*$")]
_LocalAnswerEvidenceStatusInput = Annotated[
    LocalAnswerEvidenceStatus,
    BeforeValidator(validate_string_enum_input),
]
_OutputReviewDecisionInput = Annotated[
    OutputReviewDecision,
    BeforeValidator(validate_string_enum_input),
]
_OutputReviewReasonCodeInput = Annotated[
    OutputReviewReasonCode,
    BeforeValidator(validate_string_enum_input),
]


def _validate_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _validate_unique_citation_ids(
    local_evidence: tuple[AnswerLocalEvidence, ...],
    web_evidence: tuple[AnswerWebEvidence, ...],
) -> None:
    citation_ids = tuple(item.citation_id for item in local_evidence) + tuple(
        item.citation_id for item in web_evidence
    )
    if len(citation_ids) != len(set(citation_ids)):
        raise ValueError("citation IDs must be globally unique")


class AnswerLocalEvidence(FrozenModel):
    """Canonical local evidence paired with its runtime citation ID."""

    citation_id: _LocalCitationId
    evidence: LocalEvidence


class AnswerWebEvidence(FrozenModel):
    """Canonical Web evidence paired with its runtime citation ID."""

    citation_id: _WebCitationId
    evidence: WebEvidence


class AnswerInput(FrozenModel):
    """Immutable authoritative citation mapping supplied to answer generation."""

    original_query: StrictStr
    local_evidence_status: _LocalAnswerEvidenceStatusInput
    local_evidence: Annotated[
        tuple[AnswerLocalEvidence, ...],
        BeforeValidator(validate_ordered_collection_input),
    ] = ()
    web_evidence: Annotated[
        tuple[AnswerWebEvidence, ...],
        BeforeValidator(validate_ordered_collection_input),
    ] = ()

    _nonblank_original_query = field_validator("original_query")(_validate_nonblank)

    @model_validator(mode="after")
    def _validate_evidence_mapping(self) -> Self:
        _validate_unique_citation_ids(self.local_evidence, self.web_evidence)

        local_count = len(self.local_evidence)
        if (
            self.local_evidence_status is LocalAnswerEvidenceStatus.SUFFICIENT
            and local_count == 0
        ):
            raise ValueError("SUFFICIENT requires non-empty local_evidence")
        if (
            self.local_evidence_status
            is LocalAnswerEvidenceStatus.INSUFFICIENT_BEST_MATCH
            and local_count != 1
        ):
            raise ValueError(
                "INSUFFICIENT_BEST_MATCH requires exactly one local_evidence item"
            )
        if (
            self.local_evidence_status is LocalAnswerEvidenceStatus.NONE
            and local_count != 0
        ):
            raise ValueError("NONE requires empty local_evidence")
        return self


class WorkingEvidenceBundle(FrozenModel):
    """Temporary typed bundle used while assembling answer evidence."""

    local_evidence: Annotated[
        tuple[AnswerLocalEvidence, ...],
        BeforeValidator(validate_ordered_collection_input),
    ] = ()
    web_evidence: Annotated[
        tuple[AnswerWebEvidence, ...],
        BeforeValidator(validate_ordered_collection_input),
    ] = ()

    @model_validator(mode="after")
    def _globally_unique_citation_ids(self) -> Self:
        _validate_unique_citation_ids(self.local_evidence, self.web_evidence)
        return self


class GenerationResult(FrozenModel):
    """Schema-constrained answer text and its declared citation IDs."""

    answer_text: StrictStr
    used_citation_ids: Annotated[
        tuple[_CitationId, ...],
        BeforeValidator(validate_ordered_collection_input),
    ] = ()

    _nonblank_answer_text = field_validator("answer_text")(_validate_nonblank)

    @field_validator("used_citation_ids")
    @classmethod
    def _unique_citation_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("used citation IDs must be unique")
        return value


class OutputReviewResult(FrozenModel):
    """Deterministic protocol review decision and stable reason codes."""

    decision: _OutputReviewDecisionInput
    reason_codes: Annotated[
        tuple[_OutputReviewReasonCodeInput, ...],
        BeforeValidator(validate_ordered_collection_input),
    ] = ()

    @field_validator("reason_codes")
    @classmethod
    def _unique_reason_codes(
        cls,
        value: tuple[OutputReviewReasonCode, ...],
    ) -> tuple[OutputReviewReasonCode, ...]:
        if len(value) != len(set(value)):
            raise ValueError("reason_codes must be unique")
        return value
