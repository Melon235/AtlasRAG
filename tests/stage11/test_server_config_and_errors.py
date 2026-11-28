"""Unit tests for Stage 11 ServerConfig, strict DTOs, and safe error translation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from atlasrag.domain.enums import EvidenceType
from atlasrag.domain.errors import (
    CanonicalDataError,
    ConfigurationError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    ErrorCode,
    InvariantViolationError,
    MaintenanceBusyError,
    MaintenanceDrainTimeoutError,
    ProviderResponseError,
    ResourceExhaustedError,
    RuntimeBootstrapFailedError,
    RuntimeNotReadyError,
    RuntimeOwnershipConflictError,
    RuntimeShutdownFailedError,
    RuntimeShuttingDownError,
    SourceMutationError,
)
from atlasrag.domain.requests import QueryFilters
from atlasrag.domain.results import FinalLocalCitation, FinalResponse
from atlasrag.server.config import ServerConfig, load_server_config
from atlasrag.server.errors import (
    ServerHttpError,
    TransportErrorCode,
    translate_exception,
)
from atlasrag.server.schemas import QueryRequest, QueryResponse
from atlasrag.server.service import serialize_bounded_response
from atlasrag.services.cache.session import QueryCacheStatus


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "[::1]"])
def test_server_config_accepts_loopback_hosts(host: str) -> None:
    cfg = ServerConfig(host=host)
    assert cfg.host


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "192.168.1.10", "example.com", "127.0.0.1 ", ""],
)
def test_server_config_rejects_non_loopback_hosts(host: str) -> None:
    with pytest.raises(ValidationError):
        ServerConfig(host=host)


def test_server_config_rejects_inconsistent_bounds_and_wildcard_hosts() -> None:
    with pytest.raises(ValidationError):
        ServerConfig(max_concurrent_queries=4, max_concurrent_streams=5)
    with pytest.raises(ValidationError):
        ServerConfig(max_concurrent_queries=4, max_in_flight_per_session=5)
    with pytest.raises(ValidationError):
        ServerConfig(allowed_hosts=("*",))
    with pytest.raises(ValidationError):
        ServerConfig(allowed_hosts=("example.com:8000",))


def test_load_server_config_merges_file_and_env_with_redacted_errors(
    tmp_path: Path,
) -> None:
    cfg_file = tmp_path / "server.json"
    cfg_file.write_text(
        json.dumps({"host": "localhost", "port": 8081, "max_request_bytes": 32768}),
        encoding="utf-8",
    )
    loaded = load_server_config(
        config_file=cfg_file,
        environ={"ATLASRAG_SERVER_CONFIG_JSON": json.dumps({"port": 8090})},
    )
    assert loaded.host == "localhost"
    assert loaded.port == 8090
    assert loaded.max_request_bytes == 32768

    secret_val = "0.0.0.0-secret-bind"
    with pytest.raises(ConfigurationError) as exc_info:
        load_server_config(
            environ={"ATLASRAG_SERVER_CONFIG_JSON": json.dumps({"host": secret_val})}
        )
    assert secret_val not in str(exc_info.value)


def test_query_request_and_response_enforce_strict_contracts() -> None:
    valid = QueryRequest(
        session_id="sess-1",
        query="What is AtlasRAG?",
        filters=QueryFilters(file_name="doc.pdf"),
    )
    assert valid.session_id == "sess-1"
    assert valid.filters == QueryFilters(file_name="doc.pdf")

    for bad_payload in [
        {"session_id": "sess-1", "query": "hi", "top_k": 5},
        {"session_id": "sess-1", "query": "hi", "web_enabled": False},
        {"session_id": "sess-1", "query": "hi", "model": "deepseek"},
        {"session_id": "sess-1", "query": "hi", "milvus_expr": "id > 0"},
        {"session_id": "sess-1", "query": "   "},
        {"session_id": "   ", "query": "hi"},
    ]:
        with pytest.raises(ValidationError):
            QueryRequest.model_validate(bad_payload)

    final_resp = FinalResponse(
        answer_text="Answer [L1].",
        citations=(
            FinalLocalCitation(
                citation_id="L1",
                file_name="doc.pdf",
                relative_source_path="doc.pdf",
                evidence_type=EvidenceType.TEXT_PARENT,
            ),
        ),
    )
    envelope = QueryResponse(
        trace_id="trace-1",
        session_id="sess-1",
        cache_status=QueryCacheStatus.HIT,
        elapsed_ms=12.5,
        response=final_resp,
    )
    assert envelope.response == final_resp
    assert envelope.cache_status is QueryCacheStatus.HIT


def test_serialize_bounded_response_enforces_size_limit_without_truncation() -> None:
    envelope = QueryResponse(
        trace_id="trace-1",
        session_id="sess-1",
        cache_status=QueryCacheStatus.MISS,
        elapsed_ms=5.0,
        response=FinalResponse(answer_text="x" * 512, citations=()),
    )
    encoded = serialize_bounded_response(envelope, max_bytes=4096)
    assert json.loads(encoded.decode("utf-8"))["response"]["answer_text"] == "x" * 512

    with pytest.raises(ServerHttpError) as exc_info:
        serialize_bounded_response(envelope, max_bytes=64)
    assert exc_info.value.status_code == 500
    assert exc_info.value.code is ErrorCode.INVARIANT_VIOLATION


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (RuntimeNotReadyError(), 503, ErrorCode.RUNTIME_NOT_READY.value),
        (RuntimeShuttingDownError(), 503, ErrorCode.RUNTIME_SHUTTING_DOWN.value),
        (MaintenanceBusyError(), 409, ErrorCode.MAINTENANCE_BUSY.value),
        (
            MaintenanceDrainTimeoutError(),
            409,
            ErrorCode.MAINTENANCE_DRAIN_TIMEOUT.value,
        ),
        (
            RuntimeOwnershipConflictError(),
            409,
            ErrorCode.RUNTIME_OWNERSHIP_CONFLICT.value,
        ),
        (
            RuntimeBootstrapFailedError(),
            500,
            ErrorCode.RUNTIME_BOOTSTRAP_FAILED.value,
        ),
        (RuntimeShutdownFailedError(), 500, ErrorCode.RUNTIME_SHUTDOWN_FAILED.value),
        (DependencyTimeoutError(), 504, ErrorCode.DEPENDENCY_TIMEOUT.value),
        (DependencyUnavailableError(), 503, ErrorCode.DEPENDENCY_UNAVAILABLE.value),
        (ProviderResponseError(), 502, ErrorCode.INVALID_PROVIDER_RESPONSE.value),
        (CanonicalDataError(), 500, ErrorCode.CANONICAL_DATA_INCONSISTENT.value),
        (InvariantViolationError(), 500, ErrorCode.INVARIANT_VIOLATION.value),
        (ResourceExhaustedError(), 503, ErrorCode.RESOURCE_EXHAUSTED.value),
        (ConfigurationError(), 500, ErrorCode.INVALID_CONFIGURATION.value),
        (SourceMutationError(), 409, ErrorCode.SOURCE_MUTATED.value),
        (
            ServerHttpError(TransportErrorCode.INVALID_REQUEST),
            422,
            TransportErrorCode.INVALID_REQUEST.value,
        ),
        (
            ServerHttpError(TransportErrorCode.PAYLOAD_TOO_LARGE),
            413,
            TransportErrorCode.PAYLOAD_TOO_LARGE.value,
        ),
        (
            ServerHttpError(TransportErrorCode.UNAUTHORIZED),
            401,
            TransportErrorCode.UNAUTHORIZED.value,
        ),
        (
            ServerHttpError(TransportErrorCode.FORBIDDEN),
            403,
            TransportErrorCode.FORBIDDEN.value,
        ),
        (
            ServerHttpError(TransportErrorCode.SESSION_NOT_FOUND),
            404,
            TransportErrorCode.SESSION_NOT_FOUND.value,
        ),
        (
            ServerHttpError(TransportErrorCode.OPERATION_NOT_FOUND),
            404,
            TransportErrorCode.OPERATION_NOT_FOUND.value,
        ),
        (
            ServerHttpError(TransportErrorCode.REQUEST_CANCELLED),
            499,
            TransportErrorCode.REQUEST_CANCELLED.value,
        ),
        (
            ServerHttpError(TransportErrorCode.INTERNAL_ERROR),
            500,
            TransportErrorCode.INTERNAL_ERROR.value,
        ),
    ],
)
def test_translate_exception_covers_all_domain_and_transport_codes(
    error: Exception, expected_status: int, expected_code: str
) -> None:
    status, payload = translate_exception(error, trace_id="trace-xyz")
    assert status == expected_status
    assert payload.error_code == expected_code
    assert payload.trace_id == "trace-xyz"


def test_translate_exception_redacts_secrets_and_raw_validation_inputs() -> None:
    secret_dsn = "postgresql://atlasrag:super-secret-password@db/internal/path"
    status, payload = translate_exception(
        RuntimeError(f"boom {secret_dsn}"), trace_id="t1"
    )
    assert status == 500
    assert payload.error_code == TransportErrorCode.INTERNAL_ERROR.value
    assert secret_dsn not in payload.message

    raw_secret_input = "raw-user-secret-query"
    try:
        QueryRequest.model_validate(
            {"session_id": "s1", "query": raw_secret_input, "extra_key": 1}
        )
    except ValidationError as validation_err:
        v_status, v_payload = translate_exception(validation_err, trace_id="t2")
    else:
        raise AssertionError("expected ValidationError")
    assert v_status == 422
    assert v_payload.error_code == TransportErrorCode.INVALID_REQUEST.value
    assert raw_secret_input not in json.dumps(v_payload.model_dump(mode="json"))
    assert "extra_key" in " ".join(v_payload.details)
