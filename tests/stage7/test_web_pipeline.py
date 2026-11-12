"""Web preparation, strategies, and subgraph status tests."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from pydantic import HttpUrl

from atlasrag.application.web_evidence import WebEvidenceRunner
from atlasrag.domain.enums import (
    WebEvidenceRepresentation,
    WebExecutionStatus,
)
from atlasrag.domain.errors import DependencyUnavailableError, ProviderResponseError
from atlasrag.domain.web import WebEvidenceRequest, WebSearchResult
from atlasrag.graphs.web_evidence.subgraph import WebEvidenceServices
from atlasrag.providers.web.fetch import (
    UnsafeWebUrlError,
    WebFetchResponseError,
)
from atlasrag.services.web_evidence.profile import WebEvidenceProfile
from atlasrag.services.web_evidence.service import (
    BatchSummaryWebEvidenceStrategy,
    DirectWebEvidenceStrategy,
    WebEvidenceStrategy,
    WebPreparationService,
)


class FakeSearch:
    def __init__(
        self,
        results: tuple[WebSearchResult, ...] = (),
        error: BaseException | None = None,
    ) -> None:
        self.results = results
        self.error = error

    async def search(self, query: str) -> tuple[WebSearchResult, ...]:
        if self.error is not None:
            raise self.error
        return self.results


class FakeFetcher:
    def __init__(self, outcomes: dict[str, object]) -> None:
        self.outcomes = outcomes

    async def fetch(self, url: str, *, timeout_seconds: float | None = None) -> object:
        outcome = self.outcomes[url]
        if isinstance(outcome, BaseException):
            raise outcome
        return type("Page", (), {"content": outcome})()


def result(
    url: str, rank: int, *, snippet: str | None = "safe snippet"
) -> WebSearchResult:
    host = url.split("://", 1)[1].split("/", 1)[0]
    return WebSearchResult(
        title=f"Title {rank}",
        url=HttpUrl(url),
        domain=host,
        snippet=snippet,
        search_rank=rank,
    )


def services(
    search: FakeSearch,
    fetcher: FakeFetcher,
    *,
    strategy: WebEvidenceStrategy | None = None,
    profile: WebEvidenceProfile | None = None,
) -> WebEvidenceServices:
    profile = profile or WebEvidenceProfile(max_page_chars=100, total_content_chars=150)
    preparation = WebPreparationService(fetcher=fetcher, profile=profile)  # type: ignore[arg-type]
    return WebEvidenceServices(
        search=search,
        preparation=preparation,
        strategy=strategy or DirectWebEvidenceStrategy(),
        profile=profile,
    )


@pytest.mark.asyncio
async def test_snippet_fallback_and_budget_order() -> None:
    results = (
        result("https://a.example/one", 1),
        result("https://b.example/two", 2),
    )
    fetcher = FakeFetcher(
        {
            "https://a.example/one": "<html><body>" + "A" * 200 + "</body></html>",
            "https://b.example/two": WebFetchResponseError("bad"),
        }
    )
    out = await WebEvidenceRunner(services(FakeSearch(results), fetcher)).run(
        WebEvidenceRequest(original_query="q")
    )
    assert out.execution_status is WebExecutionStatus.DEGRADED
    assert out.evidence[0].representation is WebEvidenceRepresentation.SANITIZED_CONTENT
    assert out.evidence[1].representation is WebEvidenceRepresentation.SEARCH_SNIPPET
    assert out.evidence[1].search_rank == 2
    assert sum(len(item.content) for item in out.evidence) <= 150


@pytest.mark.asyncio
async def test_security_rejection_drops_source_without_snippet_reintroduction() -> None:
    results = (result("https://bad.example/x", 1), result("https://ok.example/y", 2))
    fetcher = FakeFetcher(
        {
            "https://bad.example/x": UnsafeWebUrlError("private"),
            "https://ok.example/y": "<p>usable</p>",
        }
    )
    out = await WebEvidenceRunner(services(FakeSearch(results), fetcher)).run(
        WebEvidenceRequest(original_query="q")
    )
    assert out.execution_status is WebExecutionStatus.DEGRADED
    assert len(out.evidence) == 1
    assert out.evidence[0].domain == "ok.example"


@pytest.mark.asyncio
async def test_search_failure_empty_and_zero_results_are_distinct() -> None:
    unavailable = await WebEvidenceRunner(
        services(FakeSearch(error=DependencyUnavailableError()), FakeFetcher({}))
    ).run(WebEvidenceRequest(original_query="q"))
    assert unavailable.execution_status is WebExecutionStatus.UNAVAILABLE
    with pytest.raises(ProviderResponseError):
        await WebEvidenceRunner(
            services(FakeSearch(error=ProviderResponseError()), FakeFetcher({}))
        ).run(WebEvidenceRequest(original_query="q"))
    zero = await WebEvidenceRunner(services(FakeSearch(()), FakeFetcher({}))).run(
        WebEvidenceRequest(original_query="q")
    )
    assert zero.execution_status is WebExecutionStatus.EMPTY


@pytest.mark.asyncio
async def test_batch_summary_falls_back_to_direct_on_failure() -> None:
    class Summary:
        async def summarize(
            self, query: str, sources: Sequence[object]
        ) -> tuple[str, ...]:
            raise DependencyUnavailableError

    results = (result("https://a.example/one", 1),)
    fetcher = FakeFetcher({"https://a.example/one": "<p>usable</p>"})
    strategy = BatchSummaryWebEvidenceStrategy(provider=Summary())
    out = await WebEvidenceRunner(
        services(FakeSearch(results), fetcher, strategy=strategy)
    ).run(WebEvidenceRequest(original_query="q"))
    assert out.execution_status is WebExecutionStatus.DEGRADED
    assert out.evidence[0].representation is WebEvidenceRepresentation.SANITIZED_CONTENT


@pytest.mark.asyncio
async def test_summary_success_preserves_authoritative_identity() -> None:
    class Summary:
        async def summarize(
            self, query: str, sources: Sequence[object]
        ) -> tuple[str, ...]:
            return tuple("summary" for _ in sources)

    results = (result("https://a.example/one", 1),)
    fetcher = FakeFetcher({"https://a.example/one": "<p>raw</p>"})
    strategy = BatchSummaryWebEvidenceStrategy(provider=Summary())
    out = await WebEvidenceRunner(
        services(FakeSearch(results), fetcher, strategy=strategy)
    ).run(WebEvidenceRequest(original_query="q"))
    assert out.evidence[0].url == results[0].url
    assert out.evidence[0].representation is WebEvidenceRepresentation.SUMMARY


@pytest.mark.asyncio
async def test_snippet_fallback_is_sanitized_and_control_free() -> None:
    results = (
        result(
            "https://a.example/one",
            1,
            snippet="safe\x00<script>bad()</script>snippet",
        ),
    )
    fetcher = FakeFetcher({"https://a.example/one": WebFetchResponseError("bad")})
    out = await WebEvidenceRunner(services(FakeSearch(results), fetcher)).run(
        WebEvidenceRequest(original_query="q")
    )
    assert out.evidence[0].content == "safesnippet"


def test_web_state_contains_no_transport_or_dependency_fields() -> None:
    from atlasrag.graphs.state.web_evidence import WebEvidenceState

    assert set(WebEvidenceState.__annotations__) == {
        "request",
        "search_results",
        "prepared_sources",
        "result",
    }


def test_answer_state_contains_only_execution_data() -> None:
    from atlasrag.graphs.state.answer import AnswerState

    assert set(AnswerState.__annotations__) == {
        "input",
        "working_evidence",
        "generation_attempt_index",
        "generation_result",
        "review_result",
        "final_response",
        "outcome",
    }


@pytest.mark.asyncio
async def test_total_budget_truncation_is_degraded() -> None:
    results = (
        result("https://a.example/one", 1),
        result("https://b.example/two", 2),
    )
    fetcher = FakeFetcher(
        {
            "https://a.example/one": "<p>" + "A" * 400 + "</p>",
            "https://b.example/two": "<p>" + "B" * 400 + "</p>",
        }
    )
    out = await WebEvidenceRunner(
        services(
            FakeSearch(results),
            fetcher,
            profile=WebEvidenceProfile(
                max_page_chars=400,
                total_content_chars=500,
            ),
        )
    ).run(WebEvidenceRequest(original_query="q"))
    assert out.execution_status is WebExecutionStatus.DEGRADED
