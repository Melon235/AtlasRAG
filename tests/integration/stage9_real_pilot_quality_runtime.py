"""Real providers for authorized Open RAGBench pilot Quality Run."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import redis.asyncio as redis
from pydantic import SecretStr
from pymilvus import MilvusClient  # type: ignore[import-untyped]
from stage4.candidate_fakes import config

from atlasrag.application.answer import AnswerRunner
from atlasrag.config.fingerprint import pipeline_fingerprint, runtime_fingerprint
from atlasrag.config.models import RuntimeConfig
from atlasrag.graphs.answer.subgraph import AnswerServices
from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.providers.embedding.qwen import (
    QWEN_MODEL_ID,
    QWEN_REVISION,
    QwenEmbeddingProvider,
)
from atlasrag.providers.generation.deepseek import (
    DeepSeekGenerationProvider,
    DeepSeekQueryRewriteProvider,
)
from atlasrag.providers.index.milvus import MilvusIndexProvider
from atlasrag.providers.reranker.bge import (
    BGE_RERANKER_MODEL_ID,
    BGE_RERANKER_REVISION,
    BgeRerankerProvider,
)
from atlasrag.repositories.postgres.pool import PostgresPool
from atlasrag.repositories.postgres.uow import PostgresUnitOfWork
from atlasrag.services.answer.citations import CitationResolver
from atlasrag.services.answer.compression import IdentityCompression
from atlasrag.services.answer.generation_input import GenerationInputBuilder
from atlasrag.services.answer.profile import AnswerProfile
from atlasrag.services.answer.review import OutputReviewService
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
from benchmarks.pilot.identity import load_pilot_inputs
from benchmarks.pilot.runtime import PilotRuntimeComponents, build_pilot_quality_runtime


def real_runtime() -> RuntimeConfig:
    values = config().model_dump(mode="python")
    values["part_i"]["embedding_dimension"] = 1024
    values["models"]["embedding_model_id"] = QWEN_MODEL_ID
    values["models"]["embedding_model_revision"] = QWEN_REVISION
    values["models"]["embedding_tokenizer_id"] = QWEN_MODEL_ID
    values["models"]["embedding_tokenizer_revision"] = QWEN_REVISION
    values["models"]["reranker_model_id"] = BGE_RERANKER_MODEL_ID
    values["models"]["reranker_model_revision"] = BGE_RERANKER_REVISION
    model = os.environ.get("DEEPSEEK_MODEL_ID", "deepseek-flash")
    values["models"]["query_model_id"] = model
    values["models"]["query_model_revision"] = model
    values["models"]["generation_model_id"] = model
    values["models"]["generation_model_revision"] = model
    return RuntimeConfig.model_validate(values)


async def build_runtime() -> Any:
    pool = PostgresPool(
        os.environ["ATLASRAG_POSTGRES_DSN"], min_size=1, max_size=4, timeout=5.0
    )
    await pool.open()

    def factory() -> PostgresUnitOfWork:
        return PostgresUnitOfWork(pool)

    client = MilvusClient(uri=os.environ["ATLASRAG_MILVUS_URI"], timeout=30)
    index = MilvusIndexProvider(
        client,
        collection_name=os.environ["STAGE9_MILVUS_COLLECTION"],
        embedding_dimension=1024,
        timeout_seconds=30,
    )
    await index.initialize()
    runtime = real_runtime()
    qwen = QwenEmbeddingProvider(device="cuda:0")
    await qwen.initialize()
    bge = BgeRerankerProvider(device="cuda:0")
    await bge.initialize()
    profile = LocalEvidenceProfile(dense_top_k=20, bm25_top_k=20, max_evidence=5)
    local = LocalEvidenceServices(
        retrieval=HybridRetrievalService(
            scope_builder=RetrievalScopeBuilder(),
            snapshot=PublishedRevisionSnapshotService(factory),
            compiler=RetrievalFilterCompiler(),
            dense=DenseRetriever(embedding=qwen, search=index, profile=profile),
            bm25=BM25Retriever(search=index, profile=profile),
        ),
        reranker=RerankerService(provider=bge, profile=profile),
        selector=UniqueEvidenceSelector(profile=profile),
        parent_context=ParentContextService(factory),
        sufficiency=DeterministicEvidenceSufficiency(
            profile=DeterministicSufficiencyProfile(
                sufficient_rerank_score=0.5, ambiguous_rerank_score=0.2
            )
        ),
        profile=profile,
    )
    client_http = httpx.AsyncClient(trust_env=False)
    api_key = SecretStr(os.environ["DEEPSEEK_API_KEY"])
    model = os.environ.get("DEEPSEEK_MODEL_ID", "deepseek-flash")
    qp = DeepSeekQueryRewriteProvider(
        model_id=model, api_key=api_key, client=client_http
    )
    gp = DeepSeekGenerationProvider(model_id=model, api_key=api_key, client=client_http)
    answer = AnswerRunner(
        AnswerServices(
            compression=IdentityCompression(),
            builder=GenerationInputBuilder(profile=AnswerProfile()),
            generation=gp,
            review=OutputReviewService(),
            resolver=CitationResolver(),
            profile=AnswerProfile(),
        )
    )
    redis_client = redis.from_url(
        os.environ["ATLASRAG_TEST_REDIS_URL"], decode_responses=False
    )
    components = PilotRuntimeComponents(
        query_construction=QueryConstructionService(
            provider=qp, clock=lambda: datetime(2026, 11, 23, tzinfo=UTC)
        ),
        local_services=local,
        routing=CorrectiveRoutingService(max_retries=1),
        answer_input=AnswerInputBuilder(),
        answer=answer,
        completion=CompletionAssessor(),
        redis=RedisCacheProvider(redis_client),
        uow_factory=factory,
        runtime=runtime,
        runtime_fingerprint=runtime_fingerprint(runtime),
        pipeline_fingerprint=pipeline_fingerprint(runtime),
        parent_cache_bypassed=True,
        ttl_seconds=60,
    )
    inputs = load_pilot_inputs(
        Path(os.environ["STAGE9_SNAPSHOT"]),
        Path(os.environ["STAGE9_SELECTION"]),
        Path(os.environ["STAGE9_GOLD"]),
    )
    qruntime = build_pilot_quality_runtime(inputs, components)
    usage = SimpleNamespace(query=qp.usage, generation=gp.usage)
    return SimpleNamespace(
        retrieval=qruntime.retrieval,
        observer=qruntime.observer,
        queries=qruntime.queries,
        gold=qruntime.gold,
        pipeline_fingerprint=qruntime.pipeline_fingerprint,
        runtime_fingerprint=qruntime.runtime_fingerprint,
        corpus_ready=True,
        web=qruntime.web,
        cache_gets=lambda: 0,
        cache_sets=lambda: 0,
        provider_usage=usage,
        query_provider=qp,
        generation_provider=gp,
    )
