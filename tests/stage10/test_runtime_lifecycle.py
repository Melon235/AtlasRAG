"""A-R2 graceful shutdown, cancellation and maintenance/shutdown interactions."""

from __future__ import annotations

import asyncio

import pytest
from runtime_harness import build_harness, pump_until, request

from atlasrag.domain.errors import (
    MaintenanceDrainTimeoutError,
    RuntimeNotReadyError,
    RuntimeShutdownFailedError,
    RuntimeShuttingDownError,
)
from atlasrag.runtime.gate import RuntimeGate
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.runtime.resources import ResourceGuard
from atlasrag.runtime.tasks import TaskKind


@pytest.mark.asyncio
async def test_graceful_shutdown_drains_admitted_query_without_cancellation() -> None:
    harness = build_harness(shutdown_timeout=1.0)
    await harness.start()
    gate = harness.gate
    retrieval = harness.retrieval
    retrieval.hold = True
    observed: dict[str, int] = {}

    def on_close() -> None:
        observed["active"] = gate.active_queries
        observed["tasks"] = len(harness.tasks.snapshot())

    harness.close.on_close = on_close
    query_task = asyncio.create_task(
        harness.coordinator.run_query(request("session-r"))
    )
    await asyncio.wait_for(retrieval.started.wait(), timeout=1)
    shutdown_task = asyncio.create_task(harness.coordinator.shutdown())
    await pump_until(lambda: harness.tasks.closed, label="task registry closed")

    assert harness.tasks.snapshot() == (query_task,)
    assert harness.tasks.snapshot(TaskKind.QUERY) == (query_task,)
    assert not shutdown_task.done()
    assert retrieval.cancel_seen == 0
    assert harness.close.calls == 0
    assert gate.mode.value == "SHUTTING_DOWN"

    with pytest.raises(RuntimeShuttingDownError):
        await harness.coordinator.run_query(request("session-r2"))
    assert retrieval.calls == 1
    assert gate.active_queries == 1

    retrieval.release.set()
    envelope = await asyncio.wait_for(query_task, timeout=2)
    await asyncio.wait_for(shutdown_task, timeout=2)

    assert envelope.response.answer_text == "answer"
    assert retrieval.cancel_seen == 0
    assert observed == {"active": 0, "tasks": 0}
    assert harness.close.calls == 1
    assert harness.ownership.released == 1
    assert harness.log.index("close") < harness.log.index("release")
    assert gate.mode.value == "SHUTTING_DOWN"


@pytest.mark.asyncio
async def test_independent_queries_are_not_globally_serialized() -> None:
    harness = build_harness()
    await harness.start()
    harness.retrieval.hold = True
    first = asyncio.create_task(harness.coordinator.run_query(request("session-s1")))
    second = asyncio.create_task(harness.coordinator.run_query(request("session-s2")))
    await pump_until(
        lambda: harness.retrieval.calls == 2, label="both queries admitted"
    )

    assert harness.gate.active_queries == 2
    assert not first.done()
    assert not second.done()
    assert sorted(harness.retrieval.observed_active) == [1, 2]
    assert harness.freshness.recover_calls == 2

    harness.retrieval.release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

    assert [result.response.answer_text for result in results] == ["answer", "answer"]
    assert harness.gate.active_queries == 0
    assert harness.tasks.snapshot() == ()


@pytest.mark.asyncio
async def test_cancellation_settlement_closes_resources_after_worker_settles() -> None:
    harness = build_harness(shutdown_timeout=0.01)
    await harness.start()
    settled = asyncio.Event()
    observed: dict[str, bool] = {}

    def on_close() -> None:
        observed["settled"] = settled.is_set()

    harness.close.on_close = on_close

    async def cooperative() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            settled.set()
            return

    worker = asyncio.create_task(cooperative())
    harness.tasks.register(worker, TaskKind.CACHE)

    await asyncio.wait_for(harness.coordinator.shutdown(), timeout=2)

    assert settled.is_set()
    assert observed == {"settled": True}
    assert harness.close.calls == 1
    assert harness.ownership.released == 1
    assert harness.tasks.snapshot() == ()
    assert harness.gate.mode.value == "SHUTTING_DOWN"


