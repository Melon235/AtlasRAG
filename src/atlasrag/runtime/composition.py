"""Single application composition root for shared runtime resources."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import httpx
import redis.asyncio as redis
from pymilvus import MilvusClient  # type: ignore[import-untyped]
from redis.backoff import NoBackoff
from redis.retry import Retry

from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import (
    ConfigurationError,
    DependencyError,
    DependencyUnavailableError,
)
from atlasrag.domain.indexing.models import (
    EmbeddingModelIdentity,
    RepresentationProfileV1,
)
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse
from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.providers.embedding.qwen import (
    QWEN_DIMENSION,
    QWEN_MODEL_ID,
    QWEN_REVISION,
    QwenEmbeddingProvider,
)
from atlasrag.providers.index.milvus import MilvusIndexProvider
from atlasrag.providers.index.schema import COLLECTION_NAME
from atlasrag.providers.parsing.mineru import MinerUParser
from atlasrag.providers.reranker.base import RerankerModelIdentity
from atlasrag.providers.reranker.bge import (
    BGE_RERANKER_MODEL_ID,
    BGE_RERANKER_REVISION,
    BgeRerankerProvider,
)
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
    GuardedMinerUParser,
    GuardedRerankerProvider,
    ResourceGuard,
)
from atlasrag.runtime.tasks import InFlightTaskRegistry
from atlasrag.services.ingestion.chunking import DEFAULT_CHUNKER_BEHAVIOR_VERSION
from atlasrag.services.sync.models import (
    GlobalSafetyResult,
    ReconciliationResult,
    SyncResult,
)
from atlasrag.services.sync.source_safety import SourceSafetyResult

_T = TypeVar("_T")
_MODEL_DEVICE_PATTERN = re.compile(r"^(?:cpu|cuda(?::[0-9]+)?)$")
_SUPPORTED_REPRESENTATION_VERSION = RepresentationProfileV1().representation_version
_SUPPORTED_WEB_EVIDENCE_STRATEGIES = frozenset(
    {"sanitized-content", "direct", "summary"}
)
_TRANSIENT_STARTUP_ERRORS = (DependencyError, TimeoutError, ConnectionError)


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
    guarded_mineru: GuardedMinerUParser
    knowledge_directory: Path
    deepseek_http: httpx.AsyncClient
    web_http: httpx.AsyncClient
    cache_freshness: CacheFreshnessBarrier | None = None


@dataclass(frozen=True)
class BusinessBundle:
    """Existing services composed with the shared resources by the caller."""

    retrieval: Callable[[UserTurnRequest], Awaitable[FinalResponse]]
    sync: Callable[[], Awaitable[SyncResult]]
    reconcile: Callable[[], Awaitable[ReconciliationResult]]
    source_safety: Callable[[], Awaitable[SourceSafetyResult]]
    verify: Callable[[], Awaitable[GlobalSafetyResult]]
    close: Callable[[], Awaitable[None]]


BusinessBuilder = Callable[[SharedResources], Awaitable[BusinessBundle]]


def validate_runtime_composition_config(config: RuntimeConfig) -> None:
    """Fail explicitly if configured model/index/pipeline selectors are unimplemented."""

    models = config.models
    if (
        models.embedding_model_id != QWEN_MODEL_ID
        or models.embedding_model_revision != QWEN_REVISION
        or models.embedding_tokenizer_id != QWEN_MODEL_ID
        or models.embedding_tokenizer_revision != QWEN_REVISION
        or models.reranker_model_id != BGE_RERANKER_MODEL_ID
        or models.reranker_model_revision != BGE_RERANKER_REVISION
        or _MODEL_DEVICE_PATTERN.fullmatch(models.model_device) is None
    ):
        raise ConfigurationError
    part_i = config.part_i
    if (
        part_i.loader_behavior_version != "loader-v1"
        or part_i.parser_behavior_version != "parser-v1"
        or part_i.chunker_behavior_version != DEFAULT_CHUNKER_BEHAVIOR_VERSION
        or part_i.representation_version != _SUPPORTED_REPRESENTATION_VERSION
        or part_i.embedding_dimension != QWEN_DIMENSION
        or part_i.bm25_analyzer != "standard"
        or part_i.milvus_schema_version != "schema-v1"
        or part_i.milvus_index_version != "index-v1"
    ):
        raise ConfigurationError
    retrieval = config.retrieval
    if (
        retrieval.query_construction_version != "query-construction-v1"
        or retrieval.retrieval_semantics_version != "retrieval-v1"
        or retrieval.sufficiency_strategy != "evidence-coverage"
        or retrieval.sufficiency_version != "sufficiency-v1"
        or retrieval.reranker_strategy != "cross-encoder"
    ):
        raise ConfigurationError
    answer = config.answer
    if (
        answer.generation_prompt_version != "generation-v1"
        or answer.web_evidence_strategy not in _SUPPORTED_WEB_EVIDENCE_STRATEGIES
        or answer.compression_strategy != "lossless-v1"
        or answer.citation_policy_version != "citation-v1"
        or answer.output_policy_version != "output-v1"
    ):
        raise ConfigurationError


def _device_matches(identity_device: str, configured: str) -> bool:
    if identity_device == configured:
        return True
    return (
        configured == "cuda"
        and re.fullmatch(r"cuda:[0-9]+", identity_device) is not None
    )


def validate_shared_resource_contracts(shared: SharedResources) -> None:
    """Ensure composed shared resources match validated runtime config."""

    config = shared.config
    validate_runtime_composition_config(config)
    if (
        getattr(shared.milvus, "embedding_dimension", None)
        != config.part_i.embedding_dimension
        or getattr(shared.milvus, "analyzer", None) != config.part_i.bm25_analyzer
    ):
        raise ConfigurationError
    embedding_identity = getattr(shared.embedding, "identity", None)
    if not isinstance(embedding_identity, EmbeddingModelIdentity):
        raise ConfigurationError
    if (
        embedding_identity.model_id != config.models.embedding_model_id
        or embedding_identity.model_revision != config.models.embedding_model_revision
        or embedding_identity.tokenizer_id != config.models.embedding_tokenizer_id
        or embedding_identity.tokenizer_revision
        != config.models.embedding_tokenizer_revision
        or embedding_identity.dimension != config.part_i.embedding_dimension
        or not _device_matches(embedding_identity.device, config.models.model_device)
    ):
        raise ConfigurationError
    if shared.reranker is not None:
        if not config.models.model_device.startswith("cuda"):
            raise ConfigurationError
        reranker_identity = getattr(shared.reranker, "identity", None)
        if not isinstance(reranker_identity, RerankerModelIdentity):
            raise ConfigurationError
        if (
            reranker_identity.model_id != config.models.reranker_model_id
            or reranker_identity.model_revision != config.models.reranker_model_revision
            or not _device_matches(reranker_identity.device, config.models.model_device)
        ):
            raise ConfigurationError


async def _retry_startup_dependency(
    operation: Callable[[], Awaitable[_T]],
    *,
    attempts: int,
    delay_seconds: float,
    on_failure: Callable[[], Awaitable[None]] | None = None,
) -> _T:
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except _TRANSIENT_STARTUP_ERRORS:
            if on_failure is not None:
                try:
                    await on_failure()
                except BaseException:
                    pass
            if attempt >= attempts:
                raise
            await asyncio.sleep(delay_seconds)
    raise AssertionError("unreachable")


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
        self._deepseek_http: httpx.AsyncClient | None = None
        self._web_http: httpx.AsyncClient | None = None
        self._guard = ResourceGuard(capacity=config.runtime.gpu_concurrency)
        self._business: BusinessBundle | None = None
        self._closed = False
        self._cleanup_error: BaseException | None = None

    async def start(
        self, build_business: BusinessBuilder | None = None
    ) -> ApplicationCoordinator:
        if self._closed:
            raise RuntimeError("composition is closed")
        validate_runtime_composition_config(self._config)
        self._ownership.acquire()
        try:
            config = self._config
            startup_attempts = config.runtime.startup_attempts
            startup_delay = config.runtime.startup_retry_delay_seconds

            async def _open_pool() -> PostgresPool:
                pool = PostgresPool(
                    _dsn(config),
                    min_size=config.runtime.postgres_pool_min_size,
                    max_size=config.runtime.postgres_pool_max_size,
                )
                try:
                    await pool.open()
                except BaseException:
                    try:
                        await pool.close()
                    except BaseException:
                        pass
                    raise
                return pool

            self._pool = await _retry_startup_dependency(
                _open_pool,
                attempts=startup_attempts,
                delay_seconds=startup_delay,
            )
            self._deepseek_http = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=10.0)
            )
            self._web_http = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0)
            )
            self._redis_client = redis.from_url(
                f"redis://{config.infrastructure.redis_host}:"
                f"{config.infrastructure.redis_port}/0",
                decode_responses=False,
                socket_connect_timeout=config.runtime.redis_connect_timeout_seconds,
                socket_timeout=config.runtime.redis_socket_timeout_seconds,
                socket_keepalive=True,
                health_check_interval=(
                    config.runtime.redis_health_check_interval_seconds
                ),
                retry_on_timeout=False,
                retry_on_error=[],
                retry=Retry(NoBackoff(), 0),
            )
            self._redis = RedisCacheProvider(self._redis_client)
            client = MilvusClient(
                uri=f"http://{config.infrastructure.milvus_host}:"
                f"{config.infrastructure.milvus_port}",
                timeout=10,
            )
            self._milvus = MilvusIndexProvider(
                client,
                collection_name=COLLECTION_NAME,
                embedding_dimension=config.part_i.embedding_dimension,
                analyzer=config.part_i.bm25_analyzer,
                timeout_seconds=30,
            )
            milvus = self._milvus
            await _retry_startup_dependency(
                milvus.initialize,
                attempts=startup_attempts,
                delay_seconds=startup_delay,
            )
            self._qwen = QwenEmbeddingProvider(device=config.models.model_device)
            guarded_embedding = GuardedEmbeddingProvider(self._qwen, self._guard)
            await _retry_startup_dependency(
                lambda: guarded_embedding.initialize(allow_download=False),
                attempts=startup_attempts,
                delay_seconds=startup_delay,
            )
            guarded_reranker: GuardedRerankerProvider | None = None
            if config.models.model_device.startswith("cuda"):
                candidate_bge = BgeRerankerProvider(device=config.models.model_device)
                candidate_reranker = GuardedRerankerProvider(candidate_bge, self._guard)
                try:
                    await candidate_reranker.initialize(allow_download=False)
                except (
                    DependencyError,
                    TimeoutError,
                    ConnectionError,
                    OSError,
                    RuntimeError,
                ):
                    guarded_reranker = None
                else:
                    self._bge = candidate_bge
                    guarded_reranker = candidate_reranker
            assert self._deepseek_http is not None
            assert self._web_http is not None
            web_http = self._web_http
            searxng_url = str(config.infrastructure.searxng_url)
            probe_timeout = config.runtime.health_probe_timeout_seconds

            async def _probe_searxng() -> None:
                response = await web_http.get(
                    searxng_url,
                    timeout=httpx.Timeout(probe_timeout, connect=probe_timeout),
                    follow_redirects=False,
                )
                if response.status_code >= 400:
                    raise DependencyUnavailableError

            assert self._pool is not None
            pool = self._pool
            freshness = CacheFreshnessBarrier(
                redis=self._redis,
                uow_factory=lambda: PostgresUnitOfWork(pool),
                retry_interval_seconds=config.runtime.cache_recovery_interval_seconds,
            )
            shared = SharedResources(
                config=config,
                ownership=self._ownership,
                pool=self._pool,
                milvus=self._milvus,
                redis_provider=self._redis,
                embedding=guarded_embedding,
                reranker=guarded_reranker,
                guard=self._guard,
                guarded_mineru=GuardedMinerUParser(MinerUParser(), self._guard),
                knowledge_directory=Path(config.infrastructure.knowledge_directory),
                deepseek_http=self._deepseek_http,
                web_http=self._web_http,
                cache_freshness=freshness,
            )
            validate_shared_resource_contracts(shared)
            if build_business is None:
                from atlasrag.runtime.production import build_default_business

                build_business = build_default_business
            business = await build_business(shared)
            self._business = business
            gate = RuntimeGate()
            tasks = InFlightTaskRegistry()
            health = HealthRegistry(
                mode=lambda: gate.mode,
                postgres=self._pool.health,
                milvus=self._milvus.health,
                redis=self._redis.health,
                searxng=_probe_searxng,
                embedding=_initialized,
                reranker=_initialized if guarded_reranker is not None else _unavailable,
                probe_timeout=config.runtime.health_probe_timeout_seconds,
                refresh_interval=config.runtime.health_refresh_interval_seconds,
                lifecycle_generation=lambda: gate.lifecycle_generation,
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
                retrieval=business.retrieval,
                reconcile=business.reconcile,
                source_safety=business.source_safety,
                verify=business.verify,
                close_resources=self._close_resources,
                shutdown_timeout_seconds=config.maintenance.shutdown_timeout_seconds,
                ownership_acquired=True,
            )
            coordinator = ApplicationCoordinator(deps)
            await coordinator._start()
            return coordinator
        except BaseException as primary:
            try:
                await self._close_resources()
            except BaseException as cleanup:
                primary.add_note(f"runtime cleanup failure: {type(cleanup).__name__}")
            else:
                self._ownership.release()
            raise

    async def _close_resources(self) -> None:
        if self._closed:
            if self._cleanup_error is not None:
                raise self._cleanup_error
            return
        self._closed = True
        first: BaseException | None = None
        operations = (
            ("business", self._business.close if self._business else None),
            ("milvus", self._milvus.close if self._milvus else None),
            ("redis", self._redis_client.aclose if self._redis_client else None),
            ("postgres", self._pool.close if self._pool else None),
            (
                "deepseek_http",
                self._deepseek_http.aclose if self._deepseek_http else None,
            ),
            ("web_http", self._web_http.aclose if self._web_http else None),
        )
        for name, close in operations:
            if close is None:
                continue
            try:
                await close()
            except BaseException as error:
                if first is None:
                    first = error
                else:
                    first.add_note(f"runtime cleanup: {name}/{type(error).__name__}")
        if first is not None:
            self._cleanup_error = first
            raise first


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
