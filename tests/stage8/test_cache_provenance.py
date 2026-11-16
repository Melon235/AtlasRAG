"""Actual RagCore/retrieval graph provenance regressions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from stage7.helpers import local_evidence, web_evidence

from atlasrag.application.rag_core import RagCoreRunner
from atlasrag.application.retrieval import RetrievalRunner
from atlasrag.domain.answer import AnswerInput
from atlasrag.domain.enums import (
    AnswerExecutionOutcome,
    EvidenceType,
    LocalEvidenceStatus,
    LocalExecutionStatus,
    RagCompletionStatus,
    WebExecutionStatus,
)
from atlasrag.domain.evidence import LocalEvidenceResult
from atlasrag.domain.requests import LocalRetrievalRequest, UserTurnRequest
from atlasrag.domain.results import FinalLocalCitation, FinalResponse
from atlasrag.domain.web import WebEvidenceResult
from atlasrag.graphs.rag_core.subgraph import RagCoreServices
from atlasrag.graphs.retrieval.graph import (
    RetrievalGraphServices,
    build_retrieval_graph,
)
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


class FakeLocal:
    def __init__(self, result: LocalEvidenceResult) -> None:
        self.result = result

    async def run(self, request: LocalRetrievalRequest) -> LocalEvidenceResult:
        return self.result


class FakeWeb:
    def __init__(self, result: WebEvidenceResult) -> None:
        self.result = result
        self.calls = 0

    async def run(self, request: object) -> WebEvidenceResult:
        self.calls += 1
        return self.result


class FakeAnswer:
    async def run(
        self, input: AnswerInput
    ) -> tuple[FinalResponse, AnswerExecutionOutcome]:
        assert input.local_evidence
        evidence = input.local_evidence[0].evidence
        return (
            FinalResponse(
                answer_text="Answer [L1].",
                citations=(
                    FinalLocalCitation(
                        citation_id="L1",
                        file_name=evidence.provenance.file_name,
                        relative_source_path=evidence.provenance.relative_source_path,
                        evidence_type=EvidenceType(evidence.evidence_type),
                    ),
                ),
            ),
            AnswerExecutionOutcome.SUCCESS,
        )


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str, str], CachedQueryResult] = {}
        self.gets = 0
        self.puts = 0

    async def get_query(
        self, session_id: str, runtime_fingerprint: str, request_hash: str
    ) -> CachedQueryResult | None:
        self.gets += 1
        return self.values.get((session_id, runtime_fingerprint, request_hash))

    async def put_query(
        self,
        session_id: str,
        runtime_fingerprint: str,
        request_hash: str,
        value: CachedQueryResult,
        *,
        ttl_seconds: int,
    ) -> None:
        self.puts += 1
        self.values[(session_id, runtime_fingerprint, request_hash)] = value


class FakeMeta:
    query_cache_invalidation_required = False


class FakeMetaRepo:
    async def get(self) -> FakeMeta:
        return FakeMeta()


class FakeUnit:
    runtime_metadata = FakeMetaRepo()

    async def __aenter__(self) -> FakeUnit:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def unit_factory() -> FakeUnit:
    return FakeUnit()


def local_result(
    status: LocalEvidenceStatus,
    *,
    execution: LocalExecutionStatus = LocalExecutionStatus.OK,
) -> LocalEvidenceResult:
    evidence = local_evidence()
    if status is LocalEvidenceStatus.SUFFICIENT:
        return LocalEvidenceResult(
            execution_status=execution,
            evidence_status=status,
            selected_evidence=(evidence,),
        )
    return LocalEvidenceResult(
        execution_status=execution,
        evidence_status=status,
        selected_evidence=(),
        best_available_evidence=evidence
        if status is LocalEvidenceStatus.INSUFFICIENT
        else None,
    )


def make_services(
    local: LocalEvidenceResult,
    web: WebEvidenceResult,
) -> tuple[RagCoreRunner, FakeWeb, FakeRedis, SessionQueryCache]:
    web_runner = FakeWeb(web)
    core = RagCoreRunner(
        RagCoreServices(
            query_construction=cast(Any, FakeQuery()),
            local_evidence=cast(Any, FakeLocal(local)),
            routing=CorrectiveRoutingService(max_retries=1),
            web_evidence=cast(Any, web_runner),
            answer_input=AnswerInputBuilder(),
            answer=cast(Any, FakeAnswer()),
            completion=CompletionAssessor(),
        )
    )
    redis = FakeRedis()
    cache = SessionQueryCache(
        redis=cast(Any, redis),
        uow_factory=cast(Any, unit_factory),
        runtime_fingerprint="runtime-a",
        ttl_seconds=60,
        policy=QueryCachePolicy(enabled=True),
        clock=lambda: datetime(2026, 11, 14, tzinfo=UTC),
    )
    return core, web_runner, redis, cache


def request() -> UserTurnRequest:
    return UserTurnRequest(
        session_id="s1", query="What is the documented maintenance interval?"
    )


@pytest.mark.asyncio
async def test_web_invoked_with_local_only_citation_is_not_cached() -> None:
    core, web, redis, cache = make_services(
        local_result(LocalEvidenceStatus.INSUFFICIENT),
        WebEvidenceResult(
            execution_status=WebExecutionStatus.OK, evidence=(web_evidence(),)
        ),
    )
    execution = await core.run_with_facts(request())
    assert execution.web_invoked is True
    assert execution.result.completion_status is RagCompletionStatus.SUCCESS
    assert [item.citation_id for item in execution.result.final_response.citations] == [
        "L1"
    ]
    assert web.calls == 1

    response = await RetrievalRunner(
        RetrievalGraphServices(cache=cache, rag_core=core)
    ).run(request())
    assert response.answer_text
    assert redis.puts == 0


@pytest.mark.asyncio
async def test_local_success_without_web_is_cached_and_second_run_hits() -> None:
    core, web, redis, cache = make_services(
        local_result(LocalEvidenceStatus.SUFFICIENT),
        WebEvidenceResult(execution_status=WebExecutionStatus.EMPTY, evidence=()),
    )
    execution = await core.run_with_facts(request())
    assert execution.web_invoked is False
    assert web.calls == 0
    runner = RetrievalRunner(RetrievalGraphServices(cache=cache, rag_core=core))
    first = await runner.run(request())
    assert first.citations
    assert redis.puts == 1
    second = await runner.run(request())
    assert second == first
    assert redis.puts == 1
    assert redis.gets >= 2


@pytest.mark.asyncio
async def test_web_empty_or_unavailable_still_records_invocation() -> None:
    for status in (WebExecutionStatus.EMPTY, WebExecutionStatus.UNAVAILABLE):
        core, web, redis, cache = make_services(
            local_result(LocalEvidenceStatus.INSUFFICIENT),
            WebEvidenceResult(execution_status=status, evidence=()),
        )
        execution = await core.run_with_facts(request())
        assert execution.web_invoked is True
        await RetrievalRunner(RetrievalGraphServices(cache=cache, rag_core=core)).run(
            request()
        )
        assert redis.puts == 0
        assert web.calls == 2


@pytest.mark.asyncio
async def test_direct_graph_without_provenance_skips_cache_write() -> None:
    core, _, redis, cache = make_services(
        local_result(LocalEvidenceStatus.SUFFICIENT),
        WebEvidenceResult(execution_status=WebExecutionStatus.EMPTY, evidence=()),
    )
    graph = build_retrieval_graph(RetrievalGraphServices(cache=cache, rag_core=core))
    await graph.ainvoke({"request": request()})
    assert redis.puts == 0


@pytest.mark.asyncio
async def test_concurrent_retrievals_do_not_share_provenance() -> None:
    def build(
        local_status: LocalEvidenceStatus, web_status: WebExecutionStatus
    ) -> tuple[RagCoreRunner, FakeWeb, FakeRedis, SessionQueryCache]:
        evidence = (
            ()
            if web_status in {WebExecutionStatus.EMPTY, WebExecutionStatus.UNAVAILABLE}
            else (web_evidence(),)
        )
        core, web, redis, cache = make_services(
            local_result(local_status),
            WebEvidenceResult(execution_status=web_status, evidence=evidence),
        )
        return core, web, redis, cache

    local_core, local_web, local_redis, local_cache = build(
        LocalEvidenceStatus.SUFFICIENT, WebExecutionStatus.EMPTY
    )
    web_core, web_runner, web_redis, web_cache = build(
        LocalEvidenceStatus.INSUFFICIENT, WebExecutionStatus.OK
    )
    local_runner = RetrievalRunner(
        RetrievalGraphServices(cache=local_cache, rag_core=local_core)
    )
    web_runner_graph = RetrievalRunner(
        RetrievalGraphServices(cache=web_cache, rag_core=web_core)
    )
    await asyncio.gather(local_runner.run(request()), web_runner_graph.run(request()))
    assert local_web.calls == 0 and web_runner.calls == 1
    assert local_redis.puts == 1 and web_redis.puts == 0


@pytest.mark.asyncio
async def test_disabled_cache_skips_reads_even_with_existing_entry() -> None:
    core, _, redis, _ = make_services(
        local_result(LocalEvidenceStatus.SUFFICIENT),
        WebEvidenceResult(execution_status=WebExecutionStatus.EMPTY, evidence=()),
    )
    redis.values[("s1", "runtime-a", "stale")] = CachedQueryResult(
        created_at=datetime(2026, 11, 14, tzinfo=UTC),
        final_response=FinalResponse(answer_text="stale [L1].", citations=()),
    )
    disabled = SessionQueryCache(
        redis=cast(Any, redis),
        uow_factory=cast(Any, unit_factory),
        runtime_fingerprint="runtime-a",
        ttl_seconds=60,
        policy=QueryCachePolicy(enabled=False),
        clock=lambda: datetime(2026, 11, 14, tzinfo=UTC),
    )
    response = await RetrievalRunner(
        RetrievalGraphServices(cache=disabled, rag_core=core)
    ).run(request())
    assert response.answer_text
    assert redis.gets == 0
    assert redis.puts == 0
