"""A-R2 health ownership, bounded refresh and stale-probe lifecycle evidence."""

from __future__ import annotations

import asyncio

import pytest
from runtime_harness import Probe, build_harness, build_health, pump_until, request

from atlasrag.domain.errors import (
    RuntimeNotReadyError,
    RuntimeShuttingDownError,
)
from atlasrag.runtime import health as health_module
from atlasrag.runtime.gate import RuntimeGate
from atlasrag.runtime.health import DependencyHealth
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.runtime.tasks import TaskKind


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _healthy() -> None:
    return None


@pytest.mark.asyncio
async def test_public_health_request_is_tracked_until_probe_settles() -> None:
    postgres = Probe()
    probe_started = asyncio.Event()
    probe_release = asyncio.Event()
    postgres.on_call(_healthy)
    postgres.block_call(probe_started, probe_release)
    harness = build_harness(probes={"postgres": postgres}, refresh_interval=0.0)
    await harness.start()
    health_task = asyncio.create_task(harness.coordinator.health())
    await asyncio.wait_for(probe_started.wait(), timeout=1)
    assert health_task in harness.tasks.snapshot(TaskKind.HEALTH)
    assert harness.gate.active_queries == 0

    shutdown_task = asyncio.create_task(harness.coordinator.shutdown())
    await pump_until(lambda: harness.tasks.closed, label="task registry closed")
    assert not shutdown_task.done()
    assert harness.close.calls == 0

    probe_release.set()
    snapshot = await asyncio.wait_for(health_task, timeout=2)
    await asyncio.wait_for(shutdown_task, timeout=2)

    assert snapshot.hard_healthy
    assert harness.tasks.snapshot() == ()
    assert harness.close.calls == 1
    assert harness.ownership.released == 1


@pytest.mark.asyncio
async def test_public_readiness_request_is_tracked_until_probe_settles() -> None:
    postgres = Probe()
    probe_started = asyncio.Event()
    probe_release = asyncio.Event()
    postgres.on_call(_healthy)
    postgres.block_call(probe_started, probe_release)
    harness = build_harness(probes={"postgres": postgres}, refresh_interval=0.0)
    await harness.start()
    readiness_task = asyncio.create_task(harness.coordinator.readiness())
    await asyncio.wait_for(probe_started.wait(), timeout=1)
    assert readiness_task in harness.tasks.snapshot(TaskKind.HEALTH)

    shutdown_task = asyncio.create_task(harness.coordinator.shutdown())
    await pump_until(lambda: harness.tasks.closed, label="task registry closed")
    assert not shutdown_task.done()
    assert harness.close.calls == 0

    probe_release.set()
    ready = await asyncio.wait_for(readiness_task, timeout=2)
    await asyncio.wait_for(shutdown_task, timeout=2)

    assert ready is False
    assert harness.tasks.snapshot() == ()
    assert harness.close.calls == 1
    assert harness.ownership.released == 1


@pytest.mark.asyncio
async def test_fresh_snapshot_is_reused_and_expired_snapshot_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(health_module, "monotonic", clock)
    gate = RuntimeGate()
    await gate.mark_serving()
    postgres = Probe()
    registry = build_health(gate, probes={"postgres": postgres}, refresh_interval=10.0)
    registry.set_knowledge_verified(True)

    first = await registry.refresh_if_stale()
    assert postgres.calls == 1
    reused = await registry.refresh_if_stale()
    assert reused.generation == first.generation
    assert postgres.calls == 1

    clock.advance(11.0)
    refreshed = await registry.refresh_if_stale()
    assert refreshed.generation == first.generation + 1
    assert postgres.calls == 2
    assert refreshed.lifecycle_generation == gate.lifecycle_generation


@pytest.mark.asyncio
async def test_concurrent_health_refreshes_are_bounded_and_serialized() -> None:
    gate = RuntimeGate()
    await gate.mark_serving()
    postgres = Probe()
    probe_started = asyncio.Event()
    probe_release = asyncio.Event()
    postgres.block_call(probe_started, probe_release)
    registry = build_health(gate, probes={"postgres": postgres}, refresh_interval=0.0)
    registry.set_knowledge_verified(True)

    refresh_tasks = [asyncio.create_task(registry.refresh()) for _ in range(3)]
    await asyncio.wait_for(probe_started.wait(), timeout=1)
    assert postgres.calls == 1
    assert postgres.max_active == 1

    probe_release.set()
    snapshots = await asyncio.wait_for(asyncio.gather(*refresh_tasks), timeout=2)
    assert postgres.calls == 3
    assert postgres.max_active == 1
    assert len({snapshot.generation for snapshot in snapshots}) == 3


