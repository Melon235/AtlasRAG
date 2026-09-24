"""Contract tests for shared immutable models, enums, and technical errors."""

from __future__ import annotations

import pickle
from collections.abc import Callable
from copy import copy, deepcopy
from enum import StrEnum
from math import inf, nan
from typing import cast

import pytest
from pydantic import ValidationError

from atlasrag.domain.base import FrozenModel
from atlasrag.domain.enums import (
    AnswerExecutionOutcome,
    CandidateOrdering,
    EvidenceType,
    LocalAnswerEvidenceStatus,
    LocalEvidenceStatus,
    LocalExecutionStatus,
    OutputReviewDecision,
    OutputReviewReasonCode,
    RagCompletionStatus,
    RetrievalBranch,
    RetrievalBranchStatus,
    RetrievalChunkType,
    RetrievalMode,
    SourceKind,
    SourceType,
    TraceStatus,
    WebContentOrigin,
    WebEvidenceRepresentation,
    WebExecutionStatus,
)
from atlasrag.domain.errors import (
    AtlasRAGError,
    CanonicalDataError,
    ConfigurationError,
    DependencyError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    ErrorCode,
    InvariantViolationError,
    ProviderResponseError,
    ResourceExhaustedError,
    SourceMutationError,
)

ENUM_CASES: tuple[tuple[type[StrEnum], tuple[str, ...]], ...] = (
    (SourceType, ("PDF", "DOCX", "XLSX", "MD", "TXT")),
    (RetrievalChunkType, ("TEXT_CHILD", "TABLE")),
    (EvidenceType, ("TEXT_PARENT", "TABLE")),
    (RetrievalBranch, ("DENSE", "BM25")),
    (RetrievalBranchStatus, ("OK", "EMPTY", "UNAVAILABLE")),
    (RetrievalMode, ("HYBRID", "DENSE_ONLY", "BM25_ONLY", "UNAVAILABLE")),
    (CandidateOrdering, ("RRF", "DENSE", "BM25", "RERANKER")),
    (LocalExecutionStatus, ("OK", "DEGRADED", "UNAVAILABLE")),
    (LocalEvidenceStatus, ("SUFFICIENT", "INSUFFICIENT", "AMBIGUOUS")),
    (WebContentOrigin, ("FETCHED_PAGE", "SEARCH_SNIPPET")),
    (
        WebEvidenceRepresentation,
        ("SUMMARY", "SANITIZED_CONTENT", "SEARCH_SNIPPET"),
    ),
    (WebExecutionStatus, ("OK", "DEGRADED", "EMPTY", "UNAVAILABLE")),
    (
        LocalAnswerEvidenceStatus,
        ("SUFFICIENT", "INSUFFICIENT_BEST_MATCH", "NONE"),
    ),
    (OutputReviewDecision, ("ACCEPT", "REGENERATE", "BLOCK")),
    (
        OutputReviewReasonCode,
        (
            "UNKNOWN_CITATION",
            "CITATION_SET_MISMATCH",
            "INVALID_OUTPUT_SCHEMA",
            "FORGED_SOURCE_REFERENCE",
            "SOURCE_BOUNDARY_VIOLATION",
            "CONTROL_LEAKAGE",
            "PROTOCOL_CONTENT",
        ),
    ),
    (SourceKind, ("LOCAL", "WEB")),
    (RagCompletionStatus, ("SUCCESS", "DEGRADED_SUCCESS", "SAFE_FAILURE")),
    (AnswerExecutionOutcome, ("SUCCESS", "SAFE_FAILURE")),
    (TraceStatus, ("OK", "DEGRADED", "ERROR")),
)

ERROR_CODE_VALUES = (
    "DEPENDENCY_TIMEOUT",
    "DEPENDENCY_UNAVAILABLE",
    "INVALID_PROVIDER_RESPONSE",
    "CANONICAL_DATA_INCONSISTENT",
    "INVARIANT_VIOLATION",
    "RESOURCE_EXHAUSTED",
    "INVALID_CONFIGURATION",
    "SOURCE_MUTATED",
)


class _EnumProbe(FrozenModel):
    source_type: SourceType
    error_code: ErrorCode


class _NumberProbe(FrozenModel):
    value: float


@pytest.mark.parametrize(("enum_type", "expected_values"), ENUM_CASES)
def test_stable_enums_have_exact_names_values_and_no_aliases(
    enum_type: type[StrEnum], expected_values: tuple[str, ...]
) -> None:
    assert tuple(enum_type.__members__) == expected_values
    assert tuple(member.value for member in enum_type) == expected_values
    assert len(enum_type.__members__) == len(enum_type)