@pytest.mark.asyncio
async def test_canceled_query_settlement_is_awaited_before_resource_close() -> None:
    harness = build_harness(shutdown_timeout=0.01)
    await harness.start()
    harness.retrieval.hold = True
    harness.retrieval.settle_gate = asyncio.Event()
    observed: dict[str, int] = {}

    def on_close() -> None:
        observed["active"] = harness.gate.active_queries
        observed["tasks"] = len(harness.tasks.snapshot())

    harness.close.on_close = on_close
    query_task = asyncio.create_task(
        harness.coordinator.run_query(request("session-t"))
    )
    await asyncio.wait_for(harness.retrieval.started.wait(), timeout=1)
    shutdown_task = asyncio.create_task(harness.coordinator.shutdown())
    await pump_until(
        lambda: harness.retrieval.cancel_seen == 1, label="query cancellation"
    )

    assert not shutdown_task.done()
    assert harness.close.calls == 0
    assert harness.gate.active_queries == 1

    assert harness.retrieval.settle_gate is not None
    harness.retrieval.settle_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(query_task, timeout=2)
    await asyncio.wait_for(shutdown_task, timeout=2)

    assert observed == {"active": 0, "tasks": 0}
    assert harness.close.calls == 1
    assert harness.ownership.released == 1


@pytest.mark.asyncio
async def test_queued_guard_work_cannot_begin_after_shutdown() -> None:
    guard = ResourceGuard(capacity=1)
    entered = asyncio.Event()
    release = asyncio.Event()
    executed: list[str] = []

    async def holder() -> None:
        async with guard.slot():
            entered.set()
            await release.wait()

    async def queued() -> None:
        async with guard.slot():
            executed.append("queued")

    holder_task = asyncio.create_task(holder())
    await asyncio.wait_for(entered.wait(), timeout=1)
    queued_task = asyncio.create_task(queued())
    await asyncio.sleep(0)
    await guard.begin_shutdown()
    release.set()
    await asyncio.wait_for(holder_task, timeout=1)
    with pytest.raises(RuntimeShuttingDownError):
        await asyncio.wait_for(queued_task, timeout=1)
    assert executed == []


@pytest.mark.asyncio
async def test_maintenance_ownership_is_exclusive() -> None:
    harness = build_harness()
    await harness.start()
    harness.sync.hold = True
    first = asyncio.create_task(harness.coordinator.run_maintenance())
    await asyncio.wait_for(harness.sync.started.wait(), timeout=1)
    assert harness.gate.mode.value == "MAINTENANCE"

    with pytest.raises(RuntimeNotReadyError):
        await harness.coordinator.run_maintenance()
    assert harness.sync.calls == 1

    harness.sync.release.set()
    await asyncio.wait_for(first, timeout=2)
    assert harness.gate.mode.value == "SERVING"


@pytest.mark.asyncio
async def test_maintenance_drain_timeout_preserves_query_and_restores_admission() -> (
    None
):
    harness = build_harness(drain_timeout=0.0)
    await harness.start()
    harness.retrieval.hold = True
    query_task = asyncio.create_task(
        harness.coordinator.run_query(request("session-u"))
    )
    await asyncio.wait_for(harness.retrieval.started.wait(), timeout=1)

    with pytest.raises(MaintenanceDrainTimeoutError):
        await harness.coordinator.run_maintenance()

    assert harness.sync.calls == 0
    assert harness.gate.mode.value == "SERVING"
    assert harness.gate.active_queries == 1
    assert harness.retrieval.cancel_seen == 0
    assert not query_task.done()

    restored = await harness.gate.acquire_query()
    await restored.release()

    harness.retrieval.release.set()
    envelope = await asyncio.wait_for(query_task, timeout=2)
    assert envelope.response.answer_text == "answer"
    assert harness.gate.mode.value == "SERVING"
    assert harness.gate.active_queries == 0


