from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from atlasrag.application.rag_core import RagCoreRunner
from atlasrag.application.retrieval import RetrievalRunner
from atlasrag.domain.enums import (
    LocalEvidenceStatus,
    LocalExecutionStatus,
    WebExecutionStatus,
)
from atlasrag.domain.evidence import LocalEvidenceResult
from atlasrag.domain.requests import LocalRetrievalRequest, UserTurnRequest
from atlasrag.domain.web import WebEvidenceResult
from atlasrag.graphs.rag_core.subgraph import RagCoreServices
from atlasrag.graphs.retrieval.graph import RetrievalGraphServices
from atlasrag.providers.cache.models import CachedQueryResult
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.cache.session import SessionQueryCache
from atlasrag.services.rag_core.boundary import AnswerInputBuilder
from atlasrag.services.rag_core.completion import CompletionAssessor
from atlasrag.services.rag_core.query import QueryConstructionResult
from atlasrag.services.rag_core.routing import CorrectiveRoutingService


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


class InsufficientLocal:
    async def run(self, request: LocalRetrievalRequest) -> LocalEvidenceResult:
        return LocalEvidenceResult(
            execution_status=LocalExecutionStatus.OK,
            evidence_status=LocalEvidenceStatus.INSUFFICIENT,
            selected_evidence=(),
        )


class NoNetworkWeb:
    """Disabled Web boundary: records the route but never performs HTTP."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request: object) -> WebEvidenceResult:
        self.calls += 1
        return WebEvidenceResult(execution_status=WebExecutionStatus.EMPTY, evidence=())


class NoCache:
    def __init__(self) -> None:
        self.gets = 0
        self.puts = 0

    async def lookup(self, request: UserTurnRequest) -> object:
        self.gets += 1
        return None

    async def store(
        self, request: UserTurnRequest, result: object, **kwargs: object
    ) -> bool:
        self.puts += 1
        return False


class NoAnswer:
    async def run(self, input: object) -> object:
        raise AssertionError("no evidence should reach generation")


class EmptyRedis:
    async def get_query(
        self, *args: object, **kwargs: object
    ) -> CachedQueryResult | None:
        raise AssertionError("disabled cache must not GET")

    async def put_query(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("disabled cache must not SET")


class Meta:
    query_cache_invalidation_required = False


class MetaRepo:
    async def get(self) -> Meta:
        return Meta()


class FakeUnit:
    runtime_metadata = MetaRepo()

    async def __aenter__(self) -> FakeUnit:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_insufficient_local_forces_web_route_without_network_or_answer() -> None:
    web = NoNetworkWeb()
    cache = SessionQueryCache(
        redis=cast(Any, EmptyRedis()),
        uow_factory=cast(Any, lambda: FakeUnit()),
        runtime_fingerprint="test",
        ttl_seconds=60,
        policy=QueryCachePolicy(enabled=False),
        clock=lambda: datetime(2026, 11, 18, tzinfo=UTC),
    )
    core = RagCoreRunner(
        RagCoreServices(
            query_construction=cast(Any, FakeQuery()),
            local_evidence=cast(Any, InsufficientLocal()),
            routing=CorrectiveRoutingService(max_retries=0),
            web_evidence=cast(Any, web),
            answer_input=AnswerInputBuilder(),
            answer=cast(Any, NoAnswer()),
            completion=CompletionAssessor(),
        )
    )
    response = await RetrievalRunner(
        RetrievalGraphServices(cache=cast(Any, cache), rag_core=core)
    ).run(UserTurnRequest(session_id="s", query="unanswerable"))
    assert response.citations == ()
    assert web.calls == 1
