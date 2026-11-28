"""Strict Pydantic v2 transport DTOs independent of FastAPI/Starlette."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator

from atlasrag._canonical import validate_unicode_scalar_text
from atlasrag.domain.base import FrozenModel, StrictReal
from atlasrag.domain.observation import ObservationStage
from atlasrag.domain.requests import QueryFilters
from atlasrag.domain.results import FinalResponse
from atlasrag.runtime.health import DependencyHealth
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.services.cache.session import QueryCacheStatus
from atlasrag.services.sync.models import ServingSafety, SyncOutcome


def _validate_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return validate_unicode_scalar_text(value)


class LivenessStatus(StrEnum):
    ALIVE = "ALIVE"
    SHUTTING_DOWN = "SHUTTING_DOWN"


class ClientKind(StrEnum):
    WEB = "web"
    CLI = "cli"


class CreateSessionRequest(FrozenModel):
    """Strict session creation payload preserving default web classification."""

    client_kind: ClientKind = ClientKind.WEB


class SessionResponse(FrozenModel):
    """Server-issued session identity and expiration metadata."""

    session_id: StrictStr
    client_kind: ClientKind
    created_at: datetime
    expires_at: datetime


class ApiErrorResponse(FrozenModel):
    """Stable public error payload without stack traces, DSNs, or raw inputs."""

    error_code: StrictStr
    message: StrictStr
    trace_id: StrictStr | None = None
    details: tuple[StrictStr, ...] = ()


class LivenessResponse(FrozenModel):
    status: LivenessStatus
    mode: RuntimeMode


class ReadinessResponse(FrozenModel):
    ready: StrictBool
    mode: RuntimeMode


class DependencyStatusMap(FrozenModel):
    postgres: DependencyHealth
    milvus: DependencyHealth
    redis: DependencyHealth
    searxng: DependencyHealth
    embedding: DependencyHealth
    reranker: DependencyHealth


class RuntimeStatusResponse(FrozenModel):
    mode: RuntimeMode
    ready: StrictBool
    knowledge_verified: StrictBool
    active_queries: Annotated[StrictInt, Field(ge=0)]
    dependencies: DependencyStatusMap
    query_cache: StrictStr
    pipeline_fingerprint: StrictStr
    runtime_fingerprint: StrictStr
    checked_at: datetime


class QueryRequest(FrozenModel):
    """Canonical query transport request preserving UserTurnRequest semantics."""

    session_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    query: Annotated[StrictStr, Field(min_length=1, max_length=8_192)]
    filters: QueryFilters | None = None

    @field_validator("session_id")
    @classmethod
    def _clean_session_id(cls, value: str) -> str:
        return _validate_nonblank(value).strip()

    _nonblank_query = field_validator("query")(_validate_nonblank)


class QueryResponse(FrozenModel):
    """Transport envelope wrapping unmodified FinalResponse."""

    trace_id: StrictStr
    session_id: StrictStr
    cache_status: QueryCacheStatus
    elapsed_ms: Annotated[StrictReal, Field(ge=0)]
    response: FinalResponse


class SseAcceptedEvent(FrozenModel):
    """First SSE frame emitted only after authoritative QueryLease admission."""

    trace_id: StrictStr
    session_id: StrictStr


class SseProgressEvent(FrozenModel):
    """Strictly allowlisted SSE progress projection (never raw observations)."""

    trace_id: StrictStr
    stage: ObservationStage
    status: StrictStr
    attempt: Annotated[StrictInt, Field(ge=1)]
    degraded: StrictBool


class SseErrorEvent(FrozenModel):
    """Safe terminal SSE error frame matching public error vocabulary."""

    trace_id: StrictStr | None = None
    error_code: StrictStr
    message: StrictStr


class ControlEmptyRequest(FrozenModel):
    """Strict empty / {} payload for control-plane POST actions."""


class OperationKind(StrEnum):
    SYNC = "SYNC"
    RECOVER = "RECOVER"


class OperationStatus(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class OperationAcceptedResponse(FrozenModel):
    """HTTP 202 response returned only after coordinator DRAINING handoff."""

    operation_id: StrictStr
    kind: OperationKind
    status: OperationStatus
    accepted_at: datetime


class OperationSummary(FrozenModel):
    """Bounded count-only summary projected from SyncResult."""

    checked_documents: Annotated[StrictInt, Field(ge=0)] = 0
    planned_builds: Annotated[StrictInt, Field(ge=0)] = 0
    planned_deletions: Annotated[StrictInt, Field(ge=0)] = 0
    ready_builds: Annotated[StrictInt, Field(ge=0)] = 0
    failed_builds: Annotated[StrictInt, Field(ge=0)] = 0
    source_mutated_builds: Annotated[StrictInt, Field(ge=0)] = 0
    technical_build_failures: Annotated[StrictInt, Field(ge=0)] = 0
    deleted_documents: Annotated[StrictInt, Field(ge=0)] = 0
    failed_deletions: Annotated[StrictInt, Field(ge=0)] = 0
    verified_documents: Annotated[StrictInt, Field(ge=0)] = 0
    verified_records: Annotated[StrictInt, Field(ge=0)] = 0


class OperationStatusResponse(FrozenModel):
    """Control-plane operation status separating sync outcome from serving safety."""

    operation_id: StrictStr
    kind: OperationKind
    status: OperationStatus
    sync_outcome: SyncOutcome | None = None
    serving_safety: ServingSafety | None = None
    runtime_mode: RuntimeMode
    unsafe_reasons: tuple[StrictStr, ...] = ()
    blocked_code: StrictStr | None = None
    summary: OperationSummary | None = None
    started_at: datetime
    finished_at: datetime | None = None
    error_code: StrictStr | None = None
    message: StrictStr | None = None


class ShutdownAcceptedResponse(FrozenModel):
    """HTTP 202 response returned after closing admission and handing off shutdown."""

    status: LivenessStatus = LivenessStatus.SHUTTING_DOWN
    pid: Annotated[StrictInt, Field(ge=1)]
    server_instance_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]


class ServerDiscoveryRecord(FrozenModel):
    """Local server discovery metadata written only after lock and socket bind."""

    url: StrictStr
    host: StrictStr
    port: Annotated[StrictInt, Field(ge=1, le=65535)]
    pid: Annotated[StrictInt, Field(ge=1)]
    process_start_ticks: Annotated[StrictInt, Field(ge=0)]
    boot_id: StrictStr
    server_instance_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    started_at: datetime
    shutdown_timeout_seconds: Annotated[StrictReal, Field(gt=0)] | None = None


class ShutdownReceipt(FrozenModel):
    """Atomic clean-shutdown receipt written strictly under RuntimeOwnershipLock."""

    server_instance_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    pid: Annotated[StrictInt, Field(ge=1)]
    status: Literal["CLEAN_SHUTDOWN"] = "CLEAN_SHUTDOWN"
    completed_at: datetime


class CliSessionRecord(FrozenModel):
    """Locally persisted CLI session metadata scoped to one server instance."""

    server_instance_id: StrictStr | None = None
    server_url: StrictStr
    session_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    expires_at: datetime
