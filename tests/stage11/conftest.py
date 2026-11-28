"""Shared deterministic helpers for Stage 11 WP-A tests."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from atlasrag.application.retrieval import RetrievalExecution
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse
from atlasrag.providers.embedding.qwen import (
    QWEN_DIMENSION,
    QWEN_MODEL_ID,
    QWEN_REVISION,
)
from atlasrag.providers.reranker.bge import (
    BGE_RERANKER_MODEL_ID,
    BGE_RERANKER_REVISION,
)
from atlasrag.repositories.postgres.records import RuntimeMetadata
from atlasrag.runtime.cache_freshness import CacheFreshnessBarrier
from atlasrag.runtime.coordinator import ApplicationCoordinator, CoordinatorDependencies
from atlasrag.runtime.gate import RuntimeGate
from atlasrag.runtime.health import DependencyHealth, HealthRegistry
from atlasrag.runtime.maintenance import MaintenanceCoordinator
from atlasrag.runtime.ownership import RuntimeOwnershipLock
from atlasrag.runtime.tasks import InFlightTaskRegistry
from atlasrag.server.config import ServerConfig
from atlasrag.services.cache.session import QueryCacheStatus
from atlasrag.services.ingestion.chunking import DEFAULT_CHUNKER_BEHAVIOR_VERSION
from atlasrag.services.sync.models import (
    GlobalSafetyResult,
    ReconciliationOutcome,
    ReconciliationResult,
    ServingSafety,
    SyncOutcome,
    SyncResult,
)
from atlasrag.services.sync.source_safety import SourceSafetyResult

ProbeCallable = Callable[[], Awaitable[None]]


def sample_runtime_config() -> RuntimeConfig:
    return RuntimeConfig.model_validate(
        {
            "infrastructure": {
                "knowledge_directory": "/tmp/atlasrag-production",
                "postgres_host": "postgres",
                "postgres_port": 5432,
                "postgres_database": "atlasrag",
                "postgres_user": "atlasrag",
                "postgres_password": "postgres-secret",
                "redis_host": "redis",
                "redis_port": 6379,
                "milvus_host": "milvus",
                "milvus_port": 19530,
                "searxng_url": "http://searxng:8080",
            },
            "models": {
                "embedding_model_id": QWEN_MODEL_ID,
                "embedding_model_revision": QWEN_REVISION,
                "embedding_tokenizer_id": QWEN_MODEL_ID,
                "embedding_tokenizer_revision": QWEN_REVISION,
                "reranker_model_id": BGE_RERANKER_MODEL_ID,
                "reranker_model_revision": BGE_RERANKER_REVISION,
                "query_model_id": "deepseek-query",
                "query_model_revision": "query-r1",
                "generation_model_id": "deepseek-generation",
                "generation_model_revision": "generation-r1",
                "deepseek_api_key": "deepseek-secret",
                "model_device": "cuda",
            },
            "part_i": {
                "loader_behavior_version": "loader-v1",
                "parser_behavior_version": "parser-v1",
                "chunker_behavior_version": DEFAULT_CHUNKER_BEHAVIOR_VERSION,
                "representation_version": "representation-v1",
                "embedding_dimension": QWEN_DIMENSION,
                "bm25_analyzer": "standard",
                "milvus_schema_version": "schema-v1",
                "milvus_index_version": "index-v1",
            },
            "retrieval": {
                "query_construction_version": "query-construction-v1",
                "retrieval_semantics_version": "retrieval-v1",
                "sufficiency_strategy": "evidence-coverage",
                "sufficiency_version": "sufficiency-v1",
                "reranker_strategy": "cross-encoder",
            },
            "answer": {
                "generation_prompt_version": "generation-v1",
                "web_evidence_strategy": "sanitized-content",
                "compression_strategy": "lossless-v1",
                "citation_policy_version": "citation-v1",
                "output_policy_version": "output-v1",
            },
            "cache": {
                "query_cache_enabled": True,
                "parent_cache_enabled": True,
                "query_ttl_seconds": 600,
                "parent_ttl_seconds": 3600,
            },
            "maintenance": {
                "max_document_concurrency": 2,
                "drain_timeout_seconds": 30.0,
                "shutdown_timeout_seconds": 10.0,
            },
            "observability": {
                "trace_sample_rate": 0.25,
                "log_level": "INFO",
                "log_directory": "/var/log/atlasrag",
                "log_queries": False,
            },
            "runtime": {"postgres_pool_max_size": 16},
        }
    )


def sample_server_config(*, port: int = 8000) -> ServerConfig:
    return ServerConfig(port=port)


def sample_request(
    session_id: str = "session-1", query: str = "What is AtlasRAG?"
) -> UserTurnRequest:
    return UserTurnRequest(session_id=session_id, query=query)


def sample_response(text: str = "AtlasRAG answer.") -> FinalResponse:
    return FinalResponse(answer_text=text, citations=())


async def healthy_probe() -> None:
    return None


@dataclass
class FakeRuntimeMetadataRepo:
    invalidation_required: bool = False

    async def get(self) -> RuntimeMetadata:
        return RuntimeMetadata(
            query_cache_invalidation_required=self.invalidation_required
        )

    async def set_query_cache_invalidation_required(
        self, required: bool
    ) -> RuntimeMetadata:
        self.invalidation_required = required
        return RuntimeMetadata(
            query_cache_invalidation_required=self.invalidation_required
        )


class FakeSyncUow:
    def __init__(self, metadata_repo: FakeRuntimeMetadataRepo) -> None:
        self.runtime_metadata = metadata_repo

    async def __aenter__(self) -> FakeSyncUow:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None


class FakeRedisBarrierClient:
    async def invalidate_queries(self) -> int:
        return 0

    async def establish_query_write_fence(self) -> str:
        return "fence-1"

    async def ensure_query_write_fence(self) -> str:
        return "fence-1"

    async def revoke_query_write_fence(self) -> None:
        return None


def _isolated_ownership_lock() -> RuntimeOwnershipLock:
    lock_dir = Path(tempfile.mkdtemp(prefix="atlasrag-s11-lock-"))
    os.chmod(lock_dir, 0o700)
    return RuntimeOwnershipLock(lock_dir / "runtime.lock")


@dataclass
class HarnessObjects:
    ownership: RuntimeOwnershipLock = field(default_factory=_isolated_ownership_lock)
    gate: RuntimeGate = field(default_factory=RuntimeGate)
    tasks: InFlightTaskRegistry = field(default_factory=InFlightTaskRegistry)
    metadata_repo: FakeRuntimeMetadataRepo = field(
        default_factory=FakeRuntimeMetadataRepo
    )


def build_test_dependencies(
    *,
    retrieval: Callable[[UserTurnRequest], Awaitable[FinalResponse]] | None = None,
    retrieval_with_facts: Callable[[UserTurnRequest], Awaitable[RetrievalExecution]]
    | None = None,
    sync_fn: Callable[[], Awaitable[SyncResult]] | None = None,
    reconcile_fn: Callable[[], Awaitable[ReconciliationResult]] | None = None,
    source_safety_fn: Callable[[], Awaitable[SourceSafetyResult]] | None = None,
    verify_fn: Callable[[], Awaitable[GlobalSafetyResult]] | None = None,
    open_resources: Callable[[], Awaitable[None]] | None = None,
    close_resources: Callable[[], Awaitable[None]] | None = None,
    on_shutdown_settled: Callable[[], Awaitable[None] | None] | None = None,
    probes: dict[str, ProbeCallable | None] | None = None,
    query_cache_status: Callable[[DependencyHealth], str] | None = None,
    refresh_interval: float = 60.0,
    manual_cooldown: float = 1.0,
    shutdown_timeout_seconds: float = 2.0,
) -> tuple[CoordinatorDependencies, HarnessObjects]:
    harness = HarnessObjects()
    freshness = CacheFreshnessBarrier(
        redis=cast(Any, FakeRedisBarrierClient()),
        uow_factory=cast(Any, lambda: FakeSyncUow(harness.metadata_repo)),
    )
    resolved_probes: dict[str, ProbeCallable | None] = {
        "postgres": healthy_probe,
        "milvus": healthy_probe,
        "redis": healthy_probe,
        "searxng": healthy_probe,
        "embedding": healthy_probe,
        "reranker": healthy_probe,
    }
    if probes is not None:
        resolved_probes.update(probes)
    postgres_probe = resolved_probes["postgres"] or healthy_probe
    milvus_probe = resolved_probes["milvus"] or healthy_probe
    redis_probe = resolved_probes["redis"] or healthy_probe
    embedding_probe = resolved_probes["embedding"] or healthy_probe
    health = HealthRegistry(
        mode=lambda: harness.gate.mode,
        lifecycle_generation=lambda: harness.gate.lifecycle_generation,
        postgres=postgres_probe,
        milvus=milvus_probe,
        redis=redis_probe,
        searxng=resolved_probes["searxng"],
        embedding=embedding_probe,
        reranker=resolved_probes["reranker"],
        refresh_interval=refresh_interval,
        query_cache_status=query_cache_status,
    )

    async def default_sync() -> SyncResult:
        return SyncResult(
            outcome=SyncOutcome.SUCCESS, serving_safety=ServingSafety.SAFE_TO_SERVE
        )

    async def default_reconcile() -> ReconciliationResult:
        return ReconciliationResult(
            outcome=ReconciliationOutcome.CLEAN,
            checked_documents=1,
        )

    async def default_source_safety() -> SourceSafetyResult:
        return SourceSafetyResult(safe=True, checked_documents=1)

    async def default_verify() -> GlobalSafetyResult:
        return GlobalSafetyResult(serving_safety=ServingSafety.SAFE_TO_SERVE)

    async def default_retrieval(request: UserTurnRequest) -> FinalResponse:
        return sample_response()

    async def default_open() -> None:
        return None

    async def default_close() -> None:
        return None

    maintenance = MaintenanceCoordinator(
        gate=harness.gate,
        freshness=freshness,
        run_sync=sync_fn or default_sync,
        drain_timeout_seconds=1.0,
    )
    deps = CoordinatorDependencies(
        ownership=harness.ownership,
        gate=harness.gate,
        tasks=harness.tasks,
        health=health,
        freshness=freshness,
        maintenance=maintenance,
        retrieval=retrieval or default_retrieval,
        retrieval_with_facts=retrieval_with_facts,
        reconcile=reconcile_fn or default_reconcile,
        source_safety=source_safety_fn or default_source_safety,
        verify=verify_fn or default_verify,
        open_resources=open_resources or default_open,
        close_resources=close_resources or default_close,
        on_shutdown_settled=on_shutdown_settled,
        manual_health_refresh_cooldown_seconds=manual_cooldown,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
    )
    return deps, harness


async def start_test_coordinator(
    *,
    retrieval_cache_status: QueryCacheStatus | None = None,
    **kwargs: object,
) -> tuple[ApplicationCoordinator, HarnessObjects]:
    if retrieval_cache_status is not None and "retrieval_with_facts" not in kwargs:
        status_value = retrieval_cache_status

        async def _run_with_facts(_request: UserTurnRequest) -> RetrievalExecution:
            return RetrievalExecution(
                response=sample_response(),
                cache_status=status_value,
            )

        kwargs["retrieval_with_facts"] = _run_with_facts
    deps, harness = build_test_dependencies(**cast(dict[str, Any], kwargs))
    coordinator = await ApplicationCoordinator.start(deps)
    return coordinator, harness
