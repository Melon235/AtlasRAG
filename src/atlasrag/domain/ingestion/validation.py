"""Closed immutable outcomes for Stage 3 production validation."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BeforeValidator, model_validator

from atlasrag.domain.base import (
    FrozenModel,
    validate_ordered_collection_input,
    validate_string_enum_input,
)


class ValidationOutcome(StrEnum):
    """The only production-validation outcomes."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class ValidationReasonCode(StrEnum):
    """Stable reason vocabulary in canonical evaluation order."""

    PARSE_DEGRADED = "PARSE_DEGRADED"
    PARSE_PARTIAL = "PARSE_PARTIAL"
    DOCUMENT_SCHEMA_INVALID = "DOCUMENT_SCHEMA_INVALID"
    DOCUMENT_ID_INVALID = "DOCUMENT_ID_INVALID"
    DOCUMENT_SOURCE_INVALID = "DOCUMENT_SOURCE_INVALID"
    DOCUMENT_REVISION_INVALID = "DOCUMENT_REVISION_INVALID"
    PARSE_UNUSABLE = "PARSE_UNUSABLE"
    DOCUMENT_EMPTY = "DOCUMENT_EMPTY"
    ELEMENT_ORDER_INVALID = "ELEMENT_ORDER_INVALID"
    ELEMENT_ID_DUPLICATE = "ELEMENT_ID_DUPLICATE"
    ELEMENT_DOCUMENT_MISMATCH = "ELEMENT_DOCUMENT_MISMATCH"
    ELEMENT_REVISION_MISMATCH = "ELEMENT_REVISION_MISMATCH"
    ELEMENT_ID_INVALID = "ELEMENT_ID_INVALID"
    UNICODE_INVALID = "UNICODE_INVALID"
    CANONICAL_JSON_INVALID = "CANONICAL_JSON_INVALID"
    SECTION_PATH_INVALID = "SECTION_PATH_INVALID"
    SOURCE_ANCHOR_INVALID = "SOURCE_ANCHOR_INVALID"
    TABLE_INVARIANT_INVALID = "TABLE_INVARIANT_INVALID"
    CHUNK_SET_EMPTY = "CHUNK_SET_EMPTY"
    CHUNK_SCHEMA_INVALID = "CHUNK_SCHEMA_INVALID"
    CHUNK_UNKNOWN_FIELD = "CHUNK_UNKNOWN_FIELD"
    CHUNK_TRUNCATION_STATE = "CHUNK_TRUNCATION_STATE"
    CHUNK_ORDER_INVALID = "CHUNK_ORDER_INVALID"
    CHUNK_ID_DUPLICATE = "CHUNK_ID_DUPLICATE"
    CHUNK_DOCUMENT_MISMATCH = "CHUNK_DOCUMENT_MISMATCH"
    CHUNK_REVISION_MISMATCH = "CHUNK_REVISION_MISMATCH"
    CHUNK_SHAPE_INVALID = "CHUNK_SHAPE_INVALID"
    CHUNK_CONTENT_EMPTY = "CHUNK_CONTENT_EMPTY"
    CHUNK_CONTENT_HASH_INVALID = "CHUNK_CONTENT_HASH_INVALID"
    CHUNK_ID_INVALID = "CHUNK_ID_INVALID"
    PARENT_REFERENCE_INVALID = "PARENT_REFERENCE_INVALID"
    PARENT_CHILD_ORDER_INVALID = "PARENT_CHILD_ORDER_INVALID"
    CHUNK_SOURCE_ORDER_INVALID = "CHUNK_SOURCE_ORDER_INVALID"
    ELEMENT_SPAN_INVALID = "ELEMENT_SPAN_INVALID"
    PARENT_CONTENT_INVALID = "PARENT_CONTENT_INVALID"
    PARENT_COVERAGE_INVALID = "PARENT_COVERAGE_INVALID"
    CHILD_RANGE_INVALID = "CHILD_RANGE_INVALID"
    CHILD_CONTENT_INVALID = "CHILD_CONTENT_INVALID"
    CHILD_LINEAGE_INVALID = "CHILD_LINEAGE_INVALID"
    CHILD_COVERAGE_INVALID = "CHILD_COVERAGE_INVALID"
    TABLE_SOURCE_INVALID = "TABLE_SOURCE_INVALID"
    TABLE_SLICE_INVALID = "TABLE_SLICE_INVALID"
    TABLE_COVERAGE_INVALID = "TABLE_COVERAGE_INVALID"


_ValidationOutcome = Annotated[
    ValidationOutcome, BeforeValidator(validate_string_enum_input)
]
_ValidationReasonCode = Annotated[
    ValidationReasonCode, BeforeValidator(validate_string_enum_input)
]
_ReasonCodes = Annotated[
    tuple[_ValidationReasonCode, ...],
    BeforeValidator(validate_ordered_collection_input),
]
_WARNING_CODES = frozenset(
    {
        ValidationReasonCode.PARSE_DEGRADED,
        ValidationReasonCode.PARSE_PARTIAL,
    }
)
_REASON_ORDER = {code: index for index, code in enumerate(ValidationReasonCode)}


def outcome_for_reason_codes(
    reason_codes: tuple[ValidationReasonCode, ...],
) -> ValidationOutcome:
    """Derive the only coherent outcome for one canonical reason tuple."""
    if not reason_codes:
        return ValidationOutcome.PASS
    if all(code in _WARNING_CODES for code in reason_codes):
        return ValidationOutcome.WARN
    return ValidationOutcome.FAIL


class ValidationResult(FrozenModel):
    """Minimal deterministic result with no messages or provider payloads."""

    outcome: _ValidationOutcome
    reason_codes: _ReasonCodes

    @model_validator(mode="after")
    def _ordered_unique_and_coherent(self) -> Self:
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("reason_codes must be unique")
        if tuple(sorted(self.reason_codes, key=_REASON_ORDER.__getitem__)) != (
            self.reason_codes
        ):
            raise ValueError("reason_codes must use canonical rule order")
        expected = outcome_for_reason_codes(self.reason_codes)
        if self.outcome is not expected:
            raise ValueError("outcome must exactly match reason_codes")
        return self
