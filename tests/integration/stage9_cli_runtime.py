"""Runtime factory used by the subprocess acceptance test; synthetic only."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import redis.asyncio as redis
from pymilvus import MilvusClient  # type: ignore[import-untyped]
from stage7.helpers import services as answer_services

from atlasrag.config.fingerprint import pipeline_fingerprint, runtime_fingerprint
from atlasrag.domain.answer import GenerationResult
from atlasrag.domain.requests import LocalRetrievalRequest, UserTurnRequest
from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.providers.index.milvus import MilvusIndexProvider
from atlasrag.repositories.postgres.pool import PostgresPool
from atlasrag.repositories.postgres.uow import PostgresUnitOfWork
from atlasrag.services.rag_core.boundary import AnswerInputBuilder
from atlasrag.services.rag_core.completion import CompletionAssessor
from atlasrag.services.rag_core.query import QueryConstructionResult
from atlasrag.services.rag_core.routing import CorrectiveRoutingService
from atlasrag.services.sync.models import GlobalSafetyResult
from benchmarks.contracts.models import BenchmarkQuery, Modality
from benchmarks.ingestion.readiness_collector import collect_production_readiness
from benchmarks.wiring.quality import (
    CollectingObserver,
    QualityRuntime,
    build_quality_retrieval,
)
from integration.stage5_helpers import application, request, runtime_config
from integration.test_stage6_local_evidence import services as local_services


class FakeQuery:
    async def construct(self, request: UserTurnRequest) -> QueryConstructionResult:
        return QueryConstructionResult(
            request=LocalRetrievalRequest(
                original_query=request.query,
                retrieval_query=request.query,
                filters=request.filters,
            ),
            attempts=1,
            used_fallback=False,
        )


class FakeGeneration:
    async def generate(self, input: object) -> GenerationResult:
        return GenerationResult(answer_text="Answer [L1].", used_citation_ids=("L1",))


async def build_runtime() -> QualityRuntime:
    dsn = os.environ["ATLASRAG_POSTGRES_DSN"]
    milvus_uri = os.environ["ATLASRAG_MILVUS_URI"]
    collection = os.environ["STAGE9_MILVUS_COLLECTION"]
    root = Path(os.environ["STAGE9_SYNTHETIC_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    (root / "guide.md").write_text(
        "# Guide\n\nAtlasRAG maintenance interval is 30 days.\n", encoding="utf-8"
    )
    pool = PostgresPool(dsn, min_size=1, max_size=4, timeout=2.0)
    await pool.open()

    def factory() -> PostgresUnitOfWork:
        return PostgresUnitOfWork(pool)

    client = MilvusClient(uri=milvus_uri, timeout=10)
    index = MilvusIndexProvider(
        client, collection_name=collection, embedding_dimension=3, timeout_seconds=10
    )
    await index.initialize()
    runtime = runtime_config()
    sync = application(factory, pool, index, root)
    sync_result = await sync.run(request(root))
    if sync_result.serving_safety.value != "SAFE_TO_SERVE":
        raise RuntimeError("synthetic corpus not SAFE_TO_SERVE")
    # Fresh authoritative verification evidence.
    from atlasrag.services.sync.models import BuildExecutionSummary, DeletionSummary
    from atlasrag.services.sync.verification import GlobalKnowledgeVerificationService

    verifier = GlobalKnowledgeVerificationService(factory, index, runtime)

    async def verify() -> GlobalSafetyResult:
        return await verifier.verify(
            deletion_summary=DeletionSummary(),
            build_summary=BuildExecutionSummary(),
        )

    evidence = await collect_production_readiness(
        cast(Any, factory),
        runtime=runtime,
        verifier=verify,
        expected_source_paths=("guide.md",),
        expected_pipeline_fingerprint=pipeline_fingerprint(runtime),
        snapshot_identity=os.environ.get("STAGE9_SNAPSHOT_ID", "synthetic-snapshot"),
    )
    if evidence.serving_safety != "SAFE_TO_SERVE":
        raise RuntimeError("fresh readiness verification failed")
    local_bundle, _, _ = local_services(factory, index)
    observer = CollectingObserver()
    local_bundle = replace(local_bundle, observer=observer)
    redis_client = redis.from_url(
        os.environ["ATLASRAG_TEST_REDIS_URL"], decode_responses=False
    )

    retrieval, web, cache = build_quality_retrieval(
        query_construction=FakeQuery(),  # type: ignore[arg-type]
        local_evidence=__import__(
            "atlasrag.application.local_evidence", fromlist=["LocalEvidenceRunner"]
        ).LocalEvidenceRunner(local_bundle),
        routing=CorrectiveRoutingService(max_retries=1),
        answer_input=AnswerInputBuilder(),
        answer=__import__(
            "atlasrag.application.answer", fromlist=["AnswerRunner"]
        ).AnswerRunner(answer_services(FakeGeneration())),
        completion=CompletionAssessor(),
        observer=observer,
        redis=RedisCacheProvider(redis_client),
        uow_factory=factory,
        runtime_fingerprint=runtime_fingerprint(runtime),
        ttl_seconds=60,
        clock=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
        parent_cache_bypassed=True,
    )
    queries = (
        BenchmarkQuery(
            query_id="q1",
            text="What is the maintenance interval?",
            modality=Modality.TEXT,
            query_type="extractive",
        ),
        BenchmarkQuery(
            query_id="q2",
            text="Which maintenance interval does the manual require?",
            modality=Modality.TEXT,
            query_type="extractive",
        ),
    )
    return QualityRuntime(
        retrieval=retrieval,
        observer=observer,
        queries=queries,
        gold={},
        pipeline_fingerprint=pipeline_fingerprint(runtime),
        runtime_fingerprint=runtime_fingerprint(runtime),
        corpus_ready=True,
        web=web,
        cache_gets=lambda: 0,
        cache_sets=lambda: 0,
    )