def test_error_codes_have_exact_stable_values_and_no_aliases() -> None:
    assert tuple(ErrorCode.__members__) == ERROR_CODE_VALUES
    assert tuple(member.value for member in ErrorCode) == ERROR_CODE_VALUES
    assert len(ErrorCode.__members__) == len(ErrorCode)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (("source_type", "HTML"), ("error_code", "VENDOR_FAILURE")),
)
def test_pydantic_probe_rejects_unknown_enum_values(
    field_name: str, invalid_value: str
) -> None:
    payload: dict[str, object] = {
        "source_type": SourceType.PDF,
        "error_code": ErrorCode.DEPENDENCY_TIMEOUT,
    }
    payload[field_name] = invalid_value

    with pytest.raises(ValidationError) as exc_info:
        _EnumProbe.model_validate(payload)

    assert exc_info.value.errors()[0]["type"] == "enum"


def test_frozen_model_rejects_mutation() -> None:
    probe = _NumberProbe(value=1.0)

    with pytest.raises(ValidationError) as exc_info:
        setattr(probe, "value", 2.0)

    assert exc_info.value.errors()[0]["type"] == "frozen_instance"


def test_frozen_model_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _NumberProbe.model_validate({"value": 1.0, "unexpected": True})

    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("non_finite", (inf, -inf, nan))
def test_frozen_model_rejects_non_finite_numbers(non_finite: float) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _NumberProbe(value=non_finite)

    assert exc_info.value.errors()[0]["type"] == "finite_number"


def test_exception_hierarchy_distinguishes_dependency_failures() -> None:
    assert issubclass(DependencyError, AtlasRAGError)
    assert issubclass(DependencyUnavailableError, DependencyError)
    assert issubclass(DependencyTimeoutError, DependencyError)

    for exception_type in (
        ProviderResponseError,
        CanonicalDataError,
        InvariantViolationError,
        ResourceExhaustedError,
        ConfigurationError,
        SourceMutationError,
    ):
        assert issubclass(exception_type, AtlasRAGError)
        assert not issubclass(exception_type, DependencyError)


FIXED_ERROR_CASES: tuple[tuple[Callable[[], AtlasRAGError], ErrorCode, str], ...] = (
    (
        DependencyUnavailableError,
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        "A required dependency is unavailable.",
    ),
    (
        DependencyTimeoutError,
        ErrorCode.DEPENDENCY_TIMEOUT,
        "A required dependency timed out.",
    ),
    (
        ProviderResponseError,
        ErrorCode.INVALID_PROVIDER_RESPONSE,
        "A provider returned an invalid response.",
    ),
    (
        CanonicalDataError,
        ErrorCode.CANONICAL_DATA_INCONSISTENT,
        "Canonical data is inconsistent.",
    ),
    (
        InvariantViolationError,
        ErrorCode.INVARIANT_VIOLATION,
        "An internal invariant was violated.",
    ),
    (
        ResourceExhaustedError,
        ErrorCode.RESOURCE_EXHAUSTED,
        "A required resource was exhausted.",
    ),
    (
        ConfigurationError,
        ErrorCode.INVALID_CONFIGURATION,
        "Configuration is invalid.",
    ),
    (
        SourceMutationError,
        ErrorCode.SOURCE_MUTATED,
        "The source changed during processing.",
    ),
)


@pytest.mark.parametrize(
    ("exception_type", "expected_code", "expected_message"), FIXED_ERROR_CASES
)
def test_fixed_exceptions_expose_stable_code_and_caller_safe_message(
    exception_type: Callable[[], AtlasRAGError],
    expected_code: ErrorCode,
    expected_message: str,
) -> None:
    error = exception_type()

    assert error.code is expected_code
    assert error.safe_message == expected_message
    assert str(error) == expected_message
    assert not hasattr(error, "vendor_exception")
    assert not hasattr(error, "vendor_payload")


@pytest.mark.parametrize(
    "code", (ErrorCode.DEPENDENCY_TIMEOUT, ErrorCode.DEPENDENCY_UNAVAILABLE)
)
def test_dependency_error_accepts_only_dependency_codes(code: ErrorCode) -> None:
    error = DependencyError(code)

    assert error.code is code
    assert type(error.code) is ErrorCode
    assert error.safe_message == "A required dependency failed."
    assert str(error) == error.safe_message