@pytest.mark.asyncio
async def test_probe_failure_closes_admission_then_recovers() -> None:
    postgres = Probe()
    harness = build_harness(probes={"postgres": postgres}, refresh_interval=0.0)
    await harness.start()
    postgres.default_error = RuntimeError("postgres down")

    with pytest.raises(RuntimeNotReadyError):
        await harness.coordinator.run_query(request("session-n"))
    assert harness.retrieval.calls == 0
    assert harness.gate.mode.value == "SERVING"
    assert await harness.coordinator.readiness() is False

    postgres.default_error = None
    envelope = await harness.coordinator.run_query(request("session-n"))
    assert envelope.response.answer_text == "answer"
    assert harness.retrieval.calls == 1
    assert await harness.coordinator.readiness() is True
    assert harness.verify.calls == 1


@pytest.mark.asyncio
async def test_soft_dependency_failure_does_not_block_readiness() -> None:
    redis = Probe()
    redis.default_error = RuntimeError("redis down")
    harness = build_harness(probes={"redis": redis}, refresh_interval=0.0)
    await harness.start()

    snapshot = await harness.coordinator.health()
    assert snapshot.redis is DependencyHealth.UNAVAILABLE
    assert snapshot.hard_healthy
    assert await harness.coordinator.readiness() is True
    envelope = await harness.coordinator.run_query(request("session-o"))
    assert envelope.response.answer_text == "answer"
    assert harness.retrieval.calls == 1


@pytest.mark.asyncio
async def test_readiness_uses_current_runtime_mode_not_stale_snapshot() -> None:
    harness = build_harness()
    await harness.start()
    assert await harness.coordinator.readiness() is True

    lease = await harness.gate.begin_maintenance()
    assert await harness.coordinator.readiness() is False
    await lease.abort()
    assert harness.gate.mode.value == "SERVING"
    assert await harness.coordinator.readiness() is True


@pytest.mark.asyncio
async def test_unknown_knowledge_is_not_certified_by_dependency_health() -> None:
    gate = RuntimeGate()
    await gate.mark_serving()
    registry = build_health(gate, refresh_interval=60.0)

    snapshot = await registry.refresh()
    assert snapshot.knowledge_verified is False
    assert snapshot.hard_healthy is False
    assert snapshot.postgres is DependencyHealth.HEALTHY
    assert snapshot.milvus is DependencyHealth.HEALTHY
    assert snapshot.embedding is DependencyHealth.HEALTHY
    assert await registry.readiness() is False

    registry.set_knowledge_verified(True)
    assert (await registry.refresh()).hard_healthy is True


@pytest.mark.asyncio
async def test_health_snapshot_records_lifecycle_generation() -> None:
    gate = RuntimeGate()
    await gate.mark_serving()
    registry = build_health(gate, refresh_interval=0.0)

    serving = await registry.refresh()
    assert serving.lifecycle_generation == gate.lifecycle_generation
    lease = await gate.begin_maintenance()
    draining = await registry.refresh()

    assert draining.runtime is RuntimeMode.DRAINING
    assert draining.lifecycle_generation == gate.lifecycle_generation
    assert draining.lifecycle_generation > serving.lifecycle_generation
    await lease.abort()


@pytest.mark.asyncio
async def test_stale_probe_spanning_lifecycle_transition_is_not_current() -> None:
    gate = RuntimeGate()
    await gate.mark_serving()
    postgres = Probe()
    probe_started = asyncio.Event()
    probe_release = asyncio.Event()
    postgres.block_call(probe_started, probe_release)
    registry = build_health(gate, probes={"postgres": postgres}, refresh_interval=0.0)
    registry.set_knowledge_verified(True)

    refresh_task = asyncio.create_task(registry.refresh())
    await asyncio.wait_for(probe_started.wait(), timeout=1)
    await gate.begin_shutdown()
    probe_release.set()
    snapshot = await asyncio.wait_for(refresh_task, timeout=2)

    assert snapshot.runtime is RuntimeMode.SHUTTING_DOWN
    assert snapshot.lifecycle_generation != gate.lifecycle_generation


@pytest.mark.asyncio
async def test_gate_ignores_admission_from_stale_lifecycle_generation() -> None:
    gate = RuntimeGate()
    await gate.mark_serving()
    await gate.set_admission_ready(True)
    generation = gate.lifecycle_generation

    lease = await gate.begin_maintenance()
    assert gate.lifecycle_generation > generation
    await gate.set_admission_ready(True, lifecycle_generation=generation)
    with pytest.raises(RuntimeNotReadyError):
        await gate.acquire_query()
    await lease.abort()
    assert gate.mode.value == "SERVING"

    await gate.set_admission_ready(False, lifecycle_generation=generation)
    new_lease = await gate.acquire_query()
    await new_lease.release()

    await gate.begin_shutdown()
    with pytest.raises(RuntimeShuttingDownError):
        await gate.set_admission_ready(True, lifecycle_generation=generation)
    assert gate.mode.value == "SHUTTING_DOWN"


