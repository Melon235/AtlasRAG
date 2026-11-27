"""Strict Pydantic v2 transport DTOs independent of FastAPI/Starlette."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator

from atlasrag._canonical import validate_unicode_scalar_text
from atlasrag.domain.base import FrozenModel, StrictReal
from atlasrag.domain.requests import QueryFilters
from atlasrag.domain.results import FinalResponse
from atlasrag.runtime.health import DependencyHealth
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.services.cache.session import QueryCacheStatus


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
