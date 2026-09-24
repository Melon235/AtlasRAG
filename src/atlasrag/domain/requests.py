"""Typed request and retrieval-scope contracts."""

from __future__ import annotations

from pydantic import field_validator

from atlasrag.domain.base import FrozenModel
from atlasrag.domain.enums import RetrievalChunkType, SourceType


def _validate_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


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

    file_name: str | None = None
    source_type: SourceType | None = None
    sheet_name: str | None = None

    _nonblank_optional_strings = field_validator("file_name", "sheet_name")(
        _validate_optional_nonblank
    )


class UserTurnRequest(FrozenModel):
    """Raw immutable request admitted at the user/session boundary."""

    session_id: str
    query: str
    filters: QueryFilters | None = None

    _nonblank_required_strings = field_validator("session_id", "query")(
        _validate_nonblank
    )


class LocalRetrievalRequest(FrozenModel):
    """Input to local retrieval after query construction."""

    original_query: str
    retrieval_query: str
    filters: QueryFilters | None = None

    _nonblank_queries = field_validator("original_query", "retrieval_query")(
        _validate_nonblank
    )


class InternalRetrievalFilters(FrozenModel):
    """Minimal internal-only retrieval constraints."""

    chunk_types: tuple[RetrievalChunkType, ...] | None = None

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

    file_name: str | None = None
    source_type: SourceType | None = None
    sheet_name: str | None = None
    chunk_types: tuple[RetrievalChunkType, ...]

    _nonblank_optional_strings = field_validator("file_name", "sheet_name")(
        _validate_optional_nonblank
    )
    _nonempty_unique_chunk_types = field_validator("chunk_types")(_validate_chunk_types)