@pytest.mark.asyncio
async def test_stale_health_probe_cannot_reopen_admission_after_maintenance() -> None:
    postgres = Probe()
    query_probe_started = asyncio.Event()
    query_probe_release = asyncio.Event()
    postgres.on_call(_healthy)
    postgres.block_call(query_probe_started, query_probe_release)

    async def post_mutation_probe() -> None:
        raise RuntimeError("postgres lost during maintenance")

    postgres.on_call(post_mutation_probe)
    harness = build_harness(probes={"postgres": postgres}, refresh_interval=0.0)
    await harness.start()

    query_task = asyncio.create_task(
        harness.coordinator.run_query(request("session-p"))
    )
    await asyncio.wait_for(query_probe_started.wait(), timeout=1)
    maintenance_task = asyncio.create_task(harness.coordinator.run_maintenance())
    await pump_until(
        lambda: harness.gate.mode is RuntimeMode.SERVING and harness.sync.calls == 1,
        label="maintenance completed",
    )
    assert harness.gate.active_queries == 0

    query_probe_release.set()
    with pytest.raises(RuntimeNotReadyError):
        await asyncio.wait_for(query_task, timeout=2)
    result = await asyncio.wait_for(maintenance_task, timeout=2)

    assert result.serving_safety.value == "SAFE_TO_SERVE"
    assert harness.gate.mode.value == "SERVING"
    assert harness.gate.active_queries == 0
    assert harness.sync.calls == 1
    assert postgres.calls == 3
    with pytest.raises(RuntimeNotReadyError):
        await harness.gate.acquire_query()
    assert harness.retrieval.calls == 0


@pytest.mark.asyncio
async def test_unsafe_maintenance_outcome_is_not_overridden_by_healthy_probes() -> None:
    from runtime_harness import unsafe_result

    harness = build_harness()
    harness.sync.result = unsafe_result()
    await harness.start()
    await harness.coordinator.run_maintenance()

    assert harness.gate.mode.value == "BLOCKED"
    snapshot = await harness.coordinator.health()
    assert snapshot.postgres is DependencyHealth.HEALTHY
    assert snapshot.knowledge_verified is False
    assert snapshot.hard_healthy is False
    assert await harness.coordinator.readiness() is False
    with pytest.raises(RuntimeNotReadyError):
        await harness.coordinator.run_query(request("session-q"))
    assert harness.retrieval.calls == 0


@pytest.mark.asyncio
async def test_maintenance_health_refresh_is_tracked_until_settlement() -> None:
    postgres = Probe()
    probe_started = asyncio.Event()
    probe_release = asyncio.Event()
    postgres.on_call(_healthy)
    postgres.block_call(probe_started, probe_release)
    harness = build_harness(probes={"postgres": postgres})
    await harness.start()
    maintenance_task = asyncio.create_task(harness.coordinator.run_maintenance())
    await asyncio.wait_for(probe_started.wait(), timeout=1)
    assert maintenance_task in harness.tasks.snapshot(TaskKind.MAINTENANCE)
    assert harness.gate.active_queries == 0

    shutdown_task = asyncio.create_task(harness.coordinator.shutdown())
    await pump_until(lambda: harness.tasks.closed, label="task registry closed")
    assert not shutdown_task.done()
    assert harness.close.calls == 0

    probe_release.set()
    await asyncio.wait_for(maintenance_task, timeout=2)
    await asyncio.wait_for(shutdown_task, timeout=2)

    assert harness.close.calls == 1
    assert harness.ownership.released == 1
    assert harness.tasks.snapshot() == ()


@pytest.mark.asyncio
async def test_stale_probe_cannot_override_blocked_lifecycle() -> None:
    gate = RuntimeGate()
    await gate.mark_serving()
    await gate.set_admission_ready(True)
    generation = gate.lifecycle_generation
    lease = await gate.begin_maintenance()
    await lease.enter()
    await lease.complete(safe=False)
    assert gate.mode.value == "BLOCKED"

    await gate.set_admission_ready(True, lifecycle_generation=generation)
    with pytest.raises(RuntimeNotReadyError):
        await gate.acquire_query()
    assert gate._admission_ready is False
