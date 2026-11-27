"""Stable HTTP error translation reusing the AtlasRAG ErrorCode vocabulary."""

from __future__ import annotations

from enum import StrEnum

from pydantic import ValidationError

from atlasrag.domain.errors import AtlasRAGError, ErrorCode
from atlasrag.server.schemas import ApiErrorResponse


class TransportErrorCode(StrEnum):
    """Non-duplicative HTTP/transport-specific error codes."""

    INVALID_REQUEST = "INVALID_REQUEST"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    OPERATION_NOT_FOUND = "OPERATION_NOT_FOUND"
    REQUEST_CANCELLED = "REQUEST_CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_DOMAIN_ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.RUNTIME_NOT_READY: 503,
    ErrorCode.RUNTIME_SHUTTING_DOWN: 503,
    ErrorCode.MAINTENANCE_BUSY: 409,
    ErrorCode.MAINTENANCE_DRAIN_TIMEOUT: 409,
    ErrorCode.RUNTIME_OWNERSHIP_CONFLICT: 409,
    ErrorCode.RUNTIME_BOOTSTRAP_FAILED: 500,
    ErrorCode.RUNTIME_SHUTDOWN_FAILED: 500,
    ErrorCode.DEPENDENCY_TIMEOUT: 504,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.INVALID_PROVIDER_RESPONSE: 502,
    ErrorCode.CANONICAL_DATA_INCONSISTENT: 500,
    ErrorCode.INVARIANT_VIOLATION: 500,
    ErrorCode.RESOURCE_EXHAUSTED: 503,
    ErrorCode.INVALID_CONFIGURATION: 500,
    ErrorCode.SOURCE_MUTATED: 409,
}

_TRANSPORT_ERROR_DEFAULTS: dict[TransportErrorCode, tuple[int, str]] = {
    TransportErrorCode.INVALID_REQUEST: (422, "Request validation failed."),
    TransportErrorCode.PAYLOAD_TOO_LARGE: (
        413,
        "Request payload exceeds allowed size limit.",
    ),
    TransportErrorCode.UNAUTHORIZED: (
        401,
        "Valid local control authorization is required.",
    ),
    TransportErrorCode.FORBIDDEN: (
        403,
        "Request rejected by local transport security policy.",
    ),
    TransportErrorCode.SESSION_NOT_FOUND: (
        404,
        "Session was not found or has expired.",
    ),
    TransportErrorCode.OPERATION_NOT_FOUND: (
        404,
        "Maintenance operation was not found.",
    ),
    TransportErrorCode.REQUEST_CANCELLED: (
        499,
        "Request was cancelled before completion.",
    ),
    TransportErrorCode.INTERNAL_ERROR: (500, "An internal server error occurred."),
}


class ServerHttpError(Exception):
    """Explicit caller-safe HTTP/transport exception."""

    def __init__(
        self,
        code: ErrorCode | TransportErrorCode,
        safe_message: str | None = None,
        *,
        status_code: int | None = None,
        trace_id: str | None = None,
        details: tuple[str, ...] = (),
    ) -> None:
        if isinstance(code, ErrorCode):
            resolved_status = (
                _DOMAIN_ERROR_HTTP_STATUS[code] if status_code is None else status_code
            )
            resolved_message = (
                "A runtime error occurred." if safe_message is None else safe_message
            )
        else:
            default_status, default_msg = _TRANSPORT_ERROR_DEFAULTS[code]
            resolved_status = default_status if status_code is None else status_code
            resolved_message = default_msg if safe_message is None else safe_message
        self.status_code = resolved_status
        self.code = code
        self.safe_message = resolved_message
        self.trace_id = trace_id
        self.details = details
        super().__init__(code.value, resolved_message)


def safe_validation_details(error: ValidationError) -> tuple[str, ...]:
    """Extract only field locations and validation types; never raw inputs."""

    details: list[str] = []
    for item in error.errors(include_url=False, include_input=False):
        loc = ".".join(str(part) for part in item.get("loc", ())) or "request"
        err_type = str(item.get("type", "validation_error"))
        details.append(f"{loc}: {err_type}")
    return tuple(details[:16])


def translate_exception(
    error: BaseException, *, trace_id: str | None = None
) -> tuple[int, ApiErrorResponse]:
    """Translate any exception into an HTTP status code and safe error payload."""

    if isinstance(error, ServerHttpError):
        return (
            error.status_code,
            ApiErrorResponse(
                error_code=error.code.value,
                message=error.safe_message,
                trace_id=trace_id if error.trace_id is None else error.trace_id,
                details=error.details,
            ),
        )
    if isinstance(error, AtlasRAGError):
        status = _DOMAIN_ERROR_HTTP_STATUS.get(error.code, 500)
        return (
            status,
            ApiErrorResponse(
                error_code=error.code.value,
                message=error.safe_message,
                trace_id=trace_id,
            ),
        )
    if isinstance(error, ValidationError):
        status, message = _TRANSPORT_ERROR_DEFAULTS[TransportErrorCode.INVALID_REQUEST]
        return (
            status,
            ApiErrorResponse(
                error_code=TransportErrorCode.INVALID_REQUEST.value,
                message=message,
                trace_id=trace_id,
                details=safe_validation_details(error),
            ),
        )
    status, message = _TRANSPORT_ERROR_DEFAULTS[TransportErrorCode.INTERNAL_ERROR]
    return (
        status,
        ApiErrorResponse(
            error_code=TransportErrorCode.INTERNAL_ERROR.value,
            message=message,
            trace_id=trace_id,
        ),
    )
