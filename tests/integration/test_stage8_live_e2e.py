"""One synthetic real-model/storage Stage 8 end-to-end acceptance."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import redis.asyncio as redis
from pydantic import SecretStr
from stage3.test_candidate_services import NOW
from stage4.candidate_fakes import config

from atlasrag.application.answer import AnswerRunner
from atlasrag.application.document_build import DocumentBuildRunner
from atlasrag.application.hashing import request_hash
from atlasrag.application.local_evidence import LocalEvidenceRunner
from atlasrag.application.rag_core import RagCoreRunner
from atlasrag.application.retrieval import RetrievalRunner
from atlasrag.config.fingerprint import pipeline_fingerprint, runtime_fingerprint
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.answer import GenerationResult
from atlasrag.domain.enums import WebExecutionStatus
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.web import WebEvidenceResult
from atlasrag.graphs.rag_core.subgraph import RagCoreServices
from atlasrag.graphs.retrieval.graph import RetrievalGraphServices
from atlasrag.providers.cache.keys import query_cache_key
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
from atlasrag.providers.parsing.docling import DoclingParser
from atlasrag.providers.parsing.markdown import MarkdownParser
from atlasrag.providers.parsing.mineru import MinerUParser
from atlasrag.providers.parsing.routing import ParserRouter
from atlasrag.providers.parsing.text import TextParser
from atlasrag.providers.reranker.bge import BgeRerankerProvider
from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager
from atlasrag.repositories.postgres.runtime_metadata import RuntimeMetadataRepository
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
from atlasrag.services.rag_core.query import (
    QueryConstructionService,
    QueryRewriteOutput,
)
from atlasrag.services.rag_core.routing import CorrectiveRoutingService
from integration.test_milvus import isolated_milvus as isolated_milvus

pytestmark = pytest.mark.integration


class FakeWeb:
    """Fixture-only Web runner; this is a local RAG E2E, not live Web."""

    async def run(self, request: object) -> WebEvidenceResult:
        return WebEvidenceResult(execution_status=WebExecutionStatus.EMPTY, evidence=())


class CountingQueryProvider:
    def __init__(self, provider: DeepSeekQueryRewriteProvider) -> None:
        self.provider = provider
        self.calls = 0

    async def rewrite(
        self, query: str, *, now: str, timezone_name: str
    ) -> QueryRewriteOutput:
        self.calls += 1
        return await self.provider.rewrite(query, now=now, timezone_name=timezone_name)


class CountingGenerationProvider:
    def __init__(self, provider: DeepSeekGenerationProvider) -> None:
        self.provider = provider
        self.calls = 0

    async def generate(self, input: object) -> GenerationResult:
        self.calls += 1
        return await self.provider.generate(input)  # type: ignore[arg-type]


def runtime_config() -> RuntimeConfig:
    values = config().model_dump(mode="python")
    values["part_i"]["embedding_dimension"] = 1024
    values["models"]["embedding_model_id"] = QWEN_MODEL_ID
    values["models"]["embedding_model_revision"] = QWEN_REVISION
    values["models"]["embedding_tokenizer_id"] = QWEN_MODEL_ID
    values["models"]["embedding_tokenizer_revision"] = QWEN_REVISION
    return RuntimeConfig.model_validate(values)


@pytest.mark.asyncio
async def test_live_stage8_e2e_real_qwen_bge_deepseek_pg_milvus_redis(
    postgres_uow_factory: Any,
    postgres_pool: Any,
    isolated_milvus: Any,
    tmp_path: Path,
) -> None:
    runtime = runtime_config()
    index = MilvusIndexProvider(
        cast(Any, isolated_milvus.client),
        collection_name=isolated_milvus.collection_name,
        embedding_dimension=1024,
        timeout_seconds=10.0,
    )
    await index.initialize()
    qwen = QwenEmbeddingProvider(device="cuda:0")
    await qwen.initialize()
    bge = BgeRerankerProvider(device="cuda:0")
    await bge.initialize()
    resources = BuildResources(
        config=runtime,
        uow_factory=postgres_uow_factory,
        parsers=ParserRouter(
            MinerUParser(), DoclingParser(), MarkdownParser(), TextParser()
        ),
        embedding=qwen,
        index=index,
        clock=lambda: NOW,
        index_batch_size=1,
        verification_batch_size=1,
    )
    runner = DocumentBuildRunner(
        resources=resources,
        pool=postgres_pool,
        lock_manager_factory=PostgresAdvisoryLockManager,
        metadata_repository_factory=RuntimeMetadataRepository,
        max_concurrent_builds=1,
        pool_capacity=2,
    )
    path = tmp_path / "manual.md"
    path.write_text(
        "# Manual\n\nThe sample manual specifies a maintenance interval of 30 days.\n",
        encoding="utf-8",
    )
    from atlasrag.domain.ingestion.source import SourceFile
    from atlasrag.graphs.indexing.contracts import BuildRequest

    await runner.build(
        BuildRequest(
            source=SourceFile.from_knowledge_directory(tmp_path, path.name),
            expected_pipeline_fingerprint=pipeline_fingerprint(runtime),
        )
    )
    async with postgres_uow_factory() as unit:
        await unit.runtime_metadata.set_query_cache_invalidation_required(False)
        await unit.commit()

    profile = LocalEvidenceProfile(dense_top_k=20, bm25_top_k=20, max_evidence=5)
    local = LocalEvidenceServices(
        retrieval=HybridRetrievalService(
            scope_builder=RetrievalScopeBuilder(),
            snapshot=PublishedRevisionSnapshotService(postgres_uow_factory),
            compiler=RetrievalFilterCompiler(),
            dense=DenseRetriever(embedding=qwen, search=index, profile=profile),
            bm25=BM25Retriever(search=index, profile=profile),
        ),
        reranker=RerankerService(provider=bge, profile=profile),
        selector=UniqueEvidenceSelector(profile=profile),
        parent_context=ParentContextService(postgres_uow_factory),
        sufficiency=DeterministicEvidenceSufficiency(
            profile=DeterministicSufficiencyProfile(
                sufficient_rerank_score=0.5, ambiguous_rerank_score=0.2
            )
        ),
        profile=profile,
    )
    async with httpx.AsyncClient(trust_env=False) as client:
        api_key = os.environ["DEEPSEEK_API_KEY"]
        model = os.environ.get("DEEPSEEK_MODEL_ID", "deepseek-chat")
        query_provider = CountingQueryProvider(
            DeepSeekQueryRewriteProvider(
                model_id=model, api_key=SecretStr(api_key), client=client
            )
        )
        generation = CountingGenerationProvider(
            DeepSeekGenerationProvider(
                model_id=model, api_key=SecretStr(api_key), client=client
            )
        )
        answer = AnswerRunner(
            __import__(
                "atlasrag.graphs.answer.subgraph", fromlist=["AnswerServices"]
            ).AnswerServices(
                compression=IdentityCompression(),
                builder=GenerationInputBuilder(profile=AnswerProfile()),
                generation=generation,
                review=OutputReviewService(),
                resolver=CitationResolver(),
                profile=AnswerProfile(),
            )
        )
        core = RagCoreRunner(
            RagCoreServices(
                query_construction=QueryConstructionService(
                    provider=query_provider,
                    clock=lambda: datetime(2026, 11, 14, tzinfo=UTC),
                ),
                local_evidence=LocalEvidenceRunner(local),
                routing=CorrectiveRoutingService(max_retries=1),
                web_evidence=cast(Any, FakeWeb()),
                answer_input=AnswerInputBuilder(),
                answer=answer,
                completion=CompletionAssessor(),
            )
        )
        redis_url = os.environ["ATLASRAG_TEST_REDIS_URL"]
        redis_client = redis.from_url(redis_url, decode_responses=False)
        async for stale_key in redis_client.scan_iter(match="atlasrag:query:*"):
            await redis_client.delete(stale_key)
        fingerprint = runtime_fingerprint(runtime)
        cache = SessionQueryCache(
            redis=RedisCacheProvider(redis_client),
            uow_factory=postgres_uow_factory,
            runtime_fingerprint=fingerprint,
            ttl_seconds=60,
            policy=QueryCachePolicy(enabled=True),
            clock=lambda: datetime(2026, 11, 14, tzinfo=UTC),
        )
        graph = RetrievalRunner(RetrievalGraphServices(cache=cache, rag_core=core))
        request = UserTurnRequest(
            session_id="stage8-live",
            query="What is the maintenance interval specified in the sample manual?",
        )
        first = await graph.run(request)
        assert first.answer_text.strip()
        assert first.citations
        assert first.citations[0].citation_id == "L1"
        expected_key = query_cache_key(
            request.session_id, fingerprint, request_hash(request)
        )
        assert await redis_client.exists(expected_key) == 1
        query_calls = query_provider.calls
        generation_calls = generation.calls
        assert query_calls == 1 and generation_calls == 1
        second = await graph.run(request)
        assert second == first
        assert query_provider.calls == query_calls
        assert generation.calls == generation_calls

        alternate_runtime = runtime.model_copy(
            update={
                "models": runtime.models.model_copy(
                    update={"generation_model_revision": "alternate-revision"}
                )
            }
        )
        alternate_fingerprint = runtime_fingerprint(alternate_runtime)
        assert alternate_fingerprint != fingerprint
        alternate_cache = SessionQueryCache(
            redis=RedisCacheProvider(redis_client),
            uow_factory=postgres_uow_factory,
            runtime_fingerprint=alternate_fingerprint,
            ttl_seconds=60,
            policy=QueryCachePolicy(enabled=True),
            clock=lambda: datetime(2026, 11, 14, tzinfo=UTC),
        )
        assert await alternate_cache.lookup(request) is None
        await redis_client.aclose()
