"""Web search provider protocol at the service boundary."""

from __future__ import annotations

from typing import Protocol

from atlasrag.domain.web import WebSearchResult


class SearchProvider(Protocol):
    async def search(self, query: str) -> tuple[WebSearchResult, ...]: ...
