"""Top-level retrieval graph State schemas."""

from typing import NotRequired, TypedDict

from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse, RagCoreResult


class RetrievalGraphInput(TypedDict):
    """Input admitted by the top-level retrieval graph."""

    request: UserTurnRequest


class RetrievalGraphState(TypedDict):
    """Minimal execution data owned by the retrieval graph."""

    request: UserTurnRequest
    rag_result: NotRequired[RagCoreResult]
    final_response: NotRequired[FinalResponse]


class RetrievalGraphOutput(TypedDict):
    """Validated output returned by the retrieval graph."""

    final_response: FinalResponse
