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
        return ReadinessResponse(ready=ready, mode=self._coordinator.gate.mode)

    async def status(self, *, refresh: bool = False) -> RuntimeStatusResponse:
        if self._coordinator.gate.mode is RuntimeMode.SHUTTING_DOWN:
            raise RuntimeShuttingDownError
        snapshot = await self._coordinator.status(refresh=refresh)
        ready = (
            snapshot.runtime is RuntimeMode.SERVING
            and snapshot.hard_healthy
            and self._coordinator.gate.mode is RuntimeMode.SERVING
        )
        return RuntimeStatusResponse(
            mode=snapshot.runtime,
            ready=ready,
            knowledge_verified=snapshot.knowledge_verified,
            active_queries=self._coordinator.gate.active_queries,
            dependencies=DependencyStatusMap(
                postgres=snapshot.postgres,
                milvus=snapshot.milvus,
                redis=snapshot.redis,
                searxng=snapshot.searxng,
                embedding=snapshot.embedding,
                reranker=snapshot.reranker,
            ),
            query_cache=snapshot.query_cache,
            pipeline_fingerprint=self._pipeline_fp,
            runtime_fingerprint=self._runtime_fp,
            checked_at=snapshot.checked_at,
        )
