"""Structured, JSON-safe trace contract tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from atlasrag.domain.enums import TraceStatus
from atlasrag.domain.errors import ErrorCode
from atlasrag.domain.trace import SpanRecord

STARTED_AT = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
ENDED_AT = STARTED_AT + timedelta(milliseconds=12)


def span_record(**changes: object) -> SpanRecord:
    values: dict[str, object] = {
        "trace_id": "trace-1",
        "span_id": "span-1",
        "parent_span_id": None,
        "stage": "local_retrieval",
        "attempt": 0,
        "started_at": STARTED_AT,
        "ended_at": ENDED_AT,
        "duration_ms": 12.0,
        "status": TraceStatus.OK,
        "error_code": None,
        "attributes": {},
    }
    values.update(changes)
    return SpanRecord.model_validate(values)


def span_record_json(started_at: datetime, ended_at: datetime) -> str:
    return json.dumps(
        {
            "trace_id": "trace-1",
            "span_id": "span-1",
            "parent_span_id": None,
            "stage": "local_retrieval",
            "attempt": 0,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "duration_ms": 0,
            "status": "OK",
            "error_code": None,
            "attributes": {},
        }
    )


def test_span_record_has_exact_fields_and_optional_identifiers() -> None:
    assert set(SpanRecord.model_fields) == {
        "trace_id",
        "span_id",
        "parent_span_id",
        "stage",
        "attempt",
        "started_at",
        "ended_at",
        "duration_ms",
        "status",
        "error_code",
        "attributes",
    }

    record = SpanRecord(
        trace_id="trace-1",
        span_id="span-1",
        stage="generation",
        attempt=0,
        started_at=STARTED_AT,
        ended_at=ENDED_AT,
        duration_ms=12,
        status=TraceStatus.OK,
        attributes={},
    )

    assert record.parent_span_id is None
    assert record.error_code is None


def test_attributes_validation_schema_is_an_object() -> None:
    schema = SpanRecord.model_json_schema(mode="validation")

    assert schema["properties"]["attributes"]["type"] == "object"


@pytest.mark.parametrize("field_name", ["trace_id", "span_id", "stage"])
@pytest.mark.parametrize("invalid_value", ["", " \t", b"bytes", 123])
def test_required_trace_text_is_strict_and_nonblank(
    field_name: str, invalid_value: object
) -> None:
    with pytest.raises(ValidationError):
        span_record(**{field_name: invalid_value})


@pytest.mark.parametrize("invalid_value", ["", " \t", b"bytes", 123])
def test_parent_span_id_is_nonblank_when_present(invalid_value: object) -> None:
    with pytest.raises(ValidationError):
        span_record(parent_span_id=invalid_value)


@pytest.mark.parametrize(
    "field_name", ["trace_id", "span_id", "parent_span_id", "stage"]
)
def test_trace_text_fields_reject_unicode_surrogates(field_name: str) -> None:
    with pytest.raises(ValidationError, match="surrogate"):
        span_record(**{field_name: f"invalid-\ud800-{field_name}"})


@pytest.mark.parametrize("field_name", ["status", "error_code"])
def test_trace_enum_text_rejects_unicode_surrogates(field_name: str) -> None:
    with pytest.raises(ValidationError):
        span_record(**{field_name: "invalid-\ud800-enum"})


@pytest.mark.parametrize("invalid_value", [-1, True, "1", 1.0])
def test_attempt_is_a_strict_nonnegative_integer(invalid_value: object) -> None:
    with pytest.raises(ValidationError):
        span_record(attempt=invalid_value)


@pytest.mark.parametrize(
    "invalid_value", [-0.01, True, "1", float("nan"), float("inf")]
)
def test_duration_is_a_finite_nonnegative_real_number(invalid_value: object) -> None:
    with pytest.raises(ValidationError):
        span_record(duration_ms=invalid_value)


def test_span_timestamps_must_be_timezone_aware_and_ordered() -> None:
    naive = datetime(2026, 9, 26, 10, 0)

    with pytest.raises(ValidationError, match="timezone-aware"):
        span_record(started_at=naive)
    with pytest.raises(ValidationError, match="timezone-aware"):
        span_record(ended_at=naive)
    with pytest.raises(ValidationError, match="ended_at"):
        span_record(ended_at=STARTED_AT - timedelta(microseconds=1))


def test_equal_span_timestamps_are_allowed() -> None:
    record = span_record(ended_at=STARTED_AT, duration_ms=0)

    assert record.ended_at == record.started_at
    assert record.duration_ms == 0.0


def test_span_timestamp_order_uses_absolute_instants_across_dst_fold() -> None:
    new_york = ZoneInfo("America/New_York")
    started_at = datetime(2026, 11, 1, 1, 30, tzinfo=new_york, fold=1)
    ended_at = datetime(2026, 11, 1, 1, 45, tzinfo=new_york, fold=0)

    assert started_at.astimezone(UTC) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert ended_at.astimezone(UTC) == datetime(2026, 11, 1, 5, 45, tzinfo=UTC)
    with pytest.raises(ValidationError, match="ended_at"):
        span_record(started_at=started_at, ended_at=ended_at)


def test_span_timestamp_order_allows_normal_cross_timezone_instants() -> None:
    started_at = datetime(2026, 9, 26, 18, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    ended_at = datetime(2026, 9, 26, 5, 30, tzinfo=ZoneInfo("America/New_York"))

    record = span_record(started_at=started_at, ended_at=ended_at)

    assert record.started_at == started_at
    assert record.ended_at == ended_at


@pytest.mark.parametrize(
    "boundary",
    [
        datetime.min.replace(tzinfo=timezone(timedelta(hours=14))),
        datetime.max.replace(tzinfo=timezone(-timedelta(hours=12))),
    ],
)
def test_extreme_offset_timestamps_outside_utc_range_are_rejected_from_python(
    boundary: datetime,
) -> None:
    with pytest.raises(ValidationError, match="UTC range"):
        span_record(started_at=boundary, ended_at=boundary, duration_ms=0)


@pytest.mark.parametrize(
    "boundary",
    [
        datetime.min.replace(tzinfo=timezone(timedelta(hours=14))),
        datetime.max.replace(tzinfo=timezone(-timedelta(hours=12))),
    ],
)
def test_extreme_offset_timestamps_outside_utc_range_are_rejected_from_json(
    boundary: datetime,
) -> None:
    with pytest.raises(ValidationError, match="UTC range"):
        SpanRecord.model_validate_json(span_record_json(boundary, boundary))


@pytest.mark.parametrize(
    ("source", "expected_utc"),
    [
        (
            datetime(
                2026,
                1,
                2,
                microsecond=123_456,
                tzinfo=timezone(
                    timedelta(
                        hours=5,
                        minutes=30,
                        seconds=45,
                        microseconds=123_456,
                    )
                ),
            ),
            datetime(2026, 1, 1, 18, 29, 15, tzinfo=UTC),
        ),
        (
            datetime(
                2026,
                1,
                2,
                microsecond=654_321,
                tzinfo=timezone(
                    -timedelta(
                        hours=3,
                        minutes=15,
                        seconds=20,
                        microseconds=234_567,
                    )
                ),
            ),
            datetime(2026, 1, 2, 3, 15, 20, 888_888, tzinfo=UTC),
        ),
    ],
)
def test_subminute_offset_timestamps_canonicalize_and_roundtrip_exactly(
    source: datetime, expected_utc: datetime
) -> None:
    record = span_record(started_at=source, ended_at=expected_utc, duration_ms=0)
    parsed = SpanRecord.model_validate_json(record.model_dump_json())

    assert record.started_at == expected_utc
    assert record.started_at.tzinfo is UTC
    assert record.ended_at == expected_utc
    assert record.ended_at.tzinfo is UTC
    assert parsed == record


@pytest.mark.parametrize(
    ("fold", "expected_utc"),
    [
        (0, datetime(2026, 11, 1, 5, 30, tzinfo=UTC)),
        (1, datetime(2026, 11, 1, 6, 30, tzinfo=UTC)),
    ],
)
def test_dst_fold_timestamps_canonicalize_and_roundtrip_semantically(
    fold: int, expected_utc: datetime
) -> None:
    source = datetime(
        2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=fold
    )
    record = span_record(started_at=source, ended_at=source, duration_ms=0)
    parsed = SpanRecord.model_validate_json(record.model_dump_json())

    assert record.started_at == expected_utc
    assert record.started_at.tzinfo is UTC
    assert record.ended_at == expected_utc
    assert record.ended_at.tzinfo is UTC
    assert parsed == record


@pytest.mark.parametrize("status", [TraceStatus.OK, "OK", "DEGRADED", "ERROR"])
def test_trace_status_accepts_only_legitimate_enum_strings(
    status: TraceStatus | str,
) -> None:
    assert span_record(status=status).status is TraceStatus(status)


@pytest.mark.parametrize("invalid_status", [b"OK", "UNKNOWN", 1])
def test_trace_status_rejects_bytes_and_unknown_values(
    invalid_status: object,
) -> None:
    with pytest.raises(ValidationError):
        span_record(status=invalid_status)


@pytest.mark.parametrize(
    "error_code",
    [
        ErrorCode.DEPENDENCY_TIMEOUT,
        "DEPENDENCY_UNAVAILABLE",
        "INVALID_PROVIDER_RESPONSE",
    ],
)
def test_error_code_accepts_only_legitimate_enum_strings(
    error_code: ErrorCode | str,
) -> None:
    assert span_record(error_code=error_code).error_code is ErrorCode(error_code)


@pytest.mark.parametrize("invalid_error_code", [b"DEPENDENCY_TIMEOUT", "UNKNOWN", 1])
def test_error_code_rejects_bytes_and_unknown_values(
    invalid_error_code: object,
) -> None:
    with pytest.raises(ValidationError):
        span_record(error_code=invalid_error_code)


def test_span_record_is_frozen_and_forbids_unknown_fields() -> None:
    record = span_record()

    with pytest.raises(ValidationError, match="frozen"):
        record.stage = "generation"

    payload = record.model_dump()
    payload["raw_query"] = "secret"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SpanRecord.model_validate(payload)


def test_attributes_admit_recursive_json_values_and_legitimate_aggregate_keys() -> None:
    attributes: dict[str, object] = {
        "---": "nonblank punctuation key",
        "cache_hit": True,
        "token_count": 42,
        "latency_ratio": 0.25,
        "chunk_id": "chunk-1",
        "optional_value": None,
        "passwordless_enabled": True,
        "scores": [0.9, 0.8],
        "reason_codes": ["LOCAL_EMPTY", "WEB_USED"],
        "secretary_count": 2,
        "secretion_rate": 0.1,
        "nested": {"selected": False, "ranks": [1, 2]},
    }

    record = span_record(attributes=attributes)

    assert record.model_dump(mode="json")["attributes"] == attributes


@pytest.mark.parametrize(
    "invalid_value",
    [
        b"bytes",
        STARTED_AT,
        {"not", "json"},
        object(),
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_attributes_reject_non_json_values_at_any_depth(
    invalid_value: object,
) -> None:
    with pytest.raises(ValidationError):
        span_record(attributes={"nested": ["ok", {"value": invalid_value}]})


@pytest.mark.parametrize("invalid_attributes", [None, [], "object", 1])
def test_attributes_must_be_a_json_object(invalid_attributes: object) -> None:
    with pytest.raises(ValidationError):
        span_record(attributes=invalid_attributes)


@pytest.mark.parametrize("invalid_key", [1, b"bytes", "", " \t"])
def test_attribute_keys_are_nonblank_strings(invalid_key: object) -> None:
    with pytest.raises(ValidationError):
        span_record(attributes={invalid_key: "value"})


@pytest.mark.parametrize(
    "attributes",
    [
        {"invalid-\ud800-key": "value"},
        {"value": "invalid-\ud800-value"},
        {"nested": {"value": "invalid-\ud800-nested"}},
        {"items": ["valid", "invalid-\ud800-list-item"]},
    ],
)
def test_attributes_reject_unicode_surrogates_recursively(
    attributes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="surrogate"):
        span_record(attributes=attributes)


@pytest.mark.parametrize(
    "sensitive_key",
    [
        "chain_of_thought",
        "Chain-Of-Thought",
        "CHAIN OF THOUGHT",
        "cot",
        "C.O.T.",
        "system_prompt",
        "System-Prompt",
        "raw_evidence",
        "full-evidence",
        "Evidence Content",
        "RAW.HTML",
        "api key",
        "Authorization",
        "password",
        "secret",
        "access-token",
        "refresh.token",
        "private key",
        "Auth-Header",
        "bearer.token",
        "client_credentials",
        "client-secret",
        "system instruction",
    ],
)
def test_attributes_reject_normalized_sensitive_or_control_keys(
    sensitive_key: str,
) -> None:
    with pytest.raises(ValidationError, match="sensitive"):
        span_record(attributes={sensitive_key: "forbidden"})


def test_sensitive_attribute_keys_are_rejected_recursively() -> None:
    with pytest.raises(ValidationError, match="sensitive"):
        span_record(attributes={"safe": [{"API-KEY": "forbidden"}]})


@pytest.mark.parametrize(
    ("sensitive_key", "nested"),
    [
        ("ＡＰＩ＿ＫＥＹ", False),
        ("ｃｈａｉｎ＿ｏｆ＿ｔｈｏｕｇｈｔ", True),
    ],
)
def test_sensitive_attribute_keys_reject_nfkc_compatibility_forms(
    sensitive_key: str, *, nested: bool
) -> None:
    attributes: dict[str, object] = (
        {"safe": [{sensitive_key: "forbidden"}]}
        if nested
        else {sensitive_key: "forbidden"}
    )

    with pytest.raises(ValidationError, match="sensitive"):
        span_record(attributes=attributes)


def test_nfkc_sensitive_key_detection_does_not_rewrite_allowed_keys() -> None:
    allowed_key = "ＴＯＫＥＮ＿ＣＯＵＮＴ"

    record = span_record(attributes={allowed_key: 42})

    assert record.model_dump(mode="json")["attributes"] == {allowed_key: 42}


def test_attributes_are_deep_copied_and_deeply_immutable() -> None:
    ranks: list[object] = [1, 2]
    nested: dict[str, object] = {"ranks": ranks}
    source: dict[str, object] = {"nested": nested}

    record = span_record(attributes=source)
    source["new"] = "caller mutation"
    nested["selected"] = True
    ranks.append(3)

    assert record.model_dump(mode="json")["attributes"] == {"nested": {"ranks": [1, 2]}}

    with pytest.raises(TypeError):
        cast(dict[str, object], record.attributes)["new"] = "mutation"
    frozen_nested = cast(dict[str, object], record.attributes["nested"])
    with pytest.raises(TypeError):
        frozen_nested["selected"] = True
    frozen_ranks = cast(list[object], frozen_nested["ranks"])
    with pytest.raises(AttributeError):
        frozen_ranks.append(3)


def test_attributes_serialize_as_normal_json_and_roundtrip_semantically() -> None:
    record = span_record(
        parent_span_id="parent-1",
        status="DEGRADED",
        error_code="DEPENDENCY_TIMEOUT",
        attributes={
            "chunk_id": "chunk-1",
            "scores": [0.75, 0.5],
            "details": {"attempted": True, "count": 2, "note": None},
        },
    )

    dumped = record.model_dump(mode="json")
    assert isinstance(dumped["attributes"], dict)
    assert isinstance(dumped["attributes"]["scores"], list)
    json.dumps(dumped, allow_nan=False)

    encoded = record.model_dump_json()
    decoded = json.loads(encoded)
    assert isinstance(decoded["attributes"], dict)
    assert isinstance(decoded["attributes"]["scores"], list)
    assert SpanRecord.model_validate_json(encoded) == record


def test_trace_allows_valid_non_ascii_and_emoji_text_to_roundtrip() -> None:
    record = span_record(
        trace_id="追踪-🧭",
        span_id="跨度-✨",
        parent_span_id="父级-🌏",
        stage="本地检索-🔎",
        attributes={"说明-📝": ["中文", "emoji-✅"]},
    )

    assert SpanRecord.model_validate_json(record.model_dump_json()) == record


def test_span_record_is_available_from_the_public_domain_package() -> None:
    import atlasrag.domain as public_domain

    assert "SpanRecord" in public_domain.__all__
    assert public_domain.SpanRecord is SpanRecord
