"""Web search, preparation, evidence strategies, and one acquisition pass."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from atlasrag.domain.enums import (
    WebContentOrigin,
    WebEvidenceRepresentation,
    WebExecutionStatus,
)
from atlasrag.domain.errors import DependencyError, ProviderResponseError
from atlasrag.domain.web import (
    PreparedWebSource,
    WebEvidence,
    WebEvidenceRequest,
    WebEvidenceResult,
    WebSearchResult,
)
from atlasrag.providers.web.fetch import (
    SafeHttpFetcher,
    UnsafeWebUrlError,
    WebFetchResponseError,
)
from atlasrag.providers.web.sanitize import sanitize_html, sanitize_text
from atlasrag.services.web_evidence.profile import WebEvidenceProfile


@dataclass(frozen=True)
class PreparationOutcome:
    sources: tuple[PreparedWebSource, ...]
    degraded: bool


@dataclass(frozen=True)
class _PreparedOne:
    source: PreparedWebSource | None
    degraded: bool


class WebPreparationService:
    def __init__(
        self,
        *,
        fetcher: SafeHttpFetcher,
        profile: WebEvidenceProfile,
    ) -> None:
        self._fetcher = fetcher
        self._profile = profile

    async def prepare(self, results: tuple[WebSearchResult, ...]) -> PreparationOutcome:
        semaphore = asyncio.Semaphore(self._profile.max_concurrent_fetches)

        async def prepare_one(result: WebSearchResult) -> _PreparedOne:
            async with semaphore:
                return await self._prepare_one(result)

        outcomes = await asyncio.gather(*(prepare_one(result) for result in results))
        degraded = any(item.degraded for item in outcomes)
        selected: list[PreparedWebSource] = []
        total = 0
        for outcome in outcomes:
            source = outcome.source
            if source is None:
                continue
            remaining = self._profile.total_content_chars - total
            if remaining <= 0:
                degraded = True
                break
            content = source.content[:remaining].rstrip()
            if not content:
                degraded = True
                continue
            if len(content) < len(source.content):
                degraded = True
            selected.append(source.model_copy(update={"content": content}))
            total += len(content)
        return PreparationOutcome(sources=tuple(selected), degraded=degraded)

    async def _prepare_one(self, result: WebSearchResult) -> _PreparedOne:
        try:
            title_text = sanitize_text(
                result.title, max_chars=self._profile.max_title_chars
            )
        except ValueError:
            return _PreparedOne(source=None, degraded=True)
        try:
            page = await self._fetcher.fetch(
                str(result.url),
                timeout_seconds=self._profile.fetch_timeout_seconds,
            )
            sanitized = sanitize_html(
                page.content, max_chars=self._profile.max_page_chars
            )
            fetched_title = sanitized.title.strip()
            title = (
                fetched_title[: self._profile.max_title_chars]
                if fetched_title
                else title_text
            )
            return _PreparedOne(
                source=PreparedWebSource(
                    title=title,
                    url=result.url,
                    domain=result.domain,
                    search_rank=result.search_rank,
                    content=sanitized.content,
                    content_origin=WebContentOrigin.FETCHED_PAGE,
                ),
                degraded=False,
            )
        except UnsafeWebUrlError:
            # A security rejection must not be reintroduced through snippet
            # identity; drop the source.
            return _PreparedOne(source=None, degraded=True)
        except (DependencyError, WebFetchResponseError, ValueError, OSError):
            if result.snippet is None:
                return _PreparedOne(source=None, degraded=True)
            try:
                snippet = sanitize_text(
                    result.snippet, max_chars=self._profile.max_snippet_chars
                )
            except ValueError:
                return _PreparedOne(source=None, degraded=True)
            return _PreparedOne(
                source=PreparedWebSource(
                    title=title_text,
                    url=result.url,
                    domain=result.domain,
                    search_rank=result.search_rank,
                    content=snippet,
                    content_origin=WebContentOrigin.SEARCH_SNIPPET,
                ),
                degraded=True,
            )


class WebEvidenceStrategy(Protocol):
    async def build(
        self,
        request: WebEvidenceRequest,
        sources: tuple[PreparedWebSource, ...],
        *,
        degraded: bool,
    ) -> WebEvidenceResult: ...


class DirectWebEvidenceStrategy:
    async def build(
        self,
        request: WebEvidenceRequest,
        sources: tuple[PreparedWebSource, ...],
        *,
        degraded: bool,
    ) -> WebEvidenceResult:
        if not sources:
            return WebEvidenceResult(
                execution_status=WebExecutionStatus.EMPTY, evidence=()
            )
        evidence = tuple(
            WebEvidence(
                title=source.title,
                url=source.url,
                domain=source.domain,
                search_rank=source.search_rank,
                content=source.content,
                representation=(
                    WebEvidenceRepresentation.SEARCH_SNIPPET
                    if source.content_origin is WebContentOrigin.SEARCH_SNIPPET
                    else WebEvidenceRepresentation.SANITIZED_CONTENT
                ),
            )
            for source in sources
        )
        return WebEvidenceResult(
            execution_status=(
                WebExecutionStatus.DEGRADED if degraded else WebExecutionStatus.OK
            ),
            evidence=evidence,
        )


class SummaryProvider(Protocol):
    async def summarize(
        self, query: str, sources: tuple[PreparedWebSource, ...]
    ) -> tuple[str, ...]: ...


class BatchSummaryWebEvidenceStrategy:
    """One batch summary call; untrusted identity always comes from sources."""

    def __init__(
        self, *, provider: SummaryProvider, max_summary_chars: int = 2_000
    ) -> None:
        if type(max_summary_chars) is not int or max_summary_chars < 1:
            raise ValueError("max_summary_chars must be positive")
        self._provider = provider
        self._max_summary_chars = max_summary_chars

    async def build(
        self,
        request: WebEvidenceRequest,
        sources: tuple[PreparedWebSource, ...],
        *,
        degraded: bool,
    ) -> WebEvidenceResult:
        if not sources:
            return WebEvidenceResult(
                execution_status=WebExecutionStatus.EMPTY, evidence=()
            )
        try:
            summaries = await self._provider.summarize(request.original_query, sources)
        except (DependencyError, ProviderResponseError):
            summaries = ()
        if len(summaries) != len(sources) or any(
            not item.strip() for item in summaries
        ):
            direct = await DirectWebEvidenceStrategy().build(
                request, sources, degraded=True
            )
            return direct.model_copy(
                update={"execution_status": WebExecutionStatus.DEGRADED}
            )
        evidence = tuple(
            WebEvidence(
                title=source.title,
                url=source.url,
                domain=source.domain,
                search_rank=source.search_rank,
                content=summary[: self._max_summary_chars],
                representation=WebEvidenceRepresentation.SUMMARY,
            )
            for source, summary in zip(sources, summaries, strict=True)
        )
        return WebEvidenceResult(
            execution_status=(
                WebExecutionStatus.DEGRADED if degraded else WebExecutionStatus.OK
            ),
            evidence=evidence,
        )


def preparation_degraded(
    results: tuple[WebSearchResult, ...],
    sources: tuple[PreparedWebSource, ...],
    *,
    total_budget: int,
) -> bool:
    """Derive degradation without adding a non-frozen State field."""
    return (
        len(sources) < len(results)
        or any(
            source.content_origin is WebContentOrigin.SEARCH_SNIPPET
            for source in sources
        )
        or sum(len(source.content) for source in sources) >= total_budget
    )