@pytest.mark.parametrize(
    "invalid_code",
    ("DEPENDENCY_TIMEOUT", "DEPENDENCY_UNAVAILABLE", None, 0, object()),
)
def test_dependency_error_rejects_values_that_are_not_error_codes(
    invalid_code: object,
) -> None:
    with pytest.raises(TypeError, match="code must be an ErrorCode"):
        DependencyError(invalid_code)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "code",
    tuple(
        code
        for code in ErrorCode
        if code not in {ErrorCode.DEPENDENCY_TIMEOUT, ErrorCode.DEPENDENCY_UNAVAILABLE}
    ),
)
def test_dependency_error_rejects_non_dependency_codes(code: ErrorCode) -> None:
    with pytest.raises(ValueError, match="dependency error code"):
        DependencyError(code)


def _generic_atlasrag_error() -> AtlasRAGError:
    return AtlasRAGError(
        ErrorCode.INVARIANT_VIOLATION,
        "A caller-safe technical failure occurred.",
    )


def _generic_dependency_error() -> AtlasRAGError:
    return DependencyError(ErrorCode.DEPENDENCY_TIMEOUT)


ERROR_RECONSTRUCTION_CASES: tuple[
    tuple[str, Callable[[], AtlasRAGError], tuple[object, ...]], ...
] = (
    (
        "AtlasRAGError",
        _generic_atlasrag_error,
        (
            ErrorCode.INVARIANT_VIOLATION,
            "A caller-safe technical failure occurred.",
        ),
    ),
    (
        "DependencyError",
        _generic_dependency_error,
        (ErrorCode.DEPENDENCY_TIMEOUT,),
    ),
    ("DependencyUnavailableError", DependencyUnavailableError, ()),
    ("DependencyTimeoutError", DependencyTimeoutError, ()),
    ("ProviderResponseError", ProviderResponseError, ()),
    ("CanonicalDataError", CanonicalDataError, ()),
    ("InvariantViolationError", InvariantViolationError, ()),
    ("ResourceExhaustedError", ResourceExhaustedError, ()),
    ("ConfigurationError", ConfigurationError, ()),
    ("SourceMutationError", SourceMutationError, ()),
)


def _copy_error(error: AtlasRAGError) -> AtlasRAGError:
    return copy(error)


def _deepcopy_error(error: AtlasRAGError) -> AtlasRAGError:
    return deepcopy(error)


def _pickle_error(error: AtlasRAGError) -> AtlasRAGError:
    return cast(AtlasRAGError, pickle.loads(pickle.dumps(error)))


ERROR_ROUND_TRIPS: tuple[tuple[str, Callable[[AtlasRAGError], AtlasRAGError]], ...] = (
    ("copy", _copy_error),
    ("deepcopy", _deepcopy_error),
    ("pickle", _pickle_error),
)


@pytest.mark.parametrize(
    ("_case_name", "error_factory", "expected_constructor_args"),
    ERROR_RECONSTRUCTION_CASES,
    ids=tuple(case_name for case_name, _, _ in ERROR_RECONSTRUCTION_CASES),
)
def test_exception_args_match_constructor_inputs(
    _case_name: str,
    error_factory: Callable[[], AtlasRAGError],
    expected_constructor_args: tuple[object, ...],
) -> None:
    error = error_factory()

    assert error.args == expected_constructor_args


@pytest.mark.parametrize(
    ("_round_trip_name", "round_trip"),
    ERROR_ROUND_TRIPS,
    ids=tuple(name for name, _ in ERROR_ROUND_TRIPS),
)
@pytest.mark.parametrize(
    ("_case_name", "error_factory", "expected_constructor_args"),
    ERROR_RECONSTRUCTION_CASES,
    ids=tuple(case_name for case_name, _, _ in ERROR_RECONSTRUCTION_CASES),
)
def test_exceptions_survive_copy_deepcopy_and_pickle_round_trips(
    _round_trip_name: str,
    round_trip: Callable[[AtlasRAGError], AtlasRAGError],
    _case_name: str,
    error_factory: Callable[[], AtlasRAGError],
    expected_constructor_args: tuple[object, ...],
) -> None:
    original = error_factory()

    assert type(original.code) is ErrorCode
    assert str(original) == original.safe_message

    reconstructed = round_trip(original)

    assert reconstructed is not original
    assert type(reconstructed) is type(original)
    assert type(reconstructed.code) is ErrorCode
    assert reconstructed.code is original.code
    assert reconstructed.safe_message == original.safe_message
    assert str(reconstructed) == str(original)
    assert reconstructed.args == expected_constructor_args


def test_normal_negative_outcomes_are_not_error_codes() -> None:
    assert {"EMPTY", "INSUFFICIENT", "AMBIGUOUS"}.isdisjoint(
        code.value for code in ErrorCode
    )
