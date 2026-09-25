"""Resolved final citations, responses, and RAG core outcomes."""

from __future__ import annotations

from typing import Annotated, Literal, Self, TypeAlias

from pydantic import (
    BeforeValidator,
    Field,
    HttpUrl,
    StrictStr,
    field_validator,
    model_validator,
)

from atlasrag.domain.base import (
    FrozenModel,
    validate_ordered_collection_input,
    validate_string_enum_input,
)
from atlasrag.domain.enums import (
    EvidenceType,
    RagCompletionStatus,
    SourceKind,
)
from atlasrag.domain.evidence import LocalProvenance, SourceAnchor
from atlasrag.domain.web import _HttpUrlInput, _WebSourceBase

_LocalCitationId = Annotated[StrictStr, Field(pattern=r"^L[1-9][0-9]*$")]
_WebCitationId = Annotated[StrictStr, Field(pattern=r"^W[1-9][0-9]*$")]
_LocalSourceKind = Literal[SourceKind.LOCAL]
_WebSourceKind = Literal[SourceKind.WEB]
_EvidenceTypeInput = Annotated[
    EvidenceType,
    BeforeValidator(validate_string_enum_input),
]
_RagCompletionStatusInput = Annotated[
    RagCompletionStatus,
    BeforeValidator(validate_string_enum_input),
]


def _validate_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class FinalLocalCitation(FrozenModel):
    """Resolved display citation for canonical local evidence."""

    citation_id: _LocalCitationId
    source_kind: _LocalSourceKind = SourceKind.LOCAL
    file_name: StrictStr
    relative_source_path: StrictStr
    evidence_type: _EvidenceTypeInput
    source_anchor: SourceAnchor | None = None

    _nonblank_file_name = field_validator("file_name")(_validate_nonblank)

    @field_validator("relative_source_path")
    @classmethod
    def _canonical_relative_source_path(cls, value: str) -> str:
        return LocalProvenance._canonical_relative_source_path(value)


class FinalWebCitation(FrozenModel):
    """Resolved display citation for canonical Web evidence."""

    citation_id: _WebCitationId
    source_kind: _WebSourceKind = SourceKind.WEB
    title: StrictStr
    url: _HttpUrlInput
    domain: StrictStr

    _nonblank_title = field_validator("title")(_validate_nonblank)

    @field_validator("url")
    @classmethod
    def _safe_canonical_url(cls, value: HttpUrl) -> HttpUrl:
        return _WebSourceBase._safe_canonical_url(value)

    @field_validator("domain")
    @classmethod
    def _host_only_domain(cls, value: str) -> str:
        return _WebSourceBase._host_only_domain(value)

    @model_validator(mode="after")
    def _domain_matches_url_hostname(self) -> Self:
        if self.url.host is None or self.domain.casefold() != self.url.host.casefold():
            raise ValueError("domain must match the URL hostname")
        return self


FinalCitation: TypeAlias = Annotated[
    FinalLocalCitation | FinalWebCitation,
    Field(discriminator="source_kind"),
]


class FinalResponse(FrozenModel):
    """Final answer and only the resolved citations used by that answer."""

    answer_text: StrictStr
    citations: Annotated[
        tuple[FinalCitation, ...],
        BeforeValidator(validate_ordered_collection_input),
    ] = ()

    _nonblank_answer_text = field_validator("answer_text")(_validate_nonblank)

    @field_validator("citations")
    @classmethod
    def _unique_citation_ids(
        cls,
        value: tuple[FinalLocalCitation | FinalWebCitation, ...],
    ) -> tuple[FinalLocalCitation | FinalWebCitation, ...]:
        citation_ids = tuple(citation.citation_id for citation in value)
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("final citation IDs must be globally unique")
        return value


class RagCoreResult(FrozenModel):
    """Completed RAG core result without policy- or transport-derived metadata."""

    final_response: FinalResponse
    completion_status: _RagCompletionStatusInput
