"""Structured trace records kept separate from graph execution State."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
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
        "goldlabel",
        "goldanswer",
        "golddocumentid",
        "goldsectionid",
        "goldenanswer",
        "queryid",
        "alignedevidenceids",
        "alignmentconfidence",
        "qrel",
        "expectedanswer",
        "benchmarkcasetype",
        "benchmarkcaseid",
        "leaderboardscore",
        "experimentid",
        "experimentmetadata",
        "candidatestrategyset",
        "candidatestrategysets",
        "evaluationdatabase",
        "evaluationdatabases",
        "metricobject",
        "metricobjects",
    }
)
_SENSITIVE_TOKEN_SEQUENCES = (
    ("chain", "of", "thought"),
    ("system", "prompt"),
    ("raw", "evidence"),
    ("full", "evidence"),
    ("evidence", "content"),
    ("raw", "html"),
    ("api", "key"),
    ("api", "keys"),
    ("authorization",),
    ("password",),
    ("passwords",),
    ("secret",),
    ("secrets",),
    ("access", "token"),
    ("access", "tokens"),
    ("refresh", "token"),
    ("refresh", "tokens"),
    ("private", "key"),
    ("private", "keys"),
    ("auth", "header"),
    ("auth", "headers"),
    ("bearer", "token"),
    ("bearer", "tokens"),
    ("client", "credentials"),
    ("client", "secret"),
    ("client", "secrets"),
    ("system", "instruction"),
    ("system", "instructions"),
    ("gold", "label"),
    ("gold", "answer"),
    ("gold", "document"),
    ("gold", "section"),
    ("gold", "reference"),
    ("gold", "citation"),
    ("gold", "relevance"),
    ("golden", "answer"),
    ("golden", "label"),
    ("qrel",),
    ("qrels",),
    ("expected", "answer"),
    ("expected", "answers"),
    ("benchmark",),
    ("leaderboard", "score"),
    ("leaderboard", "scores"),
    ("experiment",),
    ("experiments",),
    ("candidate", "strategy", "set"),
    ("candidate", "strategy", "sets"),
    ("evaluation", "database"),
    ("evaluation", "databases"),
    ("metric", "object"),
    ("metric", "objects"),
)
_SENSITIVE_NORMALIZED_AFFIXES = frozenset(
    _SENSITIVE_KEYS - {"cot", "password", "queryid", "secret", "qrel"}
)
_SENSITIVE_NORMALIZED_PREFIXES = frozenset({"benchmark", "experiment", "qrel"})
_SENSITIVE_NORMALIZED_SUFFIXES = frozenset(
    {
        "apikey",
        "apikeys",
        "password",
        "passwords",
        "secret",
        "secrets",
        "qrel",
        "qrels",
    }
)
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_TOKEN = re.compile(r"[^\W_]+")


def _normalize_attribute_key(key: str) -> str:
    compatible = normalize("NFKC", key)
    return "".join(
        character for character in compatible.casefold() if character.isalnum()
    )


def _attribute_key_tokens(key: str) -> tuple[str, ...]:
    compatible = normalize("NFKC", key)
    separated = _CAMEL_CASE_BOUNDARY.sub(" ", compatible)
    return tuple(token.casefold() for token in _KEY_TOKEN.findall(separated))


def _contains_token_sequence(
    tokens: tuple[str, ...], sequence: tuple[str, ...]
) -> bool:
    width = len(sequence)
    return any(
        tokens[index : index + width] == sequence
        for index in range(len(tokens) - width + 1)
    )


def _is_sensitive_attribute_key(key: str) -> bool:
    normalized = _normalize_attribute_key(key)
    tokens = _attribute_key_tokens(key)
    return (
        normalized in _SENSITIVE_KEYS
        or any(
            normalized.startswith(fragment) or normalized.endswith(fragment)
            for fragment in _SENSITIVE_NORMALIZED_AFFIXES
        )
        or any(
            normalized.startswith(fragment)
            for fragment in _SENSITIVE_NORMALIZED_PREFIXES
        )
        or any(
            normalized.endswith(fragment) for fragment in _SENSITIVE_NORMALIZED_SUFFIXES
        )
        or any(
            _contains_token_sequence(tokens, sequence)
            for sequence in _SENSITIVE_TOKEN_SEQUENCES
        )
    )


def _validate_attribute_key(key: object) -> str:
    if not isinstance(key, str):
        raise ValueError("attribute keys must be strings")
    validate_unicode_scalar_text(key)
    if not key.strip():
        raise ValueError("attribute keys must not be blank")
    if _is_sensitive_attribute_key(key):
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


def _validate_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return validate_unicode_scalar_text(value)


def _validate_optional_nonblank(value: str | None) -> str | None:
    if value is not None:
        _validate_nonblank(value)
    return value


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
        try:
            return value.astimezone(UTC)
        except OverflowError as exc:
            raise ValueError(
                "timestamp instant is outside Python datetime UTC range"
            ) from exc

    @model_validator(mode="after")
    def _ordered_timestamps(self) -> Self:
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must be greater than or equal to started_at")
        return self