@pytest.mark.asyncio
async def test_maintenance_cancellation_before_entry_restores_serving() -> None:
    harness = build_harness(drain_timeout=1.0)
    await harness.start()
    harness.retrieval.hold = True
    query_task = asyncio.create_task(
        harness.coordinator.run_query(request("session-v"))
    )
    await asyncio.wait_for(harness.retrieval.started.wait(), timeout=1)
    maintenance_task = asyncio.create_task(harness.coordinator.run_maintenance())
    await pump_until(
        lambda: harness.gate.mode is RuntimeMode.DRAINING, label="runtime DRAINING"
    )
    maintenance_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await maintenance_task

    assert harness.sync.calls == 0
    assert harness.gate.mode.value == "SERVING"
    assert harness.gate.active_queries == 1
    assert harness.retrieval.cancel_seen == 0
    restored = await harness.gate.acquire_query()
    await restored.release()
    harness.retrieval.release.set()
    await asyncio.wait_for(query_task, timeout=2)
    assert harness.gate.mode.value == "SERVING"


@pytest.mark.asyncio
async def test_recovery_cancellation_after_maintenance_entry_remains_blocked() -> None:
    harness = build_harness()
    await harness.gate.mark_blocked()
    harness.sync.hold = True
    recovery_task = asyncio.create_task(harness.coordinator.recover())
    await asyncio.wait_for(harness.sync.started.wait(), timeout=1)
    assert harness.gate.mode.value == "MAINTENANCE"

    recovery_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovery_task

    assert harness.sync.cancel_seen == 1
    assert harness.gate.mode.value == "BLOCKED"
    with pytest.raises(RuntimeNotReadyError):
        await harness.gate.acquire_query()
    assert harness.retrieval.calls == 0


@pytest.mark.asyncio
async def test_maintenance_exception_after_mutation_blocks_and_never_serves() -> None:
    harness = build_harness()
    harness.sync.error = RuntimeError("index mutation failed")
    await harness.start()
    enforced_at_startup = harness.freshness.enforce_calls

    with pytest.raises(RuntimeError):
        await harness.coordinator.run_maintenance()

    assert harness.gate.mode.value == "BLOCKED"
    assert harness.freshness.enforce_calls == enforced_at_startup
    with pytest.raises(RuntimeNotReadyError):
        await harness.gate.acquire_query()
    assert harness.retrieval.calls == 0


@pytest.mark.asyncio
async def test_shutdown_during_maintenance_keeps_shutting_down_terminal() -> None:
    harness = build_harness(shutdown_timeout=1.0)
    await harness.start()
    harness.sync.hold = True
    maintenance_task = asyncio.create_task(harness.coordinator.run_maintenance())
    await asyncio.wait_for(harness.sync.started.wait(), timeout=1)
    assert harness.gate.mode.value == "MAINTENANCE"

    shutdown_task = asyncio.create_task(harness.coordinator.shutdown())
    await pump_until(lambda: harness.tasks.closed, label="task registry closed")
    assert not shutdown_task.done()
    assert harness.close.calls == 0
    assert harness.gate.mode.value == "SHUTTING_DOWN"

    harness.sync.release.set()
    result = await asyncio.wait_for(maintenance_task, timeout=2)
    await asyncio.wait_for(shutdown_task, timeout=2)

    assert result.serving_safety.value == "SAFE_TO_SERVE"
    assert harness.gate.mode.value == "SHUTTING_DOWN"
    assert harness.close.calls == 1
    assert harness.ownership.released == 1
    with pytest.raises(RuntimeShuttingDownError):
        await harness.coordinator.run_maintenance()


