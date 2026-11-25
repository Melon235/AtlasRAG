"""Single application composition root for shared runtime resources."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
import redis.asyncio as redis
from pymilvus import MilvusClient  # type: ignore[import-untyped]

from atlasrag.config.models import RuntimeConfig
from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.providers.embedding.qwen import QwenEmbeddingProvider
from atlasrag.providers.index.milvus import MilvusIndexProvider
from atlasrag.providers.reranker.bge import BgeRerankerProvider
from atlasrag.repositories.postgres.pool import PostgresPool
from atlasrag.repositories.postgres.uow import PostgresUnitOfWork
from atlasrag.runtime.cache_freshness import CacheFreshnessBarrier
from atlasrag.runtime.coordinator import (
    ApplicationCoordinator,
    CoordinatorDependencies,
)
from atlasrag.runtime.gate import RuntimeGate
from atlasrag.runtime.health import HealthRegistry
from atlasrag.runtime.maintenance import MaintenanceCoordinator
from atlasrag.runtime.ownership import RuntimeOwnershipLock
from atlasrag.runtime.resources import (
    GuardedEmbeddingProvider,
    GuardedRerankerProvider,
    ResourceGuard,
)
from atlasrag.runtime.tasks import InFlightTaskRegistry
from atlasrag.services.sync.models import (
    GlobalSafetyResult,
    ReconciliationResult,
    SyncResult,
)
from atlasrag.services.sync.source_safety import SourceSafetyResult


@dataclass(frozen=True)
class SharedResources:
    config: RuntimeConfig
    ownership: RuntimeOwnershipLock
    pool: PostgresPool
    milvus: MilvusIndexProvider
    redis_provider: RedisCacheProvider
    embedding: GuardedEmbeddingProvider
    reranker: GuardedRerankerProvider | None
    guard: ResourceGuard


@dataclass(frozen=True)
class BusinessBundle:
    """Existing services composed with the shared resources by the caller."""

    retrieval: Callable[[object], Awaitable[object]]
    sync: Callable[[], Awaitable[SyncResult]]
    reconcile: Callable[[], Awaitable[ReconciliationResult]]
    source_safety: Callable[[], Awaitable[SourceSafetyResult]]
    verify: Callable[[], Awaitable[GlobalSafetyResult]]
    close: Callable[[], Awaitable[None]]


BusinessBuilder = Callable[[SharedResources], Awaitable[BusinessBundle]]


class ApplicationComposition:
    """Own shared infrastructure and expose one ApplicationCoordinator."""

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._ownership = RuntimeOwnershipLock()
        self._pool: PostgresPool | None = None
        self._milvus: MilvusIndexProvider | None = None
        self._redis_client: redis.Redis | None = None
        self._redis: RedisCacheProvider | None = None
        self._qwen: QwenEmbeddingProvider | None = None
        self._bge: BgeRerankerProvider | None = None
        self._http: httpx.AsyncClient | None = None
        self._guard = ResourceGuard(capacity=config.runtime.gpu_concurrency)
        self._business: BusinessBundle | None = None
        self._closed = False

    async def start(self, build_business: BusinessBuilder) -> ApplicationCoordinator:
        if self._closed:
            raise RuntimeError("composition is closed")
        self._ownership.acquire()
        try:
            config = self._config
            self._pool = PostgresPool(
                _dsn(config),
                min_size=config.runtime.postgres_pool_min_size,
                max_size=config.runtime.postgres_pool_max_size,
            )
            await self._pool.open()
            self._http = httpx.AsyncClient()
            self._redis_client = redis.from_url(
                f"redis://{config.infrastructure.redis_host}:"
                f"{config.infrastructure.redis_port}/0",
                decode_responses=False,
            )
            self._redis = RedisCacheProvider(self._redis_client)
            client = MilvusClient(
                uri=f"http://{config.infrastructure.milvus_host}:"
                f"{config.infrastructure.milvus_port}",
                timeout=10,
            )
            self._milvus = MilvusIndexProvider(
                client,
                collection_name="atlasrag_chunks",
                embedding_dimension=config.part_i.embedding_dimension,
                timeout_seconds=30,
            )
            await self._milvus.initialize()
            self._qwen = QwenEmbeddingProvider(device=config.models.model_device)
            guarded_embedding = GuardedEmbeddingProvider(self._qwen, self._guard)
            await guarded_embedding.initialize(allow_download=False)
            guarded_reranker: GuardedRerankerProvider | None = None
            if config.models.model_device.startswith("cuda"):
                self._bge = BgeRerankerProvider(device=config.models.model_device)
                guarded_reranker = GuardedRerankerProvider(self._bge, self._guard)
                await guarded_reranker.initialize(allow_download=False)
            shared = SharedResources(
                config=config,
                ownership=self._ownership,
                pool=self._pool,
                milvus=self._milvus,
                redis_provider=self._redis,
                embedding=guarded_embedding,
                reranker=guarded_reranker,
                guard=self._guard,
            )
            business = await build_business(shared)
            self._business = business
            gate = RuntimeGate()
            tasks = InFlightTaskRegistry()
            health = HealthRegistry(
                mode=lambda: gate.mode,
                postgres=self._pool.health,
                milvus=self._milvus.health,
                redis=self._redis.health,
                embedding=_initialized,
                reranker=_initialized if guarded_reranker is not None else _unavailable,
                probe_timeout=config.runtime.health_probe_timeout_seconds,
                refresh_interval=config.runtime.health_refresh_interval_seconds,
            )
            assert self._pool is not None
            pool = self._pool
            freshness = CacheFreshnessBarrier(
                redis=self._redis,
                uow_factory=lambda: PostgresUnitOfWork(pool),
                retry_interval_seconds=config.runtime.cache_recovery_interval_seconds,
            )
            maintenance = MaintenanceCoordinator(
                gate=gate,
                run_sync=business.sync,
                freshness=freshness,
                drain_timeout_seconds=config.maintenance.drain_timeout_seconds,
            )
            deps = CoordinatorDependencies(
                ownership=self._ownership,
                gate=gate,
                tasks=tasks,
                health=health,
                freshness=freshness,
                maintenance=maintenance,
                retrieval=business.retrieval,  # type: ignore[arg-type]
                reconcile=business.reconcile,
                source_safety=business.source_safety,
                verify=business.verify,
                close_resources=self._close_resources,
                shutdown_timeout_seconds=config.maintenance.shutdown_timeout_seconds,
            )
            coordinator = ApplicationCoordinator(deps)
            await coordinator._start()
            return coordinator
        except BaseException:
            await self._close_resources()
            self._ownership.release()
            raise

    async def _close_resources(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._business is not None:
                await self._business.close()
        finally:
            if self._milvus is not None:
                await self._milvus.close()
            if self._redis_client is not None:
                await self._redis_client.aclose()
            if self._pool is not None:
                await self._pool.close()
            if self._http is not None:
                await self._http.aclose()


async def _initialized() -> None:
    return None


async def _unavailable() -> None:
    raise RuntimeError("reranker is not configured")


def _dsn(config: RuntimeConfig) -> str:
    i = config.infrastructure
    return (
        f"postgresql://{i.postgres_user}:{i.postgres_password.get_secret_value()}"
        f"@{i.postgres_host}:{i.postgres_port}/{i.postgres_database}"
    )
