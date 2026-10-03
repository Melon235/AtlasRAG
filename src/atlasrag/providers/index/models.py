"""Immutable records admitted at the derived Milvus index boundary."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Self, TypeAlias

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    PlainSerializer,
    PlainValidator,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from atlasrag._canonical import validate_unicode_scalar_text
from atlasrag.domain.base import (
    FrozenModel,
    StrictReal,
    validate_ordered_collection_input,
    validate_string_enum_input,
)
from atlasrag.domain.enums import RetrievalChunkType, SourceType
from atlasrag.domain.errors import InvariantViolationError

MILVUS_IDENTIFIER_MAX_LENGTH = 512
MILVUS_FILE_NAME_MAX_LENGTH = 1_024
MILVUS_CONTENT_MAX_LENGTH = 65_535
MILVUS_ENUM_MAX_LENGTH = 32
MILVUS_JSON_MAX_BYTES = 65_536
MILVUS_DENSE_VECTOR_MIN_DIMENSION = 2
MILVUS_DENSE_VECTOR_MAX_DIMENSION = 32_768


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


def _milvus_text(value: str) -> str:
    value = validate_unicode_scalar_text(value)
    if "\x00" in value:
        raise ValueError("Milvus string and JSON values must not contain NUL")
    return value


def _canonical_identifier(value: str) -> str:
    value = _milvus_text(value)
    if (
        not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(
            "identifier must be a nonblank canonical string without control characters"
        )
    _validate_utf8_size(
        value,
        maximum=MILVUS_IDENTIFIER_MAX_LENGTH,
        label="identifier",
    )
    return value


def _nonblank(value: str) -> str:
    value = _milvus_text(value)
    if not value.strip():
        raise ValueError("value must not be blank")
    return value


def _validate_utf8_size(value: str, *, maximum: int, label: str) -> str:
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} must not exceed {maximum:,} UTF-8 bytes")
    return value


def _file_name(value: str) -> str:
    return _validate_utf8_size(
        _nonblank(value),
        maximum=MILVUS_FILE_NAME_MAX_LENGTH,
        label="file_name",
    )


def _content(value: str) -> str:
    return _validate_utf8_size(
        _nonblank(value),
        maximum=MILVUS_CONTENT_MAX_LENGTH,
        label="content",
    )


def _freeze_json(value: object, active: set[int]) -> JsonValue:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return _milvus_text(value)
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
                    frozen[_milvus_text(key)] = _freeze_json(item, active)
                return MappingProxyType(frozen)
            return tuple(_freeze_json(item, active) for item in value)
        finally:
            active.remove(identity)
    raise ValueError("metadata values must be JSON-compatible")


def _freeze_object(value: object) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ValueError("structured_metadata must be a JSON object")
    result = _freeze_json(value, set())
    assert isinstance(result, Mapping)
    encoded = json.dumps(
        _thaw_object(result),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MILVUS_JSON_MAX_BYTES:
        raise ValueError("structured_metadata must not exceed 65,536 bytes")
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
CanonicalId = Annotated[
    StrictStr,
    Field(max_length=MILVUS_IDENTIFIER_MAX_LENGTH),
    AfterValidator(_canonical_identifier),
]
_FileName = Annotated[
    StrictStr,
    Field(max_length=MILVUS_FILE_NAME_MAX_LENGTH),
    AfterValidator(_file_name),
]
_Content = Annotated[
    StrictStr,
    Field(max_length=MILVUS_CONTENT_MAX_LENGTH),
    AfterValidator(_content),
]
_RetrievalChunkType = Annotated[
    RetrievalChunkType, BeforeValidator(validate_string_enum_input)
]
_SourceType = Annotated[SourceType, BeforeValidator(validate_string_enum_input)]
_DenseVector = Annotated[
    tuple[StrictReal, ...],
    BeforeValidator(validate_ordered_collection_input),
    Field(
        min_length=MILVUS_DENSE_VECTOR_MIN_DIMENSION,
        max_length=MILVUS_DENSE_VECTOR_MAX_DIMENSION,
    ),
]


class MilvusChunkRecord(FrozenModel):
    """One validated retrievable child/table row for the derived index."""

    chunk_id: CanonicalId
    document_id: CanonicalId
    revision_id: CanonicalId
    file_name: _FileName
    parent_id: CanonicalId | None = None
    chunk_type: _RetrievalChunkType
    source_type: _SourceType
    content: _Content
    sparse_text: _Content
    dense_vector: _DenseVector
    structured_metadata: StructuredMetadata = Field(
        default_factory=dict,
        validate_default=True,
    )

    @model_validator(mode="after")
    def _parent_consistency(self) -> Self:
        if self.chunk_type is RetrievalChunkType.TEXT_CHILD and self.parent_id is None:
            raise ValueError("TEXT_CHILD requires parent_id")
        if self.parent_id == self.chunk_id:
            raise ValueError("a chunk cannot be its own parent")
        return self

    @field_validator("dense_vector")
    @classmethod
    def _finite_dense_vector(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if not all(math.isfinite(component) for component in value):
            raise ValueError("dense_vector components must be finite")
        return value


def _validated_embedding_dimension(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MILVUS_DENSE_VECTOR_MIN_DIMENSION
        <= value
        <= MILVUS_DENSE_VECTOR_MAX_DIMENSION
    ):
        raise InvariantViolationError
    return value


def _validated_scope_identifier(value: object) -> str:
    if not isinstance(value, str):
        raise InvariantViolationError
    try:
        return _canonical_identifier(value)
    except ValueError as error:
        raise InvariantViolationError from error


def validate_milvus_record(
    record: MilvusChunkRecord,
    *,
    embedding_dimension: int,
) -> MilvusChunkRecord:
    """Revalidate one typed record and enforce the configured vector dimension."""

    dimension = _validated_embedding_dimension(embedding_dimension)
    if not isinstance(record, MilvusChunkRecord):
        raise InvariantViolationError
    try:
        validated = MilvusChunkRecord.model_validate(record)
    except (ValidationError, TypeError, ValueError) as error:
        raise InvariantViolationError from error
    if len(validated.dense_vector) != dimension:
        raise InvariantViolationError
    return validated


def validate_milvus_record_set(
    records: Sequence[MilvusChunkRecord],
    *,
    document_id: str,
    revision_id: str,
    embedding_dimension: int,
) -> tuple[MilvusChunkRecord, ...]:
    """Validate a complete revision set before any SDK mutation occurs."""

    expected_document_id = _validated_scope_identifier(document_id)
    expected_revision_id = _validated_scope_identifier(revision_id)
    dimension = _validated_embedding_dimension(embedding_dimension)
    if isinstance(records, (str, bytes, bytearray)) or not isinstance(
        records, Sequence
    ):
        raise InvariantViolationError

    validated_records: list[MilvusChunkRecord] = []
    chunk_ids: set[str] = set()
    for record in records:
        validated = validate_milvus_record(
            record,
            embedding_dimension=dimension,
        )
        if (
            validated.document_id != expected_document_id
            or validated.revision_id != expected_revision_id
            or validated.chunk_id in chunk_ids
        ):
            raise InvariantViolationError
        chunk_ids.add(validated.chunk_id)
        validated_records.append(validated)
    return tuple(validated_records)
