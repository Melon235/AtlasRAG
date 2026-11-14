"""Query Construction with protected-term validation and bounded fallback."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from typing import Protocol

from pydantic import StrictStr, field_validator

from atlasrag.domain.base import FrozenModel
from atlasrag.domain.errors import DependencyError, ProviderResponseError
from atlasrag.domain.requests import (
    LocalRetrievalRequest,
    UserTurnRequest,
)

_PROTECTED = re.compile(
    r"\b(?:[A-Z]{2,}-\d+|[A-Za-z][A-Za-z0-9]*(?:::[A-Za-z][A-Za-z0-9]*)+|"
    r"[A-Za-z0-9_.-]+\.(?:pdf|docx|xlsx|md|txt)|"
    r"\d+(?:\.\d+)+|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+)\b",
    re.IGNORECASE,
)
_MAX_REWRITE_ATTEMPTS = 3


class QueryRewriteOutput(FrozenModel):
    retrieval_query: StrictStr

    _nonblank = field_validator("retrieval_query")(
        lambda value: value if value.strip() else (_ for _ in ()).throw(ValueError())
    )


class QueryRewriteProvider(Protocol):
    async def rewrite(
        self,
        query: str,
        *,
        now: str,
        timezone_name: str,
    ) -> QueryRewriteOutput: ...


@dataclass(frozen=True)
class QueryConstructionResult:
    request: LocalRetrievalRequest
    attempts: int
    used_fallback: bool


def protected_terms(query: str) -> tuple[str, ...]:
    terms = {match.group(0).casefold() for match in _PROTECTED.finditer(query)}
    return tuple(sorted(terms))


class QueryConstructionService:
    def __init__(
        self,
        *,
        provider: QueryRewriteProvider,
        clock: Callable[[], datetime],
        timezone: timezone = UTC,
        max_attempts: int = _MAX_REWRITE_ATTEMPTS,
    ) -> None:
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError("rewrite attempts must be within [1, 3]")
        self._provider = provider
        self._clock = clock
        self._timezone = timezone
        self._max_attempts = max_attempts

    async def construct(self, request: UserTurnRequest) -> QueryConstructionResult:
        request = UserTurnRequest.model_validate(request)
        terms = protected_terms(request.query)
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("query construction clock must be timezone-aware")
        last_recoverable: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                output = await self._provider.rewrite(
                    request.query,
                    now=now.isoformat(),
                    timezone_name=self._timezone.tzname(now) or "UTC",
                )
            except (DependencyError, ProviderResponseError, ValueError) as error:
                last_recoverable = error
                continue
            rewritten = output.retrieval_query.strip()
            if not rewritten:
                last_recoverable = ValueError("empty rewrite")
                continue
            folded = rewritten.casefold()
            if all(term in folded for term in terms):
                return QueryConstructionResult(
                    request=LocalRetrievalRequest(
                        original_query=request.query,
                        retrieval_query=rewritten,
                        filters=request.filters,
                    ),
                    attempts=attempt,
                    used_fallback=False,
                )
            last_recoverable = ValueError("protected term missing")
        del last_recoverable
        return QueryConstructionResult(
            request=LocalRetrievalRequest(
                original_query=request.query,
                retrieval_query=request.query,
                filters=request.filters,
            ),
            attempts=self._max_attempts,
            used_fallback=True,
        )
