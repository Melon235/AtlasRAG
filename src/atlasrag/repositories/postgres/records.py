"""Immutable storage contracts; transitions belong to later application services."""

from __future__ import annotations

import math
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Self, TypeAlias

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BeforeValidator,
    Field,
    PlainSerializer,
    PlainValidator,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from atlasrag._canonical import validate_unicode_scalar_text
from atlasrag.domain.base import (
    FrozenModel,
    validate_ordered_collection_input,
    validate_string_enum_input,
)
from atlasrag.domain.enums import SourceType
from atlasrag.domain.evidence import LocalProvenance, SourceAnchor


class IngestionStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PARSING = "PARSING"
    PARSED = "PARSED"
    CHUNKING = "CHUNKING"
    CHUNKED = "CHUNKED"
    INDEXING = "INDEXING"
    READY = "READY"
    FAILED = "FAILED"


class FailedStage(StrEnum):
    PARSE = "PARSE"
    CHUNK = "CHUNK"
    REPRESENTATION = "REPRESENTATION"
    EMBEDDING = "EMBEDDING"
    INDEX_WRITE = "INDEX_WRITE"
    INDEX_VERIFY = "INDEX_VERIFY"


class StoredChunkType(StrEnum):
    TEXT_PARENT = "TEXT_PARENT"
    TEXT_CHILD = "TEXT_CHILD"
    TABLE = "TABLE"


# Keep recursive JSON types available to static callers without asking Pydantic
# to synthesize a mutable recursive schema. The plain validator freezes it.
if TYPE_CHECKING:
    JsonValue: TypeAlias = (
        None
        | bool
        | int
        | float
        | str
        | tuple["JsonValue", ...]
        | Mapping[str, "JsonValue"]
    )
    JsonObject: TypeAlias = Mapping[str, JsonValue]
else:
    JsonValue = object
    JsonObject = Mapping[str, object]


def _postgres_text(value: str) -> str:
    value = validate_unicode_scalar_text(value)
    if "\x00" in value:
        raise ValueError("PostgreSQL text and JSON values must not contain NUL")
    return value


def _freeze_json(value: object, active: set[int]) -> JsonValue:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return _postgres_text(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, (Mapping, list, tuple)):
        identity = id(value)
        if identity in active:
            raise ValueError("JSON metadata must not contain cycles")
        active.add(identity)
        try:
            if isinstance(value, Mapping):
                frozen: dict[str, JsonValue] = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ValueError("JSON object keys must be strings")
                    frozen[_postgres_text(key)] = _freeze_json(item, active)
                return MappingProxyType(frozen)
            return tuple(_freeze_json(item, active) for item in value)
        finally:
            active.remove(identity)
    raise ValueError("metadata values must be JSON-compatible")


def _freeze_object(value: object) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ValueError("metadata must be a JSON object")
    result = _freeze_json(value, set())
    assert isinstance(result, Mapping)
    return result


def _thaw_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _thaw_object(value: JsonObject) -> dict[str, object]:
    return {key: _thaw_json(item) for key, item in value.items()}


StructuredMetadata = Annotated[
    JsonObject,
    PlainValidator(_freeze_object, json_schema_input_type=dict[str, object]),
    PlainSerializer(_thaw_object, return_type=dict[str, object]),
]


