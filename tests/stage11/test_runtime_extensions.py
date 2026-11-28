"""Unit tests for WP-A runtime extensions (cache metadata, admission, maintenance, shutdown)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from atlasrag.application.hashing import request_hash
from atlasrag.application.rag_core import RagCoreExecution
from atlasrag.application.retrieval import RetrievalRunner
from atlasrag.domain.enums import EvidenceType, RagCompletionStatus
from atlasrag.domain.errors import (
    DependencyUnavailableError,
    RuntimeNotReadyError,
    RuntimeShutdownFailedError,
)
from atlasrag.domain.observation import (
    ObservationStage,
    ProductionObservation,
    current_invocation_id,
    deliver_observation,
    observation_invocation,
)
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalLocalCitation, FinalResponse, RagCoreResult
from atlasrag.graphs.retrieval.graph import RetrievalGraphServices
from atlasrag.providers.cache.keys import query_cache_key
from atlasrag.providers.cache.models import CachedQueryResult
from atlasrag.runtime.coordinator import ApplicationCoordinator
from atlasrag.runtime.observation import InvocationObserverMux
from atlasrag.runtime.tasks import TaskKind
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.cache.session import QueryCacheStatus, SessionQueryCache
from stage11.conftest import (
    FakeRuntimeMetadataRepo,
    FakeSyncUow,
    build_test_dependencies,
    sample_request,
    sample_response,
    start_test_coordinator,
)


class _RecordingObserver:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def observe(self, event: ProductionObservation) -> None:
        self._sink.append(f"{event.invocation_id}:{event.status}")


class _FailingObserver:
    def observe(self, event: ProductionObservation) -> None:
        raise ValueError("bad sink")


class _InMemoryRedisQueryCache:
    def __init__(self, *, fail_reads: bool = False) -> None:
        self.fail_reads = fail_reads
        self.active_fence: str | None = "fence-1"
        self.records: dict[str, tuple[str | None, CachedQueryResult]] = {}
        self.keys_written: list[str] = []

    async def get_query_write_fence(self) -> str | None:
        return self.active_fence

    async def get_fenced_query(
        self, session_id: str, runtime_fingerprint: str, request_hash: str
    ) -> tuple[str | None, CachedQueryResult | None]:
        if self.fail_reads:
            raise DependencyUnavailableError
        key = query_cache_key(session_id, runtime_fingerprint, request_hash)
        record = self.records.get(key)
        if record is None:
            return self.active_fence, None
        return record

    async def put_fenced_query(
        self,
        session_id: str,
        runtime_fingerprint: str,
        request_hash: str,
        value: CachedQueryResult,
        *,
        ttl_seconds: int,
        write_fence: str | None = None,
    ) -> bool:
        key = query_cache_key(session_id, runtime_fingerprint, request_hash)
        self.keys_written.append(key)
        self.records[key] = (write_fence, value)
        return True


class _StubRagCore:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request: UserTurnRequest) -> RagCoreResult:
        self.calls += 1
        return RagCoreResult(
            final_response=FinalResponse(
                answer_text=f"fresh-{self.calls} [L1].",
                citations=(
                    FinalLocalCitation(
                        citation_id="L1",
                        file_name="doc.pdf",
                        relative_source_path="doc.pdf",
                        evidence_type=EvidenceType.TEXT_PARENT,
                    ),
                ),
            ),
            completion_status=RagCompletionStatus.SUCCESS,
        )

    async def run_with_facts(self, request: UserTurnRequest) -> RagCoreExecution:
        return RagCoreExecution(result=await self.run(request), web_invoked=False)


@pytest.mark.asyncio
async def test_session_query_cache_lookup_with_status_and_retrieval_facts() -> None:
    redis = _InMemoryRedisQueryCache()
    repo = FakeRuntimeMetadataRepo(invalidation_required=False)
    cache = SessionQueryCache(
        redis=cast(Any, redis),
        uow_factory=cast(Any, lambda: FakeSyncUow(repo)),
        runtime_fingerprint="fp-1",
        ttl_seconds=60,
        policy=QueryCachePolicy(enabled=True),
        clock=lambda: datetime.now(UTC),
        require_write_fence=True,
        expected_write_fence=lambda: "fence-1",
    )
    req = sample_request("sess-1", "query one")

    resp, status = await cache.lookup_with_status(req)
    assert resp is None and status is QueryCacheStatus.MISS

    rag = _StubRagCore()
    runner = RetrievalRunner(
        RetrievalGraphServices(cache=cast(Any, cache), rag_core=cast(Any, rag))
    )
    first = await runner.run_with_facts(req)
    assert first.cache_status is QueryCacheStatus.MISS
    assert rag.calls == 1
    assert len(redis.keys_written) == 1
    assert redis.keys_written[0] == query_cache_key("sess-1", "fp-1", request_hash(req))

    second = await runner.run_with_facts(req)
    assert second.cache_status is QueryCacheStatus.HIT
    assert second.response == first.response
    assert rag.calls == 1

    # Untrusted freshness marker -> BYPASSED
    repo.invalidation_required = True
    _, untrusted_status = await cache.lookup_with_status(req)
    assert untrusted_status is QueryCacheStatus.BYPASSED
    repo.invalidation_required = False

    # Redis outage -> BYPASSED
    redis.fail_reads = True
    _, redis_down_status = await cache.lookup_with_status(req)
    assert redis_down_status is QueryCacheStatus.BYPASSED

    # Cache disabled policy -> BYPASSED
    disabled_cache = SessionQueryCache(
        redis=cast(Any, _InMemoryRedisQueryCache()),
        uow_factory=cast(Any, lambda: FakeSyncUow(repo)),
        runtime_fingerprint="fp-1",
        ttl_seconds=60,
        policy=QueryCachePolicy(enabled=False),
        clock=lambda: datetime.now(UTC),
    )
    _, disabled_status = await disabled_cache.lookup_with_status(req)
    assert disabled_status is QueryCacheStatus.BYPASSED


@pytest.mark.asyncio
async def test_coordinator_query_cache_status_and_fresh_trace_id() -> None:
    coordinator, _ = await start_test_coordinator(
        retrieval_cache_status=QueryCacheStatus.HIT
    )
    try:
        r1 = await coordinator.run_query(sample_request())
        r2 = await coordinator.run_query(sample_request())
        assert r1.cache_status is QueryCacheStatus.HIT
        assert r2.cache_status is QueryCacheStatus.HIT
        assert r1.trace_id != r2.trace_id
        assert r1.elapsed_ms is not None and r1.elapsed_ms >= 0.0
    finally:
        await coordinator.shutdown()

    legacy_coord, _ = await start_test_coordinator()
    try:
        legacy_env = await legacy_coord.run_query(sample_request())
        assert legacy_env.cache_status is QueryCacheStatus.UNKNOWN
    finally:
        await legacy_coord.shutdown()


@pytest.mark.asyncio
async def test_on_admitted_timing_and_invocation_observer_mux_isolation() -> None:
    events_seen: list[str] = []
    mux = InvocationObserverMux()

    async def slow_retrieval(request: UserTurnRequest) -> FinalResponse:
        inv = current_invocation_id()
        assert inv is not None
        events_seen.append(f"before_obs:{inv}")
        deliver_observation(
            mux,
            ProductionObservation(
                stage=ObservationStage.EVIDENCE,
                status=request.query,
                invocation_id=inv,
            ),
        )
        await asyncio.sleep(0.02)
        deliver_observation(
            mux,
            ProductionObservation(
                stage=ObservationStage.ANSWER,
                status="done",
                invocation_id=inv,
            ),
        )
        return sample_response(request.query)

    coordinator, harness = await start_test_coordinator(retrieval=slow_retrieval)
    try:
        inv1_events: list[str] = []
        inv2_events: list[str] = []

        def make_admit(bucket: list[str]) -> Any:
            def _hook(invocation_id: str) -> None:
                assert len(harness.tasks.snapshot(TaskKind.QUERY)) >= 1
                events_seen.append(f"admitted:{invocation_id}")
                mux.bind(invocation_id, _RecordingObserver(bucket))

            return _hook

        res1, res2 = await asyncio.gather(
            coordinator.run_query(
                sample_request("s1", "q1"), on_admitted=make_admit(inv1_events)
            ),
            coordinator.run_query(
                sample_request("s2", "q2"), on_admitted=make_admit(inv2_events)
            ),
        )
        assert res1.trace_id != res2.trace_id
        assert events_seen.index(f"admitted:{res1.trace_id}") < events_seen.index(
            f"before_obs:{res1.trace_id}"
        )
        assert inv1_events == [f"{res1.trace_id}:q1", f"{res1.trace_id}:done"]
        assert inv2_events == [f"{res2.trace_id}:q2", f"{res2.trace_id}:done"]
        mux.unbind(res1.trace_id)
        mux.unbind(res2.trace_id)
        assert mux.bound_count() == 0

        # Failing on_admitted releases query lease and unregisters QUERY task cleanly
        def exploding_hook(_inv: str) -> None:
            raise RuntimeError("hook failed")

        with pytest.raises(RuntimeError, match="hook failed"):
            await coordinator.run_query(sample_request(), on_admitted=exploding_hook)
        assert len(harness.tasks.snapshot(TaskKind.QUERY)) == 0

        # Rejected admission never invokes on_admitted
        called = False

        def never_called(_inv: str) -> None:
            nonlocal called
            called = True

        await harness.gate.set_admission_ready(
            False, lifecycle_generation=harness.gate.lifecycle_generation
        )
        with pytest.raises(RuntimeNotReadyError):
            await coordinator.run_query(sample_request(), on_admitted=never_called)
        assert called is False
    finally:
        await coordinator.shutdown()

    # Failing observer sink warns without crashing execution when called via deliver_observation
    mux.bind("inv-fail", _FailingObserver())
    with observation_invocation("inv-fail"):
        with pytest.warns(RuntimeWarning):
            deliver_observation(
                mux, ProductionObservation(stage=ObservationStage.EVIDENCE, status="x")
            )
    mux.unbind("inv-fail")


@pytest.mark.asyncio
async def test_maintenance_on_draining_handoff_and_failure_abort() -> None:
    draining_modes: list[str] = []
    task_counts: list[int] = []

    deps, harness = build_test_dependencies()
    coordinator = await ApplicationCoordinator.start(deps)
    try:

        def record_draining() -> None:
            draining_modes.append(harness.gate.mode.value)
            task_counts.append(len(harness.tasks.snapshot(TaskKind.MAINTENANCE)))

        await coordinator.run_maintenance(on_draining=record_draining)
        assert draining_modes == ["DRAINING"]
        assert task_counts == [1]
        assert len(harness.tasks.snapshot(TaskKind.MAINTENANCE)) == 0
        assert harness.gate.mode.value == "SERVING"

        def abort_draining() -> None:
            raise RuntimeError("handoff boom")

        with pytest.raises(RuntimeError, match="handoff boom"):
            await coordinator.run_maintenance(on_draining=abort_draining)
        assert len(harness.tasks.snapshot(TaskKind.MAINTENANCE)) == 0
        assert harness.gate.mode.value == "SERVING"
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_shutdown_settled_hook_ordering_and_failure_guarantees() -> None:
    order: list[str] = []

    async def close_ok() -> None:
        order.append("close_resources")

    def on_settled() -> None:
        assert "close_resources" in order
        assert harness.ownership.acquired
        order.append("on_shutdown_settled")

    deps, harness = build_test_dependencies(
        close_resources=close_ok,
        on_shutdown_settled=on_settled,
    )
    real_release = harness.ownership.release

    def tracked_release() -> None:
        order.append("ownership_release")
        real_release()

    harness.ownership.release = tracked_release  # type: ignore[method-assign]
    coordinator = await ApplicationCoordinator.start(deps)
    await coordinator.shutdown()
    assert order == ["close_resources", "on_shutdown_settled", "ownership_release"]
    assert not harness.ownership.acquired

    # Close failure -> on_shutdown_settled skipped & ownership retained
    called_after_close_fail = False

    async def close_boom() -> None:
        raise RuntimeError("close failed")

    def never_settled() -> None:
        nonlocal called_after_close_fail
        called_after_close_fail = True

    deps2, harness2 = build_test_dependencies(
        close_resources=close_boom,
        on_shutdown_settled=never_settled,
    )
    coord2 = await ApplicationCoordinator.start(deps2)
    with pytest.raises(RuntimeError, match="close failed"):
        await coord2.shutdown()
    assert called_after_close_fail is False
    assert harness2.ownership.acquired
    harness2.ownership.release()

    # Unsettled worker timeout -> on_shutdown_settled skipped & RuntimeShutdownFailedError
    deps_unsettled, harness_unsettled = build_test_dependencies(
        on_shutdown_settled=never_settled,
        shutdown_timeout_seconds=0.05,
    )
    coord_unsettled = await ApplicationCoordinator.start(deps_unsettled)

    async def stubborn_worker() -> None:
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            await asyncio.sleep(5.0)

    stubborn = asyncio.create_task(stubborn_worker())
    harness_unsettled.tasks.register(stubborn, TaskKind.CACHE)
    try:
        with pytest.raises(RuntimeShutdownFailedError):
            await coord_unsettled.shutdown()
        assert called_after_close_fail is False
        assert harness_unsettled.ownership.acquired
    finally:
        stubborn.cancel()
        harness_unsettled.ownership.release()

    # Hook failure -> ownership NOT released, error propagates without false success
    def settled_boom() -> None:
        raise RuntimeError("receipt write failed")

    deps3, harness3 = build_test_dependencies(on_shutdown_settled=settled_boom)
    coord3 = await ApplicationCoordinator.start(deps3)
    with pytest.raises(RuntimeError, match="receipt write failed"):
        await coord3.shutdown()
    assert harness3.ownership.acquired
    harness3.ownership.release()

    # Hook cancellation -> ownership NOT released, CancelledError propagates
    async def settled_cancel() -> None:
        raise asyncio.CancelledError

    deps4, harness4 = build_test_dependencies(on_shutdown_settled=settled_cancel)
    coord4 = await ApplicationCoordinator.start(deps4)
    with pytest.raises(asyncio.CancelledError):
        await coord4.shutdown()
    assert harness4.ownership.acquired
    harness4.ownership.release()
