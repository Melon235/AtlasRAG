"""Immutable canonical parsed-document contracts."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import (
    BeforeValidator,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from atlasrag.domain.base import (
    FrozenModel,
    validate_ordered_collection_input,
    validate_string_enum_input,
)
from atlasrag.domain.ingestion.elements import (
    CanonicalElement,
    normalize_canonical_text,
    normalize_single_line,
)
from atlasrag.domain.ingestion.enums import ParseOutcome, ParseReliability
from atlasrag.domain.ingestion.source import (
    CanonicalSource,
    derive_document_id,
    validate_document_id,
    validate_revision_id,
)

_STABLE_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ParseOutcome = Annotated[ParseOutcome, BeforeValidator(validate_string_enum_input)]
_ParseReliability = Annotated[
    ParseReliability, BeforeValidator(validate_string_enum_input)
]
_OrderedStrings = Annotated[
    tuple[StrictStr, ...], BeforeValidator(validate_ordered_collection_input)
]
_Elements = Annotated[
    tuple[CanonicalElement, ...], BeforeValidator(validate_ordered_collection_input)
]


def _stable_reason_code(value: str) -> str:
    if _STABLE_REASON_CODE.fullmatch(value) is None:
        raise ValueError("value must be a stable reason code")
    return value


class ParseInfo(FrozenModel):
    """Stable parser outcome without exceptions or provider payloads."""

    outcome: _ParseOutcome
    reliability: _ParseReliability
    parser_name: StrictStr
    parser_version: StrictStr
    warning_codes: _OrderedStrings = ()
    failure_code: StrictStr | None = None

    @field_validator("parser_name", "parser_version")
    @classmethod
    def _canonical_parser_identity(cls, value: str) -> str:
        return normalize_single_line(value)

    @field_validator("warning_codes")
    @classmethod
    def _canonical_warning_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        validated = tuple(_stable_reason_code(item) for item in value)
        if len(set(validated)) != len(validated):
            raise ValueError("warning_codes must be unique")
        return validated

    @field_validator("failure_code")
    @classmethod
    def _canonical_failure_code(cls, value: str | None) -> str | None:
        return None if value is None else _stable_reason_code(value)

    @model_validator(mode="after")
    def _outcome_reliability_and_codes(self) -> Self:
        pair = (self.outcome, self.reliability)
        allowed = {
            (ParseOutcome.SUCCESS, ParseReliability.RELIABLE),
            (ParseOutcome.SUCCESS, ParseReliability.PARTIAL),
            (ParseOutcome.DEGRADED, ParseReliability.PARTIAL),
            (ParseOutcome.FAILED, ParseReliability.UNAVAILABLE),
        }
        if pair not in allowed:
            raise ValueError("parse outcome and reliability combination is invalid")

        if self.outcome is ParseOutcome.FAILED:
            if self.failure_code is None:
                raise ValueError("FAILED parse requires failure_code")
        elif self.failure_code is not None:
            raise ValueError("usable parse must not contain failure_code")

        if self.reliability is ParseReliability.PARTIAL and not self.warning_codes:
            raise ValueError("PARTIAL parse requires at least one warning code")
        return self


class DocumentMetadata(FrozenModel):
    """Typed stable document facts retained after provider normalization."""

    title: StrictStr | None = None
    authors: _OrderedStrings = ()
    language: StrictStr | None = None
    page_count: Annotated[StrictInt, Field(ge=1)] | None = None
    sheet_names: _OrderedStrings = ()

    @field_validator("title")
    @classmethod
    def _canonical_title(cls, value: str | None) -> str | None:
        return None if value is None else normalize_canonical_text(value)

    @field_validator("language")
    @classmethod
    def _canonical_language(cls, value: str | None) -> str | None:
        return None if value is None else normalize_single_line(value)

    @field_validator("authors", "sheet_names")
    @classmethod
    def _canonical_ordered_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_single_line(item) for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("metadata ordered values must be unique")
        return normalized


class DocumentContainer(FrozenModel):
    """Provider-neutral canonical result for one parse attempt."""

    schema_version: Literal[1] = 1
    document_id: StrictStr
    revision_id: StrictStr
    source: CanonicalSource
    metadata: DocumentMetadata
    parse_info: ParseInfo
    elements: _Elements

    _canonical_document_id = field_validator("document_id")(validate_document_id)
    _canonical_revision_id = field_validator("revision_id")(validate_revision_id)

    @model_validator(mode="after")
    def _identity_parse_and_element_consistency(self) -> Self:
        if self.document_id != derive_document_id(self.source.relative_source_path):
            raise ValueError("document_id does not match canonical source")

        element_ids = [element.element_id for element in self.elements]
        if len(set(element_ids)) != len(element_ids):
            raise ValueError("element_id values must be unique")
        orders = [element.order for element in self.elements]
        if any(left >= right for left, right in zip(orders, orders[1:])):
            raise ValueError("element order must be strictly increasing")
        if any(element.document_id != self.document_id for element in self.elements):
            raise ValueError("element document_id must match document")
        if any(element.revision_id != self.revision_id for element in self.elements):
            raise ValueError("element revision_id must match document")

        if self.parse_info.outcome is ParseOutcome.FAILED:
            if self.elements:
                raise ValueError("failed parse must not contain elements")
        elif not self.elements:
            raise ValueError("usable parse requires nonempty elements")
        return self