def _canonical_identifier(value: str) -> str:
    if (
        not value
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(
            "identifier must be a nonblank canonical string without control characters"
        )
    return _postgres_text(value)


def _nonblank(value: str) -> str:
    value = _postgres_text(value)
    if not value.strip():
        raise ValueError("value must not be blank")
    return value


def _postgres_source_anchor(value: SourceAnchor | None) -> SourceAnchor | None:
    if value is not None:
        for text in (value.heading, value.sheet_name, value.cell_range):
            if text is not None:
                _postgres_text(text)
    return value


CanonicalId = Annotated[StrictStr, AfterValidator(_canonical_identifier)]
_NonblankString = Annotated[StrictStr, AfterValidator(_nonblank)]
_PostgresText = Annotated[StrictStr, AfterValidator(_postgres_text)]
_SectionPath = Annotated[
    tuple[_NonblankString, ...], BeforeValidator(validate_ordered_collection_input)
]
_SourceType = Annotated[SourceType, BeforeValidator(validate_string_enum_input)]
_IngestionStatus = Annotated[
    IngestionStatus, BeforeValidator(validate_string_enum_input)
]
_FailedStage = Annotated[FailedStage, BeforeValidator(validate_string_enum_input)]
_StoredChunkType = Annotated[
    StoredChunkType, BeforeValidator(validate_string_enum_input)
]
_SourceAnchor = Annotated[SourceAnchor | None, AfterValidator(_postgres_source_anchor)]


class StoredDocument(FrozenModel):
    """One path's last-good revision and current candidate storage state."""

    document_id: CanonicalId
    file_name: _NonblankString
    relative_source_path: StrictStr
    source_type: _SourceType
    observed_content_hash: CanonicalId
    current_revision_id: CanonicalId | None = None
    current_content_hash: CanonicalId | None = None
    current_pipeline_fingerprint: CanonicalId | None = None
    building_revision_id: CanonicalId | None = None
    building_content_hash: CanonicalId | None = None
    building_pipeline_fingerprint: CanonicalId | None = None
    ingestion_status: _IngestionStatus
    failed_stage: _FailedStage | None = None
    parse_metadata: StructuredMetadata = Field(
        default_factory=dict, validate_default=True
    )
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @field_validator("relative_source_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        return LocalProvenance._canonical_relative_source_path(value)

    @model_validator(mode="after")
    def _revision_and_failure_consistency(self) -> Self:
        for triple in (
            (
                self.current_revision_id,
                self.current_content_hash,
                self.current_pipeline_fingerprint,
            ),
            (
                self.building_revision_id,
                self.building_content_hash,
                self.building_pipeline_fingerprint,
            ),
        ):
            if any(value is None for value in triple) and not all(
                value is None for value in triple
            ):
                raise ValueError(
                    "revision, content hash and fingerprint must be all present or all absent"
                )
        if (self.ingestion_status == IngestionStatus.FAILED) != (
            self.failed_stage is not None
        ):
            raise ValueError(
                "failed_stage is required exactly when ingestion_status is FAILED"
            )
        return self


class StoredElement(FrozenModel):
    """Revision-scoped canonical parsed content and structural location."""

    element_id: CanonicalId
    document_id: CanonicalId
    revision_id: CanonicalId
    element_type: _NonblankString
    order_index: Annotated[StrictInt, Field(ge=0)]
    content: _PostgresText
    section_path: _SectionPath = ()
    source_anchor: _SourceAnchor = None
    structured_content: StructuredMetadata = Field(
        default_factory=dict, validate_default=True
    )
    metadata: StructuredMetadata = Field(default_factory=dict, validate_default=True)
    created_at: AwareDatetime


class StoredChunk(FrozenModel):
    """Canonical full chunk content, including nonretrievable text parents."""

    chunk_id: CanonicalId
    document_id: CanonicalId
    revision_id: CanonicalId
    chunk_type: _StoredChunkType
    parent_id: CanonicalId | None = None
    content: _PostgresText
    section_path: _SectionPath = ()
    source_anchor: _SourceAnchor = None
    sheet_name: _NonblankString | None = None
    metadata: StructuredMetadata = Field(default_factory=dict, validate_default=True)
    strategy_metadata: StructuredMetadata = Field(
        default_factory=dict, validate_default=True
    )
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _parent_consistency(self) -> Self:
        if self.chunk_type == StoredChunkType.TEXT_CHILD and self.parent_id is None:
            raise ValueError("TEXT_CHILD requires parent_id")
        if (
            self.chunk_type == StoredChunkType.TEXT_PARENT
            and self.parent_id is not None
        ):
            raise ValueError("TEXT_PARENT must not have parent_id")
        if self.parent_id == self.chunk_id:
            raise ValueError("a chunk cannot be its own parent")
        return self


class RuntimeMetadata(FrozenModel):
    """Singleton correctness marker persisted with caller-owned transactions."""

    query_cache_invalidation_required: StrictBool = False
