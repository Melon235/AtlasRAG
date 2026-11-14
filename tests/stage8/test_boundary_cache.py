"""Evidence boundary, citation assignment, and Query Cache policy tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from stage7.helpers import local_evidence, web_evidence

from atlasrag.domain.enums import (
    AnswerExecutionOutcome,
    EvidenceType,
    LocalAnswerEvidenceStatus,
    LocalEvidenceStatus,
    LocalExecutionStatus,
    RagCompletionStatus,
    WebExecutionStatus,
)
from atlasrag.domain.evidence import LocalEvidenceResult
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalLocalCitation, FinalResponse, RagCoreResult
from atlasrag.domain.web import WebEvidenceResult
from atlasrag.providers.cache.models import CachedQueryResult
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.cache.session import SessionQueryCache
from atlasrag.services.rag_core.boundary import AnswerInputBuilder
from atlasrag.services.rag_core.completion import CompletionAssessor


def local_result(
    status: LocalEvidenceStatus,
    *,
    execution: LocalExecutionStatus = LocalExecutionStatus.OK,
    best: bool = False,
) -> LocalEvidenceResult:
    evidence = local_evidence()
    if execution is LocalExecutionStatus.UNAVAILABLE:
        return LocalEvidenceResult(execution_status=LocalExecutionStatus.UNAVAILABLE)
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
        best_available_evidence=evidence if best else None,
    )


def web_result(
    status: WebExecutionStatus = WebExecutionStatus.OK,
    items: tuple[Any, ...] = (web_evidence(),),
) -> WebEvidenceResult:
    return WebEvidenceResult(execution_status=status, evidence=items)


def test_boundary_matrix_and_citation_ids() -> None:
    builder = AnswerInputBuilder()
    local = local_result(LocalEvidenceStatus.SUFFICIENT)
    only = builder.build(original_query="q", local_result=local, web_result=None)
    assert only.local_evidence_status is LocalAnswerEvidenceStatus.SUFFICIENT
    assert [item.citation_id for item in only.local_evidence] == ["L1"]
    mixed = builder.build(
        original_query="q",
        local_result=local_result(LocalEvidenceStatus.INSUFFICIENT, best=True),
        web_result=web_result(),
    )
    assert (
        mixed.local_evidence_status is LocalAnswerEvidenceStatus.INSUFFICIENT_BEST_MATCH
    )
    assert [item.citation_id for item in mixed.local_evidence] == ["L1"]
    assert [item.citation_id for item in mixed.web_evidence] == ["W1"]
    ambiguous = builder.build(
        original_query="q",
        local_result=local_result(LocalEvidenceStatus.AMBIGUOUS),
        web_result=web_result(),
    )
    assert ambiguous.local_evidence == ()
    assert ambiguous.local_evidence_status is LocalAnswerEvidenceStatus.NONE
    unavailable = builder.build(
        original_query="q",
        local_result=local_result(
            LocalEvidenceStatus.INSUFFICIENT, execution=LocalExecutionStatus.UNAVAILABLE
        ),
        web_result=web_result(WebExecutionStatus.EMPTY, ()),
    )
    assert unavailable.local_evidence == () and unavailable.web_evidence == ()


def test_completion_matrix() -> None:
    assessor = CompletionAssessor()
    ok_input = AnswerInputBuilder().build(
        original_query="q",
        local_result=local_result(LocalEvidenceStatus.SUFFICIENT),
        web_result=None,
    )
    assert (
        assessor.assess(
            answer_outcome=AnswerExecutionOutcome.SUCCESS,
            answer_input=ok_input,
            local_result=None,
            web_result=None,
        )
        is RagCompletionStatus.SUCCESS
    )
    assert (
        assessor.assess(
            answer_outcome=AnswerExecutionOutcome.SAFE_FAILURE,
            answer_input=ok_input,
            local_result=None,
            web_result=None,
        )
        is RagCompletionStatus.SAFE_FAILURE
    )
    degraded_local = local_result(
        LocalEvidenceStatus.SUFFICIENT, execution=LocalExecutionStatus.DEGRADED
    )
    degraded_input = AnswerInputBuilder().build(
        original_query="q", local_result=degraded_local, web_result=None
    )
    assert (
        assessor.assess(
            answer_outcome=AnswerExecutionOutcome.SUCCESS,
            answer_input=degraded_input,
            local_result=degraded_local,
            web_result=None,
        )
        is RagCompletionStatus.DEGRADED_SUCCESS
    )


class FakeRedis:
    def __init__(self) -> None:
        self.value: CachedQueryResult | None = None
        self.gets = 0
        self.puts = 0
        self.error: BaseException | None = None

    async def get_query(
        self, session_id: str, runtime_fingerprint: str, request_hash: str
    ) -> CachedQueryResult | None:
        self.gets += 1
        if self.error:
            raise self.error
        return self.value

    async def put_query(
        self,
        session_id: str,
        runtime_fingerprint: str,
        request_hash: str,
        value: CachedQueryResult,
        *,
        ttl_seconds: int,
    ) -> None:
        if self.error:
            raise self.error
        self.puts += 1
        self.value = value


class FakeMetadata:
    def __init__(self, marker: bool) -> None:
        self.query_cache_invalidation_required = marker


class FakeMetadataRepo:
    def __init__(self, marker: bool) -> None:
        self.marker = marker

    async def get(self) -> FakeMetadata:
        return FakeMetadata(self.marker)


class FakeUnit:
    def __init__(self, marker: bool) -> None:
        self.runtime_metadata = FakeMetadataRepo(marker)

    async def __aenter__(self) -> FakeUnit:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeFactory:
    def __init__(self, marker: bool = False) -> None:
        self.marker = marker
        self.calls = 0

    def __call__(self) -> FakeUnit:
        self.calls += 1
        return FakeUnit(self.marker)


def final_response() -> FinalResponse:
    return FinalResponse(
        answer_text="Answer [L1].",
        citations=(
            FinalLocalCitation(
                citation_id="L1",
                file_name="guide.md",
                relative_source_path="docs/guide.md",
                evidence_type=EvidenceType.TEXT_PARENT,
            ),
        ),
    )


def core_result() -> RagCoreResult:
    return RagCoreResult(
        final_response=final_response(), completion_status=RagCompletionStatus.SUCCESS
    )


def request() -> UserTurnRequest:
    return UserTurnRequest(session_id="s1", query="question")


def cache(
    redis: FakeRedis, factory: FakeFactory, policy: QueryCachePolicy | None = None
) -> SessionQueryCache:
    return SessionQueryCache(
        redis=cast(Any, redis),
        uow_factory=cast(Any, factory),
        runtime_fingerprint="runtime",
        ttl_seconds=60,
        policy=policy or QueryCachePolicy(enabled=True),
        clock=lambda: datetime(2026, 11, 14, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_cache_hit_and_store() -> None:
    redis, factory = FakeRedis(), FakeFactory()
    provider = cache(redis, factory)
    assert await provider.lookup(request()) is None
    assert await provider.store(request(), core_result()) is True
    hit = await provider.lookup(request())
    assert hit is not None and hit.answer_text == "Answer [L1]."
    assert redis.puts == 1


@pytest.mark.asyncio
async def test_freshness_marker_bypasses_lookup_and_write() -> None:
    redis, factory = FakeRedis(), FakeFactory(marker=True)
    provider = cache(redis, factory)
    redis.value = CachedQueryResult(
        created_at=datetime(2026, 11, 14, tzinfo=UTC),
        final_response=final_response(),
    )
    assert await provider.lookup(request()) is None
    assert redis.gets == 0
    assert await provider.store(request(), core_result()) is False
    assert redis.puts == 0


@pytest.mark.asyncio
async def test_marker_becomes_true_before_write_and_redis_failure_is_soft() -> None:
    redis, factory = FakeRedis(), FakeFactory()
    provider = cache(redis, factory)
    assert await provider.store(request(), core_result()) is True
    factory.marker = True
    assert await provider.store(request(), core_result()) is False
    assert redis.puts == 1
    from atlasrag.domain.errors import DependencyUnavailableError

    redis.error = DependencyUnavailableError()
    assert await provider.lookup(request()) is None
    assert await provider.store(request(), core_result()) is False
    from atlasrag.domain.errors import ProviderResponseError

    redis.error = ProviderResponseError()
    assert await provider.store(request(), core_result()) is False


def test_policy_is_conservative() -> None:
    enabled = QueryCachePolicy(enabled=True)
    assert enabled.decide(request(), core_result()).eligible
    assert not enabled.decide(
        request(),
        RagCoreResult(
            final_response=FinalResponse(answer_text="Safe.", citations=()),
            completion_status=RagCompletionStatus.SAFE_FAILURE,
        ),
    ).eligible
    assert not enabled.decide(
        request(),
        RagCoreResult(
            final_response=final_response(),
            completion_status=RagCompletionStatus.DEGRADED_SUCCESS,
        ),
    ).eligible
    assert not enabled.decide(
        UserTurnRequest(session_id="s1", query="latest status today"),
        core_result(),
    ).eligible
    assert not enabled.decide(request(), core_result(), web_invoked=True).eligible
