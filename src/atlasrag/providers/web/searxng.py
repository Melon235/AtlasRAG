"""SearXNG native JSON search provider (MCP transport deferred by approval)."""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from pydantic import HttpUrl

from atlasrag.domain.errors import (
    DependencyTimeoutError,
    DependencyUnavailableError,
    ProviderResponseError,
)
from atlasrag.domain.web import WebSearchResult
from atlasrag.providers.web.base import SearchProvider

_MAX_TITLE_CHARS = 500
_MAX_SNIPPET_CHARS = 2_000


def _bounded(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:maximum]


class SearxngSearchProvider(SearchProvider):
    """Stable provider boundary around the configured SearXNG JSON endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        client: httpx.AsyncClient,
        top_k: int = 5,
        max_retries: int = 2,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("SearXNG base URL must be HTTP without userinfo")
        if type(top_k) is not int or not 1 <= top_k <= 50:
            raise ValueError("top_k must be between 1 and 50")
        if type(max_retries) is not int or not 0 <= max_retries <= 5:
            raise ValueError("max_retries must be between 0 and 5")
        self._base_url = base_url if base_url.endswith("/") else f"{base_url}/"
        self._client = client
        self._top_k = top_k
        self._max_retries = max_retries

    async def search(self, query: str) -> tuple[WebSearchResult, ...]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search query must be nonempty text")
        url = urljoin(
            self._base_url,
            "search?"
            + urlencode(
                {"q": query, "format": "json", "language": "en", "safesearch": "0"}
            ),
        )
        response: httpx.Response | None = None
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(
                    url,
                    timeout=httpx.Timeout(5.0, connect=3.0),
                    headers={"Accept": "application/json"},
                )
                if response.status_code in {429, 500, 502, 503, 504} and (
                    attempt < self._max_retries
                ):
                    await asyncio.sleep(min(0.25 * (attempt + 1), 1.0))
                    continue
                if response.status_code >= 400:
                    raise DependencyUnavailableError
                break
            except (httpx.TimeoutException, httpx.TransportError) as error:
                last_error = error
                if attempt < self._max_retries:
                    await asyncio.sleep(min(0.25 * (attempt + 1), 1.0))
                    continue
        if response is None:
            if isinstance(last_error, httpx.TimeoutException):
                raise DependencyTimeoutError from None
            raise DependencyUnavailableError from None
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise ProviderResponseError from None
        if not isinstance(payload, dict):
            raise ProviderResponseError
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise ProviderResponseError
        results: list[WebSearchResult] = []
        for raw in raw_results:
            if not isinstance(raw, dict):
                continue
            title = _bounded(raw.get("title"), _MAX_TITLE_CHARS)
            url_text = raw.get("url")
            if title is None or not isinstance(url_text, str):
                continue
            try:
                host = urlsplit(url_text).hostname
                if host is None:
                    continue
                result = WebSearchResult(
                    title=title,
                    url=HttpUrl(url_text),
                    domain=host,
                    snippet=_bounded(raw.get("content"), _MAX_SNIPPET_CHARS),
                    search_rank=len(results) + 1,
                )
            except (ValueError, TypeError):
                continue
            results.append(result)
            if len(results) >= self._top_k:
                break
        if raw_results and not results:
            raise ProviderResponseError
        return tuple(results)
