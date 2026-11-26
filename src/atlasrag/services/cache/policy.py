"""Conservative Query Cache eligibility policy (policy-derived, not result state)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from atlasrag.domain.enums import RagCompletionStatus
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import RagCoreResult
from atlasrag.providers.cache.keys import is_cache_key_component_safe

_LOCAL_ID = re.compile(r"^L[1-9][0-9]*$")
_TIME_SENSITIVE = re.compile(
    r"(?:\b(?:today|yesterday|tomorrow|current|currently|now|latest|recent|"
    r"last\s+(?:year|month|week)|this\s+(?:year|month|week))\b)|"
    r"(?:(?<!\d)\d{4}(?!\d)|\d{4}\s*年)|"
    r"(?:今天|昨天|明天|当前|目前|现在|最近|最新|实时|本月|今年|去年|"
    r"上个月|这个月|上季度|本季度|上周|下周|本周|这周|近期|近来|"
    r"近一周|近一个月|三天前|前两天)",
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
    def enabled(self) -> bool:
        return self._enabled

    @property
    def max_entry_bytes(self) -> int:
        return self._max_entry_bytes

    def request_is_cacheable(self, request: UserTurnRequest) -> bool:
        """Apply request-level bypasses consistently to reads and writes."""
        return (
            self._enabled
            and is_cache_key_component_safe(request.session_id)
            and _TIME_SENSITIVE.search(request.query) is None
        )

    def decide(
        self,
        request: UserTurnRequest,
        result: RagCoreResult,
        *,
        web_invoked: bool | None = None,
    ) -> CacheDecision:
        if web_invoked is None:
            return CacheDecision(False, "UNKNOWN_WEB_PROVENANCE")
        if type(web_invoked) is not bool:
            raise ValueError("web_invoked must be boolean or None")
        if not self.request_is_cacheable(request):
            if not self._enabled:
                reason = "CACHE_DISABLED"
            elif not is_cache_key_component_safe(request.session_id):
                reason = "UNSAFE_CACHE_KEY_COMPONENT"
            else:
                reason = "TIME_SENSITIVE"
            return CacheDecision(False, reason)
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
        return CacheDecision(True, "LOCAL_REPRODUCIBLE_SUCCESS")
