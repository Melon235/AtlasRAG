"""Web evidence subgraph State schemas."""

from typing import NotRequired, TypedDict

from atlasrag.domain.web import (
    PreparedWebSource,
    WebEvidenceRequest,
    WebEvidenceResult,
    WebSearchResult,
)


class WebEvidenceSubgraphInput(TypedDict):
    """Input admitted by one Web evidence acquisition attempt."""

    request: WebEvidenceRequest


class WebEvidenceState(TypedDict):
    """Minimal execution data owned by Web evidence acquisition."""

    request: WebEvidenceRequest
    search_results: NotRequired[tuple[WebSearchResult, ...]]
    prepared_sources: NotRequired[tuple[PreparedWebSource, ...]]
    result: NotRequired[WebEvidenceResult]


class WebEvidenceSubgraphOutput(TypedDict):
    """Business result returned by Web evidence acquisition."""

    result: WebEvidenceResult
