"""Web search, preparation, and evidence contracts."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import (
    BeforeValidator,
    Field,
    HttpUrl,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from atlasrag.domain.base import (
    FrozenModel,
    validate_ordered_collection_input,
    validate_string_enum_input,
)
from atlasrag.domain.enums import (
    WebContentOrigin,
    WebEvidenceRepresentation,
    WebExecutionStatus,
)

_WebContentOriginInput = Annotated[
    WebContentOrigin, BeforeValidator(validate_string_enum_input)
]
_WebEvidenceRepresentationInput = Annotated[
    WebEvidenceRepresentation, BeforeValidator(validate_string_enum_input)
]
_WebExecutionStatusInput = Annotated[
    WebExecutionStatus, BeforeValidator(validate_string_enum_input)
]

_HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)


def _validate_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _validate_http_url_input(value: object) -> object:
    if not isinstance(value, (str, HttpUrl)):
        raise ValueError("url input must be text or an HttpUrl")
    return value


_HttpUrlInput = Annotated[HttpUrl, BeforeValidator(_validate_http_url_input)]


class WebEvidenceRequest(FrozenModel):
    """Original user query supplied to Web evidence retrieval."""

    original_query: StrictStr

    _nonblank_original_query = field_validator("original_query")(_validate_nonblank)


class _WebSourceBase(FrozenModel):
    """Shared URL identity validation for public Web source DTOs."""

    title: StrictStr
    url: _HttpUrlInput
    domain: StrictStr

    _nonblank_title = field_validator("title")(_validate_nonblank)

    @field_validator("url")
    @classmethod
    def _safe_canonical_url(cls, value: HttpUrl) -> HttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("url must not contain userinfo")
        if value.host is None or value.host.endswith("."):
            raise ValueError("url hostname must not have a trailing dot")
        return value

    @field_validator("domain")
    @classmethod
    def _host_only_domain(cls, value: str) -> str:
        _validate_nonblank(value)
        if value != value.strip() or value.endswith("."):
            raise ValueError("domain must use host-only syntax")
        try:
            parsed = _HTTP_URL_ADAPTER.validate_python(f"https://{value}")
            canonical_domain = value.encode("idna").decode("ascii").casefold()
        except (UnicodeError, ValidationError) as error:
            raise ValueError("domain must use host-only syntax") from error
        if parsed.host is None or parsed.host.casefold() != canonical_domain:
            raise ValueError("domain must use host-only syntax")
        return canonical_domain

    @model_validator(mode="after")
    def _domain_matches_url_hostname(self) -> Self:
        if self.url.host is None or self.domain.casefold() != self.url.host.casefold():
            raise ValueError("domain must match the URL hostname")
        return self


class WebSearchResult(_WebSourceBase):
    """One normalized result returned by Web search."""

    snippet: StrictStr | None = None
    search_rank: Annotated[StrictInt, Field(ge=1)]

    @field_validator("snippet")
    @classmethod
    def _nonblank_snippet_if_present(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_nonblank(value)
        return value


class PreparedWebSource(_WebSourceBase):
    """Bounded text prepared from a search result without raw transport data."""

    search_rank: Annotated[StrictInt, Field(ge=1)]
    content: StrictStr
    content_origin: _WebContentOriginInput

    _nonblank_content = field_validator("content")(_validate_nonblank)


class WebEvidence(_WebSourceBase):
    """Canonical Web evidence admitted to the evidence boundary."""

    search_rank: Annotated[StrictInt, Field(ge=1)]
    content: StrictStr
    representation: _WebEvidenceRepresentationInput

    _nonblank_content = field_validator("content")(_validate_nonblank)


class WebEvidenceResult(FrozenModel):
    """Web execution outcome with evidence cardinality invariants."""

    execution_status: _WebExecutionStatusInput
    evidence: Annotated[
        tuple[WebEvidence, ...], BeforeValidator(validate_ordered_collection_input)
    ] = ()

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
