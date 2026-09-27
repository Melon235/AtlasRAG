"""Structured trace records kept separate from graph execution State."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Self, TypeAlias
from unicodedata import normalize

from pydantic import (
    BeforeValidator,
    Field,
    PlainSerializer,
    PlainValidator,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from atlasrag._canonical import validate_unicode_scalar_text
from atlasrag.domain.base import (
    FrozenModel,
    StrictReal,
    validate_string_enum_input,
)
from atlasrag.domain.enums import TraceStatus
from atlasrag.domain.errors import ErrorCode

_JsonScalar: TypeAlias = None | bool | int | float | str
if TYPE_CHECKING:
    _FrozenJsonValue: TypeAlias = (
        _JsonScalar | tuple["_FrozenJsonValue", ...] | Mapping[str, "_FrozenJsonValue"]
    )
    _FrozenJsonObject: TypeAlias = Mapping[str, _FrozenJsonValue]
else:
    _FrozenJsonValue = object
    _FrozenJsonObject = Mapping[str, object]

_SENSITIVE_KEYS = frozenset(
    {
        "chainofthought",
        "cot",
        "systemprompt",
        "rawevidence",
        "fullevidence",
        "evidencecontent",
        "rawhtml",
        "apikey",
        "authorization",
        "password",
        "secret",
        "accesstoken",
        "refreshtoken",
        "privatekey",
        "authheader",
        "bearertoken",
        "clientcredentials",
        "clientsecret",
        "systeminstruction",
    }
)


def _normalize_attribute_key(key: str) -> str:
    compatible = normalize("NFKC", key)
    return "".join(
        character for character in compatible.casefold() if character.isalnum()
    )


def _validate_attribute_key(key: object) -> str:
    if not isinstance(key, str):
        raise ValueError("attribute keys must be strings")
    validate_unicode_scalar_text(key)
    if not key.strip():
        raise ValueError("attribute keys must not be blank")
    normalized = _normalize_attribute_key(key)
    if normalized in _SENSITIVE_KEYS:
        raise ValueError(f"sensitive trace attribute key is not permitted: {key}")
    return key


def _freeze_json_value(value: object, active_containers: set[int]) -> _FrozenJsonValue:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return validate_unicode_scalar_text(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("trace attribute numbers must be finite")
        return value
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_containers:
            raise ValueError("trace attributes must not contain cycles")
        active_containers.add(identity)
        try:
            return tuple(_freeze_json_value(item, active_containers) for item in value)
        finally:
            active_containers.remove(identity)
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_containers:
            raise ValueError("trace attributes must not contain cycles")
        active_containers.add(identity)
        try:
            frozen: dict[str, _FrozenJsonValue] = {}
            for raw_key, item in value.items():
                key = _validate_attribute_key(raw_key)
                frozen[key] = _freeze_json_value(item, active_containers)
            return MappingProxyType(frozen)
        finally:
            active_containers.remove(identity)
    raise ValueError("trace attribute values must be JSON-compatible")


def _freeze_attributes(value: object) -> _FrozenJsonObject:
    if not isinstance(value, Mapping):
        raise ValueError("attributes must be a JSON object")
    frozen = _freeze_json_value(value, set())
    if not isinstance(frozen, Mapping):
        raise ValueError("attributes must be a JSON object")
    return frozen


def _thaw_json_value(value: _FrozenJsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def _thaw_attributes(value: _FrozenJsonObject) -> dict[str, object]:
    return {key: _thaw_json_value(item) for key, item in value.items()}


_TraceAttributes = Annotated[
    _FrozenJsonObject,
    PlainValidator(_freeze_attributes, json_schema_input_type=dict[str, object]),
    PlainSerializer(_thaw_attributes, return_type=dict[str, object]),
]
_TraceStatusInput = Annotated[TraceStatus, BeforeValidator(validate_string_enum_input)]
_ErrorCodeInput = Annotated[ErrorCode, BeforeValidator(validate_string_enum_input)]
_NonnegativeStrictInt = Annotated[StrictInt, Field(ge=0)]
_NonnegativeReal = Annotated[StrictReal, Field(ge=0)]
_SECONDS_PER_DAY = 86_400
_MICROSECONDS_PER_SECOND = 1_000_000


def _validate_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return validate_unicode_scalar_text(value)


def _validate_optional_nonblank(value: str | None) -> str | None:
    if value is not None:
        _validate_nonblank(value)
    return value


def _absolute_instant_key(value: datetime) -> int:
    """Represent an aware datetime as exact UTC-relative microseconds."""
    offset = value.utcoffset()
    if offset is None:
        raise ValueError("timestamp must be timezone-aware")
    wall_seconds = (
        (value.toordinal() - 1) * _SECONDS_PER_DAY
        + value.hour * 3_600
        + value.minute * 60
        + value.second
    )
    wall_microseconds = wall_seconds * _MICROSECONDS_PER_SECOND + value.microsecond
    offset_microseconds = (
        offset.days * _SECONDS_PER_DAY + offset.seconds
    ) * _MICROSECONDS_PER_SECOND + offset.microseconds
    return wall_microseconds - offset_microseconds


class SpanRecord(FrozenModel):
    """One immutable span in a request's structured execution lineage."""

    trace_id: StrictStr
    span_id: StrictStr
    parent_span_id: StrictStr | None = None
    stage: StrictStr
    attempt: _NonnegativeStrictInt
    started_at: datetime
    ended_at: datetime
    duration_ms: _NonnegativeReal
    status: _TraceStatusInput
    error_code: _ErrorCodeInput | None = None
    attributes: _TraceAttributes

    _nonblank_required_text = field_validator("trace_id", "span_id", "stage")(
        _validate_nonblank
    )
    _nonblank_parent_span_id = field_validator("parent_span_id")(
        _validate_optional_nonblank
    )

    @field_validator("started_at", "ended_at")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _ordered_timestamps(self) -> Self:
        if _absolute_instant_key(self.ended_at) < _absolute_instant_key(
            self.started_at
        ):
            raise ValueError("ended_at must be greater than or equal to started_at")
        return self
