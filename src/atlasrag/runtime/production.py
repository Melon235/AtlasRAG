"""Default production business composition over shared runtime resources."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from atlasrag.application.answer import AnswerRunner
from atlasrag.application.document_build import DocumentBuildRunner
from atlasrag.application.index_sync import (
    DocumentRecoveryRunner,
    IndexSyncApplication,
    SyncServices,
)
from atlasrag.application.local_evidence import LocalEvidenceRunner
from atlasrag.application.rag_core import RagCoreRunner
from atlasrag.application.retrieval import RetrievalRunner
from atlasrag.application.web_evidence import WebEvidenceRunner
from atlasrag.config.fingerprint import pipeline_fingerprint, runtime_fingerprint
from atlasrag.domain.errors import ConfigurationError, DependencyUnavailableError
from atlasrag.graphs.answer.subgraph import AnswerServices
from atlasrag.graphs.indexing.contracts import BuildRequest
from atlasrag.graphs.rag_core.subgraph import RagCoreServices
from atlasrag.graphs.retrieval.graph import RetrievalGraphServices
from atlasrag.graphs.web_evidence.subgraph import WebEvidenceServices
from atlasrag.providers.generation.deepseek import (
    DeepSeekGenerationProvider,
    DeepSeekQueryRewriteProvider,
    DeepSeekSummaryProvider,
)
from atlasrag.providers.parsing.docling import DoclingParser
from atlasrag.providers.parsing.markdown import MarkdownParser
from atlasrag.providers.parsing.routing import ParserRouter
from atlasrag.providers.parsing.text import TextParser
from atlasrag.providers.web.fetch import SafeHttpFetcher
from atlasrag.providers.web.searxng import SearxngSearchProvider
from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager
from atlasrag.repositories.postgres.runtime_metadata import (
    RuntimeMetadataRepository,
)
from atlasrag.repositories.postgres.uow import PostgresUnitOfWork
from atlasrag.runtime.composition import (
    BusinessBundle,
    SharedResources,
    validate_shared_resource_contracts,
)
from atlasrag.services.answer.citations import CitationResolver
from atlasrag.services.answer.compression import IdentityCompression
from atlasrag.services.answer.generation_input import GenerationInputBuilder
from atlasrag.services.answer.profile import AnswerProfile
from atlasrag.services.answer.review import OutputReviewService
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.cache.session import SessionQueryCache
from atlasrag.services.indexing.build import BuildResources
from atlasrag.services.local_evidence.parent_context import ParentContextService
from atlasrag.services.local_evidence.profile import (
    DeterministicSufficiencyProfile,
    LocalEvidenceProfile,
)
from atlasrag.services.local_evidence.reranking import RerankerService
from atlasrag.services.local_evidence.retrieval import (
    BM25Retriever,
    DenseRetriever,
    HybridRetrievalService,
)
from atlasrag.services.local_evidence.scope import (
    PublishedRevisionSnapshotService,
    RetrievalFilterCompiler,
    RetrievalScopeBuilder,
)
from atlasrag.services.local_evidence.selection import UniqueEvidenceSelector
from atlasrag.services.local_evidence.service import LocalEvidenceServices
from atlasrag.services.local_evidence.sufficiency import (
    DeterministicEvidenceSufficiency,
)
from atlasrag.services.rag_core.boundary import AnswerInputBuilder
from atlasrag.services.rag_core.completion import CompletionAssessor
from atlasrag.services.rag_core.query import QueryConstructionService
from atlasrag.services.rag_core.routing import CorrectiveRoutingService
from atlasrag.services.sync.current_repair import CurrentIndexRepairService
from atlasrag.services.sync.deletion import MissingSourceDeletionService
from atlasrag.services.sync.execution import BuildExecutionService
from atlasrag.services.sync.models import (
    BuildExecutionSummary,
    DeletionSummary,
    SyncRequest,
)
from atlasrag.services.sync.planner import SyncPlanner
from atlasrag.services.sync.reconciliation import StartupReconciliationService
from atlasrag.services.sync.recovery import DocumentRecoveryService
from atlasrag.services.sync.scanner import KnowledgeDirectoryScanner
from atlasrag.services.sync.snapshot import CanonicalDatabaseSnapshotService
from atlasrag.services.sync.source_safety import StartupSourceSafetyService
from atlasrag.services.sync.verification import GlobalKnowledgeVerificationService
from atlasrag.services.web_evidence.profile import WebEvidenceProfile
from atlasrag.services.web_evidence.service import (
    BatchSummaryWebEvidenceStrategy,
    DirectWebEvidenceStrategy,
    WebPreparationService,
)


class _UnavailableReranker:
    @property
    def identity(self) -> Any:
        raise DependencyUnavailableError

    async def initialize(self, *, allow_download: bool = False) -> None:
        raise DependencyUnavailableError

    async def score(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        raise DependencyUnavailableError


class _DeletionLeases:
    def __init__(self, pool: Any) -> None:
        self._manager = PostgresAdvisoryLockManager(pool)

    async def acquire(self, document_id: str) -> object | None:
        return await self._manager.try_acquire(document_id)


def _uow_factory(pool: Any) -> Any:
    return lambda: PostgresUnitOfWork(pool)


async def build_default_business(shared: SharedResources) -> BusinessBundle:
    """Assemble the existing Part I/II services without alternate algorithms."""

    validate_shared_resource_contracts(shared)
    config = shared.config
    now = lambda: datetime.now(UTC)  # noqa: E731
    factory = _uow_factory(shared.pool)
    pipeline = pipeline_fingerprint(config)

    mineru = shared.guarded_mineru
    parsers = ParserRouter(
        mineru=mineru,
        docling=DoclingParser(),
        markdown=MarkdownParser(),
        text=TextParser(),
    )
    build_resources = BuildResources(
        config=config,
        uow_factory=factory,
        parsers=parsers,
        embedding=shared.embedding,
        index=shared.milvus,
        clock=now,
        index_batch_size=1,
        verification_batch_size=1_000,
    )
    build_runner: DocumentBuildRunner[Any] = DocumentBuildRunner(
        resources=build_resources,
        pool=shared.pool,
        lock_manager_factory=PostgresAdvisoryLockManager,
        metadata_repository_factory=RuntimeMetadataRepository,
        max_concurrent_builds=config.maintenance.max_document_concurrency,
        pool_capacity=config.runtime.postgres_pool_max_size,
    )
    recovery_service = DocumentRecoveryService(
        factory,
        runtime=config,
        embedding=shared.embedding,
        index=shared.milvus,
        clock=now,
        batch_size=1,
        verification_batch_size=1_000,
    )
    recovery_runner = DocumentRecoveryRunner(
        recovery=recovery_service,
        pool=shared.pool,
        lock_manager_factory=PostgresAdvisoryLockManager,
        metadata_repository_factory=RuntimeMetadataRepository,
    )

    async def full_build(
        source: Any, fingerprint: str, expected_revision_id: str | None
    ) -> Any:
        return await build_runner.build(
            BuildRequest(
                source=source,
                expected_pipeline_fingerprint=fingerprint,
                expected_revision_id=expected_revision_id,
            )
        )

    builds = BuildExecutionService(
        full_build=full_build,
        recovery=recovery_runner,  # type: ignore[arg-type]
        max_concurrency=config.maintenance.max_document_concurrency,
    )
    repair = CurrentIndexRepairService(
        factory,
        runtime=config,
        embedding=shared.embedding,
        index=shared.milvus,
        clock=now,
        batch_size=1,
    )
    reconciliation = StartupReconciliationService(
        factory,
        knowledge_directory=shared.knowledge_directory,
        pipeline_fingerprint=pipeline,
        clock=now,
        index=shared.milvus,
        current_repair=repair,
    )
    verification = GlobalKnowledgeVerificationService(factory, shared.milvus, config)
    sync_services = SyncServices(
        reconciliation=reconciliation,
        scanner=KnowledgeDirectoryScanner(),
        database_snapshot=CanonicalDatabaseSnapshotService(factory),
        planner=SyncPlanner(),
        build_execution=builds,
        deletion=MissingSourceDeletionService(
            factory,
            index=shared.milvus,
            leases=_DeletionLeases(shared.pool),  # type: ignore[arg-type]
            knowledge_directory=shared.knowledge_directory,
            clock=now,
        ),
        verification=verification,
        pipeline=pipeline,
    )
    sync_app = IndexSyncApplication(
        sync_services, runtime=config, initialize_index=shared.milvus.initialize
    )

    async def run_sync() -> Any:
        return await sync_app.run(
            SyncRequest(
                knowledge_directory=shared.knowledge_directory,
                expected_pipeline_fingerprint=pipeline,
            )
        )

    async def run_verify() -> Any:
        return await verification.verify(
            deletion_summary=DeletionSummary(),
            build_summary=BuildExecutionSummary(),
        )

    source_safety = StartupSourceSafetyService(
        factory,
        knowledge_directory=shared.knowledge_directory,
        reconciliation=reconciliation,
    )

    deepseek = shared.deepseek_http
    rewrite = DeepSeekQueryRewriteProvider(
        model_id=config.models.query_model_id,
        api_key=config.models.deepseek_api_key,
        client=deepseek,
    )
    generation = DeepSeekGenerationProvider(
        model_id=config.models.generation_model_id,
        api_key=config.models.deepseek_api_key,
        client=deepseek,
    )
    profile = LocalEvidenceProfile()
    snapshot = PublishedRevisionSnapshotService(factory)
    retrieval_service = HybridRetrievalService(
        scope_builder=RetrievalScopeBuilder(),
        snapshot=snapshot,
        compiler=RetrievalFilterCompiler(),
        dense=DenseRetriever(
            embedding=shared.embedding, search=shared.milvus, profile=profile
        ),
        bm25=BM25Retriever(search=shared.milvus, profile=profile),
    )
    reranker_provider: Any
    if shared.reranker is not None:
        reranker_provider = shared.reranker
    else:
        reranker_provider = _UnavailableReranker()
    local_services = LocalEvidenceServices(
        retrieval=retrieval_service,
        reranker=RerankerService(provider=reranker_provider, profile=profile),
        selector=UniqueEvidenceSelector(profile=profile),
        parent_context=ParentContextService(
            factory,
            cache=shared.redis_provider,
            cache_enabled=config.cache.parent_cache_enabled,
            cache_ttl_seconds=config.cache.parent_ttl_seconds,
        ),
        sufficiency=DeterministicEvidenceSufficiency(
            profile=DeterministicSufficiencyProfile()
        ),
        profile=profile,
    )
    local_runner = LocalEvidenceRunner(local_services)

    web_profile = WebEvidenceProfile()
    fetcher = SafeHttpFetcher(
        timeout_seconds=web_profile.fetch_timeout_seconds,
        max_bytes=web_profile.max_response_bytes,
        max_redirects=web_profile.max_redirects,
    )
    preparation = WebPreparationService(fetcher=fetcher, profile=web_profile)
    if config.answer.web_evidence_strategy in {"sanitized-content", "direct"}:
        strategy: Any = DirectWebEvidenceStrategy()
    elif config.answer.web_evidence_strategy == "summary":
        strategy = BatchSummaryWebEvidenceStrategy(
            provider=DeepSeekSummaryProvider(
                model_id=config.models.generation_model_id,
                api_key=config.models.deepseek_api_key,
                client=deepseek,
            )
        )
    else:
        raise ConfigurationError
    web = WebEvidenceRunner(
        WebEvidenceServices(
            search=SearxngSearchProvider(
                base_url=config.infrastructure.searxng_url,
                client=shared.web_http,
                top_k=web_profile.top_k,
            ),
            preparation=preparation,
            strategy=strategy,
            profile=web_profile,
        )
    )

    answer_profile = AnswerProfile()
    if config.answer.compression_strategy != "lossless-v1":
        raise ConfigurationError
    answer = AnswerRunner(
        AnswerServices(
            compression=IdentityCompression(),
            builder=GenerationInputBuilder(profile=answer_profile),
            generation=generation,
            review=OutputReviewService(),
            resolver=CitationResolver(),
            profile=answer_profile,
        )
    )
    rag_core = RagCoreRunner(
        RagCoreServices(
            query_construction=QueryConstructionService(provider=rewrite, clock=now),
            local_evidence=local_runner,
            routing=CorrectiveRoutingService(max_retries=1),
            web_evidence=web,
            answer_input=AnswerInputBuilder(),
            answer=answer,
            completion=CompletionAssessor(),
        )
    )
    cache = SessionQueryCache(
        redis=shared.redis_provider,
        uow_factory=factory,
        runtime_fingerprint=runtime_fingerprint(config),
        ttl_seconds=config.cache.query_ttl_seconds,
        policy=QueryCachePolicy(enabled=config.cache.query_cache_enabled),
        clock=now,
        require_write_fence=True,
    )
    retrieval = RetrievalRunner(RetrievalGraphServices(cache=cache, rag_core=rag_core))

    async def close_business() -> None:
        return None

    return BusinessBundle(
        retrieval=retrieval.run,
        sync=run_sync,
        reconcile=reconciliation.reconcile,
        source_safety=source_safety.check,
        verify=run_verify,
        close=close_business,
    )
