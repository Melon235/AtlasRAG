"""Web search, preparation, and evidence contracts."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import (
    Field,
    HttpUrl,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from atlasrag.domain.base import FrozenModel
from atlasrag.domain.enums import (
    WebContentOrigin,
    WebEvidenceRepresentation,
    WebExecutionStatus,
)

_HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)


def _validate_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class WebEvidenceRequest(FrozenModel):
    """Original user query supplied to Web evidence retrieval."""

    original_query: str

    _nonblank_original_query = field_validator("original_query")(_validate_nonblank)


class _WebSourceBase(FrozenModel):
    """Shared URL identity validation for public Web source DTOs."""

    title: str
    url: HttpUrl
    domain: str

    _nonblank_title = field_validator("title")(_validate_nonblank)

    @field_validator("domain")
    @classmethod
    def _host_only_domain(cls, value: str) -> str:
        _validate_nonblank(value)
        if value != value.strip():
            raise ValueError("domain must use host-only syntax")
        try:
            parsed = _HTTP_URL_ADAPTER.validate_python(f"https://{value}")
        except ValidationError as error:
            raise ValueError("domain must use host-only syntax") from error
        if parsed.host is None or parsed.host.casefold() != value.casefold():
            raise ValueError("domain must use host-only syntax")
        return value

    @model_validator(mode="after")
    def _domain_matches_url_hostname(self) -> Self:
        if self.url.host is None or self.domain.casefold() != self.url.host.casefold():
            raise ValueError("domain must match the URL hostname")
        return self


class WebSearchResult(_WebSourceBase):
    """One normalized result returned by Web search."""

    snippet: str | None = None
    search_rank: Annotated[int, Field(ge=1)]

    @field_validator("snippet")
    @classmethod
    def _nonblank_snippet_if_present(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_nonblank(value)
        return value


class PreparedWebSource(_WebSourceBase):
    """Bounded text prepared from a search result without raw transport data."""

    search_rank: Annotated[int, Field(ge=1)]
    content: str
    content_origin: WebContentOrigin

    _nonblank_content = field_validator("content")(_validate_nonblank)


class WebEvidence(_WebSourceBase):
    """Canonical Web evidence admitted to the evidence boundary."""

    search_rank: Annotated[int, Field(ge=1)]
    content: str
    representation: WebEvidenceRepresentation

    _nonblank_content = field_validator("content")(_validate_nonblank)


class WebEvidenceResult(FrozenModel):
    """Web execution outcome with evidence cardinality invariants."""

    execution_status: WebExecutionStatus
    evidence: tuple[WebEvidence, ...] = ()

    @model_validator(mode="after")
    def _status_matches_evidence(self) -> Self:
        if self.execution_status in {
            WebExecutionStatus.OK,
            WebExecutionStatus.DEGRADED,
        }:
            if not self.evidence:
                raise ValueError("OK and DEGRADED require non-empty evidence")
        elif self.evidence:
            raise ValueError("EMPTY and UNAVAILABLE require empty evidence")
        return self
