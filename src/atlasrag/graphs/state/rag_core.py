"""RAG core orchestration State schemas."""

from typing import NotRequired, TypedDict

from atlasrag.domain.answer import AnswerInput
from atlasrag.domain.enums import AnswerExecutionOutcome
from atlasrag.domain.evidence import LocalEvidenceResult
from atlasrag.domain.requests import LocalRetrievalRequest, UserTurnRequest
from atlasrag.domain.results import FinalResponse, RagCoreResult
from atlasrag.domain.web import WebEvidenceResult


class RagCoreInput(TypedDict):
    """Input admitted by the RAG core subgraph."""

    request: UserTurnRequest


class RagCoreState(TypedDict):
    """Minimal execution data owned by the RAG core subgraph."""

    request: UserTurnRequest
    base_local_request: NotRequired[LocalRetrievalRequest]
    current_local_request: NotRequired[LocalRetrievalRequest]
    local_attempt_index: int
    local_result: NotRequired[LocalEvidenceResult]
    web_result: NotRequired[WebEvidenceResult]
    answer_input: NotRequired[AnswerInput]
    final_response: NotRequired[FinalResponse]
    answer_outcome: NotRequired[AnswerExecutionOutcome]
    result: NotRequired[RagCoreResult]


class RagCoreOutput(TypedDict):
    """Business result returned by the RAG core subgraph."""

    result: RagCoreResult
