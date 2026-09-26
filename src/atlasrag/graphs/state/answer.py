"""Answer subgraph State schemas."""

from typing import NotRequired, TypedDict

from atlasrag.domain.answer import (
    AnswerInput,
    GenerationResult,
    OutputReviewResult,
    WorkingEvidenceBundle,
)
from atlasrag.domain.enums import AnswerExecutionOutcome
from atlasrag.domain.results import FinalResponse


class AnswerSubgraphInput(TypedDict):
    """Input admitted by the answer subgraph."""

    input: AnswerInput


class AnswerState(TypedDict):
    """Minimal execution data owned by the answer subgraph."""

    input: AnswerInput
    working_evidence: NotRequired[WorkingEvidenceBundle]
    generation_attempt_index: int
    generation_result: NotRequired[GenerationResult]
    review_result: NotRequired[OutputReviewResult]
    final_response: NotRequired[FinalResponse]
    outcome: NotRequired[AnswerExecutionOutcome]


class AnswerSubgraphOutput(TypedDict):
    """Validated response and outcome returned by the answer subgraph."""

    final_response: FinalResponse
    outcome: AnswerExecutionOutcome
