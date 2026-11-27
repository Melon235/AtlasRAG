"""Framework-independent application service boundary over ApplicationCoordinator."""

from __future__ import annotations

from pydantic import BaseModel

from atlasrag.config.fingerprint import pipeline_fingerprint, runtime_fingerprint
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import ErrorCode, RuntimeShuttingDownError
from atlasrag.runtime.coordinator import ApplicationCoordinator
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.server.config import ServerConfig
from atlasrag.server.errors import ServerHttpError
from atlasrag.server.schemas import (
    DependencyStatusMap,
    LivenessResponse,
    LivenessStatus,
    ReadinessResponse,
    RuntimeStatusResponse,
)


def serialize_bounded_response(model: BaseModel, *, max_bytes: int) -> bytes:
    """Serialize a response DTO and fail closed if it exceeds max_bytes."""

    encoded = model.model_dump_json().encode("utf-8")
    if len(encoded) > max_bytes:
        raise ServerHttpError(
            ErrorCode.INVARIANT_VIOLATION,
            "Serialized response exceeds maximum allowed size.",
        )
    return encoded


class ApplicationServerService:
    """Thin transport-agnostic boundary delegating solely to ApplicationCoordinator."""

    def __init__(
        self,
        *,
        coordinator: ApplicationCoordinator,
        runtime_config: RuntimeConfig,
        server_config: ServerConfig,
    ) -> None:
        self._coordinator = coordinator
        self._runtime_config = runtime_config
        self._server_config = server_config
        self._pipeline_fp = pipeline_fingerprint(runtime_config)
        self._runtime_fp = runtime_fingerprint(runtime_config)

    @property
    def coordinator(self) -> ApplicationCoordinator:
        return self._coordinator

    @property
    def server_config(self) -> ServerConfig:
        return self._server_config

    def liveness(self) -> LivenessResponse:
        mode = self._coordinator.gate.mode
        status = (
            LivenessStatus.SHUTTING_DOWN
            if mode is RuntimeMode.SHUTTING_DOWN
            else LivenessStatus.ALIVE
        )
        return LivenessResponse(status=status, mode=mode)

    async def readiness(self) -> ReadinessResponse:
        ready = await self._coordinator.readiness()
        mode = self._coordinator.gate.mode
        return ReadinessResponse(
            ready=ready and mode is RuntimeMode.SERVING,
            mode=mode,
        )

    async def status(self, *, refresh: bool = False) -> RuntimeStatusResponse:
        gate = self._coordinator.gate
        if gate.mode is RuntimeMode.SHUTTING_DOWN:
            raise RuntimeShuttingDownError
        snapshot = await self._coordinator.status(
            refresh=refresh,
            min_refresh_interval_seconds=(
                self._server_config.min_manual_refresh_interval_seconds
            ),
        )
        current_mode: RuntimeMode = self._coordinator.gate.mode
        current_lifecycle_generation = self._coordinator.gate.lifecycle_generation
        if current_mode is RuntimeMode.SHUTTING_DOWN:
            raise RuntimeShuttingDownError
        lifecycle_consistent = (
            snapshot.lifecycle_generation == current_lifecycle_generation
        )
        ready = (
            lifecycle_consistent
            and current_mode is RuntimeMode.SERVING
            and snapshot.runtime is RuntimeMode.SERVING
            and snapshot.hard_healthy
        )
        knowledge_verified = (
            snapshot.knowledge_verified if lifecycle_consistent else False
        )
        query_cache = (
            "BYPASSED"
            if (not lifecycle_consistent and snapshot.query_cache == "ACTIVE")
            else snapshot.query_cache
        )
        return RuntimeStatusResponse(
            mode=current_mode,
            ready=ready,
            knowledge_verified=knowledge_verified,
            active_queries=gate.active_queries,
            dependencies=DependencyStatusMap(
                postgres=snapshot.postgres,
                milvus=snapshot.milvus,
                redis=snapshot.redis,
                searxng=snapshot.searxng,
                embedding=snapshot.embedding,
                reranker=snapshot.reranker,
            ),
            query_cache=query_cache,
            pipeline_fingerprint=self._pipeline_fp,
            runtime_fingerprint=self._runtime_fp,
            checked_at=snapshot.checked_at,
        )
