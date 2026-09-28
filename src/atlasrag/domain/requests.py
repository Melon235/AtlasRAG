"""Typed request and retrieval-scope contracts."""

from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator, StrictStr, field_validator

from atlasrag._canonical import validate_unicode_scalar_text
from atlasrag.domain.base import (
    FrozenModel,
    validate_ordered_collection_input,
    validate_string_enum_input,
)
from atlasrag.domain.enums import RetrievalChunkType, SourceType

_SourceTypeInput = Annotated[SourceType, BeforeValidator(validate_string_enum_input)]
_RetrievalChunkTypeInput = Annotated[
    RetrievalChunkType, BeforeValidator(validate_string_enum_input)
]


def _validate_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return validate_unicode_scalar_text(value)


def _validate_optional_nonblank(value: str | None) -> str | None:
    if value is not None:
        _validate_nonblank(value)
    return value


def _validate_chunk_types(
    value: tuple[RetrievalChunkType, ...],
) -> tuple[RetrievalChunkType, ...]:
    if not value:
        raise ValueError("chunk_types must not be empty")
    if len(value) != len(set(value)):
        raise ValueError("chunk_types must not contain duplicates")
    return value


class QueryFilters(FrozenModel):
    """Explicit user-facing metadata filter allowlist."""

    file_name: StrictStr | None = None
    source_type: _SourceTypeInput | None = None
    sheet_name: StrictStr | None = None

    _nonblank_optional_strings = field_validator("file_name", "sheet_name")(
        _validate_optional_nonblank
    )


class UserTurnRequest(FrozenModel):
    """Raw immutable request admitted at the user/session boundary."""

    session_id: StrictStr
    query: StrictStr
    filters: QueryFilters | None = None

    @field_validator("session_id")
    @classmethod
    def _canonical_session_id(cls, value: str) -> str:
        return _validate_nonblank(value).strip()

    _nonblank_query = field_validator("query")(_validate_nonblank)


class LocalRetrievalRequest(FrozenModel):
    """Input to local retrieval after query construction."""

    original_query: StrictStr
    retrieval_query: StrictStr
    filters: QueryFilters | None = None

    _nonblank_queries = field_validator("original_query", "retrieval_query")(
        _validate_nonblank
    )


class InternalRetrievalFilters(FrozenModel):
    """Minimal internal-only retrieval constraints."""

    chunk_types: (
        Annotated[
            tuple[_RetrievalChunkTypeInput, ...],
            BeforeValidator(validate_ordered_collection_input),
        ]
        | None
    ) = None

    @field_validator("chunk_types")
    @classmethod
    def _nonempty_unique_chunk_types(
        cls, value: tuple[RetrievalChunkType, ...] | None
    ) -> tuple[RetrievalChunkType, ...] | None:
        if value is not None:
            _validate_chunk_types(value)
        return value


class ResolvedRetrievalScope(FrozenModel):
    """Flattened deterministic scope consumed by all retrieval branches."""

    file_name: StrictStr | None = None
    source_type: _SourceTypeInput | None = None
    sheet_name: StrictStr | None = None
    chunk_types: Annotated[
        tuple[_RetrievalChunkTypeInput, ...],
        BeforeValidator(validate_ordered_collection_input),
    ]

    _nonblank_optional_strings = field_validator("file_name", "sheet_name")(
        _validate_optional_nonblank
    )
    _nonempty_unique_chunk_types = field_validator("chunk_types")(_validate_chunk_types)
