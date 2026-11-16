"""Real PostgreSQL/Milvus/Redis Stage 8 RAG flow integration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest
import redis.asyncio as redis
from stage7.helpers import services as answer_services

from atlasrag.application.local_evidence import LocalEvidenceRunner
from atlasrag.application.rag_core import RagCoreRunner
from atlasrag.application.retrieval import RetrievalRunner
from atlasrag.domain.answer import GenerationResult
from atlasrag.domain.enums import (
    WebExecutionStatus,
)
from atlasrag.domain.evidence import LocalEvidenceResult
from atlasrag.domain.requests import (
    LocalRetrievalRequest,
    UserTurnRequest,
)
from atlasrag.domain.web import WebEvidenceResult
from atlasrag.graphs.rag_core.subgraph import RagCoreServices
from atlasrag.graphs.retrieval.graph import RetrievalGraphServices
from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.cache.session import SessionQueryCache
from atlasrag.services.rag_core.boundary import AnswerInputBuilder
from atlasrag.services.rag_core.completion import CompletionAssessor
from atlasrag.services.rag_core.query import QueryConstructionResult
from atlasrag.services.rag_core.routing import CorrectiveRoutingService
from integration.stage4_helpers import Factory
from integration.stage5_helpers import application as sync_application
from integration.stage5_helpers import request as sync_request
from integration.test_milvus import IsolatedMilvus
from integration.test_milvus import isolated_milvus as isolated_milvus

pytestmark = pytest.mark.integration

MARKDOWN = "# Guide\n\nAtlasRAG hybrid retrieval combines dense and BM25 evidence.\n"


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


class FakeWeb:
    async def run(self, request: object) -> WebEvidenceResult:
        return WebEvidenceResult(execution_status=WebExecutionStatus.EMPTY, evidence=())


class FakeGeneration:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, input: object) -> GenerationResult:
        self.calls += 1
        return GenerationResult(answer_text="Answer [L1].", used_citation_ids=("L1",))


class CountingLocal:
    def __init__(self, runner: LocalEvidenceRunner) -> None:
        self._runner = runner
        self.calls = 0

    async def run(self, request: LocalRetrievalRequest) -> LocalEvidenceResult:
        self.calls += 1
        return await self._runner.run(request)


async def _synced(factory: Factory, pool: Any, index: Any, root: Path) -> None:
    app = sync_application(factory, pool, index, root)
    result = await app.run(sync_request(root))
    assert result.ready_builds == 1, result.model_dump()


@pytest.mark.asyncio
async def test_real_stage8_local_answer_and_query_cache(
    postgres_uow_factory: Factory,
    postgres_pool: Any,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    (tmp_path / "guide.md").write_text(MARKDOWN, encoding="utf-8")
    await _synced(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    async with postgres_uow_factory() as unit:
        await unit.runtime_metadata.set_query_cache_invalidation_required(False)
        await unit.commit()
    local_bundle, _, _ = __import__(
        "integration.test_stage6_local_evidence", fromlist=["services"]
    ).services(postgres_uow_factory, isolated_milvus.provider)
    local = CountingLocal(LocalEvidenceRunner(local_bundle))
    generation = FakeGeneration()
    answer = answer_services(generation)
    from atlasrag.application.answer import AnswerRunner

    bundle = RagCoreServices(
        query_construction=cast(Any, FakeQuery()),
        local_evidence=cast(Any, local),
        routing=CorrectiveRoutingService(max_retries=1),
        web_evidence=cast(Any, FakeWeb()),
        answer_input=AnswerInputBuilder(),
        answer=AnswerRunner(answer),
        completion=CompletionAssessor(),
    )
    redis_url = os.environ["ATLASRAG_TEST_REDIS_URL"]
    client = redis.from_url(redis_url, decode_responses=False)
    async for stale_key in client.scan_iter(match="atlasrag:query:*"):
        await client.delete(stale_key)
    cache = SessionQueryCache(
        redis=RedisCacheProvider(client),
        uow_factory=postgres_uow_factory,
        runtime_fingerprint="stage8-integration-runtime",
        ttl_seconds=60,
        policy=QueryCachePolicy(enabled=True),
        clock=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    retrieval = RetrievalRunner(
        RetrievalGraphServices(cache=cache, rag_core=RagCoreRunner(bundle))
    )
    request = UserTurnRequest(
        session_id="session-a", query="AtlasRAG hybrid retrieval evidence"
    )
    first = await retrieval.run(request)
    assert first.answer_text
    assert [citation.citation_id for citation in first.citations] == ["L1"]
    local_after_first = local.calls
    generation_after_first = generation.calls

    second = await retrieval.run(request)
    assert second == first
    assert local.calls == local_after_first
    assert generation.calls == generation_after_first

    async with postgres_uow_factory() as unit:
        await unit.runtime_metadata.set_query_cache_invalidation_required(True)
        await unit.commit()
    await retrieval.run(request)
    assert local.calls == local_after_first + 1
    await client.aclose()
