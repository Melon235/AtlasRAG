"""SearXNG JSON search provider normalization tests."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from atlasrag.domain.errors import DependencyUnavailableError, ProviderResponseError
from atlasrag.providers.web.searxng import SearxngSearchProvider


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_search_preserves_rank_and_derives_domain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "json"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "First", "url": "https://a.example/x", "content": "one"},
                    {"title": "Second", "url": "https://b.example/y", "content": "two"},
                ]
            },
        )

    async with client(handler) as http:
        results = await SearxngSearchProvider(
            base_url="http://search.test/", client=http
        ).search("q")
    assert [item.search_rank for item in results] == [1, 2]
    assert [str(item.domain) for item in results] == ["a.example", "b.example"]


@pytest.mark.asyncio
async def test_search_zero_results_is_empty_not_error() -> None:
    async with client(
        lambda request: httpx.Response(200, json={"results": []})
    ) as http:
        assert (
            await SearxngSearchProvider(
                base_url="http://search.test/", client=http
            ).search("q")
            == ()
        )


@pytest.mark.asyncio
async def test_search_malformed_response_and_unavailable() -> None:
    async with client(
        lambda request: httpx.Response(200, json={"results": "no"})
    ) as http:
        with pytest.raises(ProviderResponseError):
            await SearxngSearchProvider(
                base_url="http://search.test/", client=http
            ).search("q")
    async with client(lambda request: httpx.Response(503)) as http:
        with pytest.raises(DependencyUnavailableError):
            await SearxngSearchProvider(
                base_url="http://search.test/", client=http, max_retries=0
            ).search("q")


@pytest.mark.asyncio
async def test_search_retries_bounded_transient_failure() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={"results": [{"title": "ok", "url": "https://a.example/x"}]},
        )

    async with client(handler) as http:
        results = await SearxngSearchProvider(
            base_url="http://search.test/", client=http, max_retries=1
        ).search("q")
    assert results and len(calls) == 2
