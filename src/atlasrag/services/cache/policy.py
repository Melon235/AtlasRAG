"""Conservative Query Cache eligibility policy (policy-derived, not result state)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from atlasrag.domain.enums import RagCompletionStatus
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import RagCoreResult

_LOCAL_ID = re.compile(r"^L[1-9][0-9]*$")
_TIME_SENSITIVE = re.compile(
    r"\b(?:today|yesterday|tomorrow|current|currently|now|latest|recent|"
    r"last\s+(?:year|month|week)|this\s+(?:year|month|week)|"
    r"\d{4})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CacheDecision:
    eligible: bool
    reason: str


class QueryCachePolicy:
    def __init__(self, *, enabled: bool, max_entry_bytes: int = 262_144) -> None:
        if type(enabled) is not bool:
            raise ValueError("cache enabled must be boolean")
        if type(max_entry_bytes) is not int or max_entry_bytes < 1:
            raise ValueError("max_entry_bytes must be positive")
        self._enabled = enabled
        self._max_entry_bytes = max_entry_bytes

    @property
    def max_entry_bytes(self) -> int:
        return self._max_entry_bytes

    def decide(
        self,
        request: UserTurnRequest,
        result: RagCoreResult,
        *,
        web_invoked: bool = False,
    ) -> CacheDecision:
        if not self._enabled:
            return CacheDecision(False, "CACHE_DISABLED")
        if web_invoked:
            return CacheDecision(False, "WEB_INVOKED")
        if result.completion_status is RagCompletionStatus.SAFE_FAILURE:
            return CacheDecision(False, "SAFE_FAILURE")
        if result.completion_status is RagCompletionStatus.DEGRADED_SUCCESS:
            return CacheDecision(False, "DEGRADED_SUCCESS")
        if not result.final_response.citations:
            return CacheDecision(False, "NO_CITATIONS")
        if not all(
            _LOCAL_ID.fullmatch(citation.citation_id)
            for citation in result.final_response.citations
        ):
            return CacheDecision(False, "WEB_DERIVED")
        if _TIME_SENSITIVE.search(request.query):
            return CacheDecision(False, "TIME_SENSITIVE")
        return CacheDecision(True, "LOCAL_REPRODUCIBLE_SUCCESS")
