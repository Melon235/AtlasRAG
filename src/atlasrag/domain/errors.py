"""Stable technical error vocabulary and exception hierarchy."""

from enum import StrEnum


class ErrorCode(StrEnum):
    DEPENDENCY_TIMEOUT = "DEPENDENCY_TIMEOUT"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    INVALID_PROVIDER_RESPONSE = "INVALID_PROVIDER_RESPONSE"
    CANONICAL_DATA_INCONSISTENT = "CANONICAL_DATA_INCONSISTENT"
    INVARIANT_VIOLATION = "INVARIANT_VIOLATION"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    SOURCE_MUTATED = "SOURCE_MUTATED"


class AtlasRAGError(Exception):
    """Base class for normalized AtlasRAG technical failures."""

    code: ErrorCode
    safe_message: str

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class DependencyError(AtlasRAGError):
    """Normalized failure of an external runtime dependency."""

    safe_message = "A required dependency failed."
    _allowed_codes = frozenset(
        {ErrorCode.DEPENDENCY_TIMEOUT, ErrorCode.DEPENDENCY_UNAVAILABLE}
    )

    def __init__(self, code: ErrorCode) -> None:
        if code not in self._allowed_codes:
            raise ValueError(f"unsupported dependency error code: {code}")
        self.code = code
        super().__init__()


class DependencyUnavailableError(DependencyError):
    code = ErrorCode.DEPENDENCY_UNAVAILABLE
    safe_message = "A required dependency is unavailable."

    def __init__(self) -> None:
        super().__init__(self.code)


class DependencyTimeoutError(DependencyError):
    code = ErrorCode.DEPENDENCY_TIMEOUT
    safe_message = "A required dependency timed out."

    def __init__(self) -> None:
        super().__init__(self.code)


class ProviderResponseError(AtlasRAGError):
    code = ErrorCode.INVALID_PROVIDER_RESPONSE
    safe_message = "A provider returned an invalid response."


class CanonicalDataError(AtlasRAGError):
    code = ErrorCode.CANONICAL_DATA_INCONSISTENT
    safe_message = "Canonical data is inconsistent."


class InvariantViolationError(AtlasRAGError):
    code = ErrorCode.INVARIANT_VIOLATION
    safe_message = "An internal invariant was violated."


class ResourceExhaustedError(AtlasRAGError):
    code = ErrorCode.RESOURCE_EXHAUSTED
    safe_message = "A required resource was exhausted."


class ConfigurationError(AtlasRAGError):
    code = ErrorCode.INVALID_CONFIGURATION
    safe_message = "Configuration is invalid."


class SourceMutationError(AtlasRAGError):
    code = ErrorCode.SOURCE_MUTATED
    safe_message = "The source changed during processing."
