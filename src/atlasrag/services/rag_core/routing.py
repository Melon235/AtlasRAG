"""Pure bounded corrective routing for one final local evidence result."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from atlasrag.domain.enums import LocalEvidenceStatus, LocalExecutionStatus
from atlasrag.domain.evidence import LocalEvidenceResult
from atlasrag.domain.requests import LocalRetrievalRequest


class CorrectiveAction(StrEnum):
    ANSWER = "ANSWER"
    RETRY_LOCAL = "RETRY_LOCAL"
    WEB = "WEB"


@dataclass(frozen=True)
class RoutingDecision:
    action: CorrectiveAction
    request: LocalRetrievalRequest | None = None


class CorrectiveRoutingService:
    def __init__(self, *, max_retries: int = 1) -> None:
        if type(max_retries) is not int or not 0 <= max_retries <= 3:
            raise ValueError("max_retries must be within [0, 3]")
        self._max_retries = max_retries

    @property
    def max_retries(self) -> int:
        return self._max_retries

    def decide(
        self,
        *,
        base: LocalRetrievalRequest,
        current: LocalRetrievalRequest,
        result: LocalEvidenceResult,
        attempt_index: int,
    ) -> RoutingDecision:
        base = LocalRetrievalRequest.model_validate(base)
        current = LocalRetrievalRequest.model_validate(current)
        result = LocalEvidenceResult.model_validate(result)
        if type(attempt_index) is not int or attempt_index < 1:
            raise ValueError("attempt_index must be positive")
        if result.evidence_status is LocalEvidenceStatus.SUFFICIENT:
            return RoutingDecision(CorrectiveAction.ANSWER)
        if result.execution_status is LocalExecutionStatus.UNAVAILABLE:
            return RoutingDecision(CorrectiveAction.WEB)
        can_retry = attempt_index <= self._max_retries
        if result.evidence_status is LocalEvidenceStatus.AMBIGUOUS and can_retry:
            if current.filters is not None and current.filters.file_name is not None:
                candidate = LocalRetrievalRequest(
                    original_query=current.original_query,
                    retrieval_query=base.original_query,
                    filters=current.filters.model_copy(update={"file_name": None}),
                )
            else:
                candidate = LocalRetrievalRequest(
                    original_query=current.original_query,
                    retrieval_query=base.original_query,
                    filters=current.filters,
                )
            if _meaningfully_different(candidate, current):
                return RoutingDecision(CorrectiveAction.RETRY_LOCAL, candidate)
            return RoutingDecision(CorrectiveAction.WEB)
        if (
            result.evidence_status is LocalEvidenceStatus.INSUFFICIENT
            and can_retry
            and current.filters is not None
            and current.filters.file_name is not None
        ):
            candidate = LocalRetrievalRequest(
                original_query=current.original_query,
                retrieval_query=base.retrieval_query,
                filters=current.filters.model_copy(update={"file_name": None}),
            )
            if _meaningfully_different(candidate, current):
                return RoutingDecision(CorrectiveAction.RETRY_LOCAL, candidate)
        return RoutingDecision(CorrectiveAction.WEB)


def _meaningfully_different(
    candidate: LocalRetrievalRequest, current: LocalRetrievalRequest
) -> bool:
    return (
        candidate.retrieval_query != current.retrieval_query
        or candidate.original_query != current.original_query
        or candidate.filters != current.filters
    )
