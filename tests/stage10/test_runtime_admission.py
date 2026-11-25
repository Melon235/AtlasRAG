"""A-R2 deterministic admission, maintenance and shutdown interleavings."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from runtime_harness import (
    CacheHitRetrieval,
    Probe,
    build_harness,
    pump_until,
    request,
)

from atlasrag.domain.errors import (
    RuntimeNotReadyError,
    RuntimeShuttingDownError,
)
from atlasrag.runtime.gate import RuntimeGate
from atlasrag.runtime.health import HealthSnapshot
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.runtime.tasks import TaskKind
from atlasrag.services.sync.models import ServingSafety


@pytest.mark.asyncio
async def test_query_admission_wins_and_mutation_waits_for_query_settlement() -> None:
    harness = build_harness()
    await harness.start()
    gate = harness.gate
    tasks = harness.tasks
    retrieval = harness.retrieval
    retrieval.hold = True
    query_task = asyncio.create_task(
        harness.coordinator.run_query(request("session-a"))
    )
    await asyncio.wait_for(retrieval.started.wait(), timeout=1)
    assert gate.active_queries == 1
    assert tasks.snapshot(TaskKind.QUERY) == (query_task,)
    observed: dict[str, object] = {}

    async def before_mutation() -> None:
        observed["active"] = gate.active_queries
        observed["mode"] = gate.mode
        observed["retrieval_settled"] = retrieval.settled.is_set()
        observed["query_done"] = query_task.done()

    harness.sync.on_call = before_mutation
    maintenance_task = asyncio.create_task(harness.coordinator.run_maintenance())
    await pump_until(
        lambda: gate.mode is RuntimeMode.DRAINING, label="runtime DRAINING"
    )
    assert harness.sync.calls == 0
    with pytest.raises(RuntimeNotReadyError):
        await gate.acquire_query()
    assert retrieval.calls == 1

    retrieval.release.set()
    envelope = await asyncio.wait_for(query_task, timeout=2)
    result = await asyncio.wait_for(maintenance_task, timeout=2)

    assert envelope.response.answer_text == "answer"
    assert result.serving_safety is ServingSafety.SAFE_TO_SERVE
    assert observed == {
        "active": 0,
        "mode": RuntimeMode.MAINTENANCE,
        "retrieval_settled": True,
        "query_done": True,
    }
    assert harness.sync.calls == 1
    assert gate.mode.value == "SERVING"
    assert gate.active_queries == 0
    lease = await gate.acquire_query()
    await lease.release()


@pytest.mark.asyncio
async def test_maintenance_admission_wins_and_query_never_reaches_retrieval() -> None:
    harness = build_harness()
    await harness.start()
    gate = harness.gate
    harness.sync.hold = True
    maintenance_task = asyncio.create_task(harness.coordinator.run_maintenance())
    await asyncio.wait_for(harness.sync.started.wait(), timeout=1)
    assert gate.mode.value == "MAINTENANCE"

    with pytest.raises(RuntimeNotReadyError):
        await harness.coordinator.run_query(request("session-b"))
    assert harness.retrieval.calls == 0
    assert gate.active_queries == 0

    harness.sync.release.set()
    await asyncio.wait_for(maintenance_task, timeout=2)
    assert gate.mode.value == "SERVING"
    envelope = await harness.coordinator.run_query(request("session-b"))
    assert envelope.response.answer_text == "answer"
    assert harness.retrieval.calls == 1


@pytest.mark.asyncio
async def test_shutdown_before_admission_rejects_new_work_without_retrieval() -> None:
    harness = build_harness()
    await harness.start()
    await asyncio.wait_for(harness.coordinator.shutdown(), timeout=2)

    with pytest.raises(RuntimeShuttingDownError):
        await harness.coordinator.run_query(request("session-c"))
    with pytest.raises(RuntimeShuttingDownError):
        await harness.coordinator.run_maintenance()

    assert harness.retrieval.calls == 0
    assert harness.sync.calls == 0
    assert harness.gate.active_queries == 0
    assert harness.tasks.snapshot() == ()
    assert harness.gate.mode.value == "SHUTTING_DOWN"
    assert harness.close.calls == 1
    assert harness.ownership.released == 1


@pytest.mark.asyncio
async def test_shutdown_waits_for_tracked_pre_admission_health_probe() -> None:
    postgres = Probe()
    probe_started = asyncio.Event()
    probe_release = asyncio.Event()
    postgres.on_call(_healthy)
    postgres.block_call(probe_started, probe_release)
    harness = build_harness(probes={"postgres": postgres}, refresh_interval=0.0)
    await harness.start()
    query_task = asyncio.create_task(
        harness.coordinator.run_query(request("session-d"))
    )
    await asyncio.wait_for(probe_started.wait(), timeout=1)
    assert harness.gate.active_queries == 0
    assert query_task in harness.tasks.snapshot(TaskKind.HEALTH)

    shutdown_task = asyncio.create_task(harness.coordinator.shutdown())
    await pump_until(lambda: harness.tasks.closed, label="task registry closed")
    assert harness.gate.mode.value == "SHUTTING_DOWN"
    assert not shutdown_task.done()
    assert harness.close.calls == 0

    probe_release.set()
    with pytest.raises(RuntimeShuttingDownError):
        await asyncio.wait_for(query_task, timeout=2)
    await asyncio.wait_for(shutdown_task, timeout=2)

    assert harness.retrieval.calls == 0
    assert harness.gate.active_queries == 0
    assert harness.tasks.snapshot() == ()
    assert harness.close.calls == 1
    assert harness.ownership.released == 1


@pytest.mark.asyncio
async def test_shutdown_after_admission_allows_query_to_settle_before_close() -> None:
    harness = build_harness()
    await harness.start()
    harness.freshness.recover_hold = asyncio.Event()
    observed: dict[str, int] = {}

    def on_close() -> None:
        observed["active"] = harness.gate.active_queries
        observed["tasks"] = len(harness.tasks.snapshot())
        observed["retrieval"] = harness.retrieval.calls

    harness.close.on_close = on_close
    query_task = asyncio.create_task(
        harness.coordinator.run_query(request("session-e"))
    )
    await asyncio.wait_for(harness.freshness.recover_started.wait(), timeout=1)
    assert harness.gate.active_queries == 1
    assert harness.tasks.snapshot(TaskKind.QUERY) == (query_task,)
    assert harness.retrieval.calls == 0

    shutdown_task = asyncio.create_task(harness.coordinator.shutdown())
    await pump_until(lambda: harness.tasks.closed, label="task registry closed")
    assert not shutdown_task.done()
    assert harness.close.calls == 0
    assert harness.retrieval.cancel_seen == 0

    assert harness.freshness.recover_hold is not None
    harness.freshness.recover_hold.set()
    envelope = await asyncio.wait_for(query_task, timeout=2)
    await asyncio.wait_for(shutdown_task, timeout=2)

    assert envelope.response.answer_text == "answer"
    assert harness.retrieval.calls == 1
    assert harness.gate.active_queries == 0
    assert harness.tasks.snapshot() == ()
    assert observed == {"active": 0, "tasks": 0, "retrieval": 1}
    assert harness.close.calls == 1
    assert harness.ownership.released == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown_created_first", [True, False])
async def test_gate_admission_and_shutdown_offer_atomic_outcomes(
    shutdown_created_first: bool,
) -> None:
    gate = RuntimeGate()
    await gate.mark_serving()
    await gate.set_admission_ready(True)
    leases: list[object] = []

    async def admit() -> None:
        leases.append(await gate.acquire_query())

    async def stop() -> None:
        await gate.begin_shutdown()

    if shutdown_created_first:
        stop_task = asyncio.create_task(stop())
        admit_task = asyncio.create_task(admit())
    else:
        admit_task = asyncio.create_task(admit())
        stop_task = asyncio.create_task(stop())
    admit_result, _ = await asyncio.gather(
        admit_task, stop_task, return_exceptions=True
    )

    assert gate.mode.value == "SHUTTING_DOWN"
    if shutdown_created_first:
        assert isinstance(admit_result, RuntimeShuttingDownError)
        assert gate.active_queries == 0
    else:
        assert admit_result is None
        assert gate.active_queries == 1
        await leases[0].release()  # type: ignore[attr-defined]
        assert gate.active_queries == 0


@pytest.mark.asyncio
async def test_registration_failure_after_lease_acquisition_releases_lease() -> None:
    harness = build_harness()
    await harness.start()
    registry = harness.tasks

    async def close_registry_then_snapshot() -> HealthSnapshot:
        await registry.close()
        return cast(
            HealthSnapshot,
            SimpleNamespace(
                generation=0,
                hard_healthy=True,
                runtime=RuntimeMode.SERVING,
                lifecycle_generation=harness.gate.lifecycle_generation,
            ),
        )

    harness.health.refresh_if_stale = close_registry_then_snapshot  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await harness.coordinator.run_query(request("session-f"))

    assert registry.closed
    assert harness.gate.active_queries == 0
    assert harness.retrieval.calls == 0
    assert harness.gate.mode.value == "SERVING"


@pytest.mark.asyncio
async def test_cancellation_during_pre_admission_health_refresh_cleans_up() -> None:
    postgres = Probe()
    probe_started = asyncio.Event()
    probe_release = asyncio.Event()
    postgres.on_call(_healthy)
    postgres.block_call(probe_started, probe_release)
    harness = build_harness(probes={"postgres": postgres}, refresh_interval=0.0)
    await harness.start()
    query_task = asyncio.create_task(
        harness.coordinator.run_query(request("session-g"))
    )
    await asyncio.wait_for(probe_started.wait(), timeout=1)
    query_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await query_task

    assert harness.tasks.snapshot() == ()
    assert harness.gate.active_queries == 0
    assert harness.retrieval.calls == 0


@pytest.mark.asyncio
async def test_cancellation_after_lease_acquisition_releases_exactly_once() -> None:
    harness = build_harness()
    await harness.start()
    harness.retrieval.hold = True
    releases: list[bool] = []
    original = harness.gate.release_query

    async def counted(lease: object) -> None:
        releases.append(lease.released)  # type: ignore[attr-defined]
        await original(lease)  # type: ignore[arg-type]

    harness.gate.release_query = counted  # type: ignore[method-assign]
    query_task = asyncio.create_task(
        harness.coordinator.run_query(request("session-h"))
    )
    await asyncio.wait_for(harness.retrieval.started.wait(), timeout=1)
    query_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await query_task

    assert harness.retrieval.cancel_seen == 1
    assert releases == [False]
    assert harness.gate.active_queries == 0
    assert harness.tasks.snapshot() == ()


@pytest.mark.asyncio
async def test_repeated_cancellation_during_cleanup_does_not_double_release() -> None:
    harness = build_harness()
    await harness.start()
    harness.retrieval.hold = True
    harness.retrieval.settle_gate = asyncio.Event()
    releases: list[bool] = []
    original = harness.gate.release_query

    async def counted(lease: object) -> None:
        releases.append(lease.released)  # type: ignore[attr-defined]
        await original(lease)  # type: ignore[arg-type]

    harness.gate.release_query = counted  # type: ignore[method-assign]
    query_task = asyncio.create_task(
        harness.coordinator.run_query(request("session-i"))
    )
    await asyncio.wait_for(harness.retrieval.started.wait(), timeout=1)
    query_task.cancel()
    await pump_until(
        lambda: harness.retrieval.cancel_seen == 1, label="first cancellation"
    )
    query_task.cancel()
    assert harness.retrieval.settle_gate is not None
    harness.retrieval.settle_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(query_task, timeout=2)

    assert len(releases) == 1
    assert harness.gate.active_queries == 0
    assert harness.tasks.snapshot() == ()


@pytest.mark.asyncio
async def test_health_tracking_is_distinct_from_query_lease_accounting() -> None:
    postgres = Probe()
    probe_started = asyncio.Event()
    probe_release = asyncio.Event()
    postgres.on_call(_healthy)
    postgres.block_call(probe_started, probe_release)
    harness = build_harness(probes={"postgres": postgres}, refresh_interval=0.0)
    await harness.start()
    health_task = asyncio.create_task(harness.coordinator.health())
    await asyncio.wait_for(probe_started.wait(), timeout=1)

    assert harness.gate.active_queries == 0
    assert health_task in harness.tasks.snapshot(TaskKind.HEALTH)
    assert harness.tasks.snapshot(TaskKind.QUERY) == ()

    probe_release.set()
    snapshot = await asyncio.wait_for(health_task, timeout=2)
    assert snapshot.hard_healthy
    assert harness.tasks.snapshot() == ()

    envelope = await harness.coordinator.run_query(request("session-j"))
    assert envelope.response.answer_text == "answer"
    assert harness.retrieval.observed_active == [1]
    assert harness.gate.active_queries == 0


@pytest.mark.asyncio
async def test_cache_hit_while_serving_traverses_application_admission() -> None:
    gate = RuntimeGate()
    retrieval = CacheHitRetrieval(gate)
    harness = build_harness(gate=gate, retrieval=retrieval)
    await harness.start()

    envelope = await harness.coordinator.run_query(request("session-k"))

    assert envelope.response.answer_text == "answer"
    assert retrieval.calls == 1
    assert retrieval.observed_active == [1]
    assert gate.active_queries == 0
    assert harness.tasks.snapshot() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_mode", ["DRAINING", "MAINTENANCE", "SHUTTING_DOWN"])
async def test_cache_hit_attempt_is_rejected_outside_serving(
    blocked_mode: str,
) -> None:
    gate = RuntimeGate()
    retrieval = CacheHitRetrieval(gate)
    harness = build_harness(gate=gate, retrieval=retrieval)
    await harness.start()

    lease = None
    if blocked_mode == "DRAINING":
        lease = await gate.begin_maintenance()
        expected: type[Exception] = RuntimeNotReadyError
    elif blocked_mode == "MAINTENANCE":
        lease = await gate.begin_maintenance()
        await lease.enter()
        expected = RuntimeNotReadyError
    else:
        await gate.begin_shutdown()
        expected = RuntimeShuttingDownError
    assert gate.mode.value == blocked_mode

    with pytest.raises(expected):
        await harness.coordinator.run_query(request("session-l"))
    assert retrieval.calls == 0
    assert gate.active_queries == 0

    if lease is not None:
        await lease.abort()


@pytest.mark.asyncio
async def test_shutdown_waits_for_admitted_cache_hit_finalization() -> None:
    gate = RuntimeGate()
    retrieval = CacheHitRetrieval(gate)
    retrieval.finalize_gate = asyncio.Event()
    harness = build_harness(gate=gate, retrieval=retrieval)
    await harness.start()
    observed: dict[str, int] = {}

    def on_close() -> None:
        observed["active"] = gate.active_queries
        observed["tasks"] = len(harness.tasks.snapshot())

    harness.close.on_close = on_close
    query_task = asyncio.create_task(
        harness.coordinator.run_query(request("session-m"))
    )
    await asyncio.wait_for(retrieval.started.wait(), timeout=1)
    assert gate.active_queries == 1

    shutdown_task = asyncio.create_task(harness.coordinator.shutdown())
    await pump_until(lambda: harness.tasks.closed, label="task registry closed")
    assert not shutdown_task.done()
    assert harness.close.calls == 0
    assert retrieval.cancel_seen == 0

    assert retrieval.finalize_gate is not None
    retrieval.finalize_gate.set()
    envelope = await asyncio.wait_for(query_task, timeout=2)
    await asyncio.wait_for(shutdown_task, timeout=2)

    assert envelope.response.answer_text == "answer"
    assert observed == {"active": 0, "tasks": 0}
    assert harness.close.calls == 1
    assert harness.ownership.released == 1


async def _healthy() -> None:
    return None