@pytest.mark.asyncio
async def test_shutdown_during_drain_keeps_shutting_down_terminal() -> None:
    gate = RuntimeGate()
    await gate.mark_serving()
    await gate.set_admission_ready(True)
    lease = await gate.begin_maintenance()
    assert gate.mode.value == "DRAINING"

    await gate.begin_shutdown()
    with pytest.raises(RuntimeShuttingDownError):
        await lease.enter()
    await lease.complete(safe=True)
    assert gate.mode.value == "SHUTTING_DOWN"

    with pytest.raises(RuntimeShuttingDownError):
        await gate.begin_maintenance()
    with pytest.raises(RuntimeShuttingDownError):
        await gate.mark_serving()
    await gate.mark_blocked()
    assert gate.mode.value == "SHUTTING_DOWN"
    with pytest.raises(RuntimeShuttingDownError):
        await gate.acquire_query()

    aborting_gate = RuntimeGate()
    await aborting_gate.mark_serving()
    aborting_lease = await aborting_gate.begin_maintenance()
    await aborting_gate.begin_shutdown()
    await aborting_lease.abort()
    assert aborting_gate.mode.value == "SHUTTING_DOWN"


@pytest.mark.asyncio
async def test_repeated_successful_shutdown_closes_once_and_stays_terminal() -> None:
    harness = build_harness()
    await harness.start()
    await asyncio.wait_for(harness.coordinator.shutdown(), timeout=2)
    await asyncio.wait_for(harness.coordinator.shutdown(), timeout=2)

    assert harness.close.calls == 1
    assert harness.ownership.released == 1
    assert harness.gate.mode.value == "SHUTTING_DOWN"
    with pytest.raises(RuntimeShuttingDownError):
        await harness.gate.mark_serving()


@pytest.mark.asyncio
async def test_concurrent_shutdown_calls_close_resources_once() -> None:
    harness = build_harness()
    await harness.start()
    harness.close.hold = asyncio.Event()
    first = asyncio.create_task(harness.coordinator.shutdown())
    second = asyncio.create_task(harness.coordinator.shutdown())
    await asyncio.wait_for(harness.close.started.wait(), timeout=1)
    await asyncio.sleep(0)

    assert harness.close.calls == 1
    assert not first.done()
    assert harness.gate.mode.value == "SHUTTING_DOWN"

    assert harness.close.hold is not None
    harness.close.hold.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

    assert harness.close.calls == 1
    assert harness.ownership.released == 1
    assert harness.gate.mode.value == "SHUTTING_DOWN"


@pytest.mark.asyncio
async def test_failed_cleanup_is_not_falsely_successful_on_repeat() -> None:
    harness = build_harness()
    await harness.start()
    harness.close.error = RuntimeError("close failed")

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(harness.coordinator.shutdown(), timeout=2)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(harness.coordinator.shutdown(), timeout=2)

    assert harness.close.calls == 2
    assert harness.ownership.released == 0
    assert harness.coordinator._shutdown_done is False
    assert harness.gate.mode.value == "SHUTTING_DOWN"

    harness.close.error = None
    await asyncio.wait_for(harness.coordinator.shutdown(), timeout=2)
    assert harness.ownership.released == 1
    assert harness.coordinator._shutdown_done is True


@pytest.mark.asyncio
async def test_unsettled_worker_failure_preserves_recovery_boundary() -> None:
    harness = build_harness(shutdown_timeout=0.02)
    await harness.start()

    async def stubborn() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await asyncio.sleep(0.1)

    worker = asyncio.create_task(stubborn())
    harness.tasks.register(worker, TaskKind.CACHE)

    with pytest.raises(RuntimeShutdownFailedError):
        await asyncio.wait_for(harness.coordinator.shutdown(), timeout=2)
    assert harness.close.calls == 0
    assert harness.ownership.released == 0
    assert harness.coordinator._shutdown_done is False
    assert harness.gate.mode.value == "SHUTTING_DOWN"

    await asyncio.wait_for(worker, timeout=2)
    await asyncio.wait_for(harness.coordinator.shutdown(), timeout=2)
    assert harness.close.calls == 1
    assert harness.ownership.released == 1
    assert harness.gate.mode.value == "SHUTTING_DOWN"
