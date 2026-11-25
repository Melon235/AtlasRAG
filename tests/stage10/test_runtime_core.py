from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from atlasrag.domain.errors import (
    MaintenanceDrainTimeoutError,
    RuntimeNotReadyError,
    RuntimeOwnershipConflictError,
    RuntimeShuttingDownError,
)
from atlasrag.runtime.gate import MaintenanceOrigin, RuntimeGate
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.runtime.ownership import RuntimeOwnershipLock
from atlasrag.runtime.resources import ResourceGuard
from atlasrag.runtime.tasks import InFlightTaskRegistry, TaskKind


def test_ownership_second_acquirer_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "runtime.lock"
    first = RuntimeOwnershipLock(path)
    second = RuntimeOwnershipLock(path)
    first.acquire()
    with pytest.raises(RuntimeOwnershipConflictError):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


def test_ownership_release_is_idempotent(tmp_path: Path) -> None:
    lock = RuntimeOwnershipLock(tmp_path / "runtime.lock")
    lock.acquire()
    lock.release()
    lock.release()
    assert not lock.acquired


@pytest.mark.asyncio
async def test_gate_admission_and_exactly_once_release() -> None:
    gate = RuntimeGate()
    await gate.mark_serving()
    with pytest.raises(RuntimeNotReadyError):
        await gate.acquire_query()
    await gate.set_admission_ready(True)
    lease = await gate.acquire_query()
    assert gate.active_queries == 1
    await lease.release()
    await lease.release()
    assert gate.active_queries == 0


@pytest.mark.asyncio
async def test_gate_maintenance_drain_and_complete() -> None:
    gate = RuntimeGate()
    await gate.mark_serving()
    await gate.set_admission_ready(True)
    lease = await gate.acquire_query()
    maintenance = await gate.begin_maintenance()
    assert gate.mode is RuntimeMode.DRAINING
    drain = asyncio.create_task(maintenance.wait_for_drain(timeout=1.0))
    await asyncio.sleep(0)
    assert not drain.done()
    await lease.release()
    await drain
    await maintenance.enter()
    assert gate.mode.value == "MAINTENANCE"
    await maintenance.complete(safe=True)
    assert gate.mode.value == "SERVING"


@pytest.mark.asyncio
async def test_gate_drain_timeout_restores_serving_and_preserves_query() -> None:
    gate = RuntimeGate()
    await gate.mark_serving()
    await gate.set_admission_ready(True)
    lease = await gate.acquire_query()
    maintenance = await gate.begin_maintenance()
    with pytest.raises(MaintenanceDrainTimeoutError):
        await maintenance.wait_for_drain(timeout=0.0)
    assert gate.mode.value == "SERVING"
    assert gate.active_queries == 1
    await lease.release()


@pytest.mark.asyncio
async def test_gate_recovery_abort_returns_blocked() -> None:
    gate = RuntimeGate()
    await gate.mark_blocked()
    lease = await gate.begin_recovery()
    assert lease.origin is MaintenanceOrigin.BLOCKED
    await lease.abort()
    assert gate.mode is RuntimeMode.BLOCKED


@pytest.mark.asyncio
async def test_gate_shutdown_is_terminal() -> None:
    gate = RuntimeGate()
    await gate.mark_serving()
    await gate.set_admission_ready(True)
    await gate.begin_shutdown()
    assert gate.mode is RuntimeMode.SHUTTING_DOWN
    with pytest.raises(RuntimeShuttingDownError):
        await gate.acquire_query()
    await gate.begin_shutdown()
    assert gate.mode is RuntimeMode.SHUTTING_DOWN


@pytest.mark.asyncio
async def test_task_registry_tracks_and_settles() -> None:
    registry = InFlightTaskRegistry()

    async def work() -> None:
        await asyncio.sleep(0)

    task = asyncio.create_task(work())
    registry.register(task, TaskKind.QUERY)
    assert registry.snapshot(TaskKind.QUERY) == (task,)
    await task
    registry.unregister(task)
    assert registry.snapshot() == ()


@pytest.mark.asyncio
async def test_resource_guard_capacity_and_cancelled_waiter() -> None:
    guard = ResourceGuard(capacity=1)
    entered = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def holder() -> None:
        async with guard.slot():
            order.append("holder")
            entered.set()
            await release.wait()

    async def waiter() -> None:
        async with guard.slot():
            order.append("waiter")

    first = asyncio.create_task(holder())
    await entered.wait()
    second = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    release.set()
    await first
    assert order == ["holder"]


@pytest.mark.asyncio
async def test_guarded_mineru_uses_shared_guard_and_releases() -> None:
    from atlasrag.domain.ingestion.document import DocumentContainer
    from atlasrag.runtime.resources import GuardedMinerUParser

    entered = asyncio.Event()
    release = asyncio.Event()

    class Parser:
        async def parse(self, source: object, context: object) -> DocumentContainer:
            entered.set()
            await release.wait()
            return DocumentContainer.model_construct()

    parser = Parser()
    guarded = GuardedMinerUParser(parser, ResourceGuard(capacity=1))

    async def run() -> None:
        await guarded.parse(object(), object())  # type: ignore[arg-type]

    task = asyncio.create_task(run())
    await entered.wait()
    assert not task.done()
    release.set()
    await task


@pytest.mark.asyncio
async def test_maintenance_freshness_failure_blocks_runtime() -> None:
    from atlasrag.runtime.maintenance import MaintenanceCoordinator
    from atlasrag.services.sync.models import ServingSafety, SyncOutcome, SyncResult

    class Freshness:
        async def enforce(self) -> object:
            raise RuntimeError("barrier failed")

    async def sync() -> SyncResult:
        return SyncResult(
            outcome=SyncOutcome.SUCCESS,
            serving_safety=ServingSafety.SAFE_TO_SERVE,
        )

    gate = RuntimeGate()
    await gate.mark_serving()
    await gate.set_admission_ready(True)
    coordinator = MaintenanceCoordinator(
        gate=gate,
        run_sync=sync,
        freshness=Freshness(),  # type: ignore[arg-type]
        drain_timeout_seconds=1.0,
    )
    with pytest.raises(RuntimeError):
        await coordinator.run()
    assert gate.mode.value == "BLOCKED"


@pytest.mark.asyncio
async def test_coordinator_does_not_reacquire_preowned_lock() -> None:
    from atlasrag.runtime.coordinator import (
        ApplicationCoordinator,
        CoordinatorDependencies,
    )
    from atlasrag.runtime.health import HealthRegistry
    from atlasrag.services.sync.models import (
        GlobalSafetyResult,
        ReconciliationOutcome,
        ReconciliationResult,
        ServingSafety,
    )
    from atlasrag.services.sync.source_safety import SourceSafetyResult

    class Ownership:
        def __init__(self) -> None:
            self.released = False

        def acquire(self) -> None:
            raise AssertionError("must not reacquire")

        def release(self) -> None:
            self.released = True

    async def ok() -> None:
        return None

    gate = RuntimeGate()
    health = HealthRegistry(
        mode=lambda: gate.mode,
        postgres=ok,
        milvus=ok,
        redis=ok,
        embedding=ok,
        probe_timeout=1,
        refresh_interval=10,
    )

    class Freshness:
        async def recover_if_due(self) -> None:
            return None

        async def enforce(self) -> object:
            return "TRUSTED"

    class Maintenance:
        async def run(self) -> object:
            raise AssertionError

        async def recover_blocked(self) -> object:
            raise AssertionError

    async def reconcile() -> ReconciliationResult:
        return ReconciliationResult(
            outcome=ReconciliationOutcome.CLEAN, checked_documents=0
        )

    async def source() -> SourceSafetyResult:
        return SourceSafetyResult(safe=True)

    async def verify() -> GlobalSafetyResult:
        return GlobalSafetyResult(serving_safety=ServingSafety.SAFE_TO_SERVE)

    async def retrieval(request: object) -> object:
        raise AssertionError

    deps = CoordinatorDependencies(
        ownership=Ownership(),  # type: ignore[arg-type]
        gate=gate,
        tasks=InFlightTaskRegistry(),
        health=health,
        freshness=Freshness(),  # type: ignore[arg-type]
        maintenance=Maintenance(),  # type: ignore[arg-type]
        retrieval=retrieval,  # type: ignore[arg-type]
        reconcile=reconcile,
        source_safety=source,
        verify=verify,
        ownership_acquired=True,
    )
    coordinator = ApplicationCoordinator(deps)
    await coordinator._start()
    assert gate.mode.value == "SERVING"


@pytest.mark.asyncio
async def test_shutdown_unsettled_worker_retains_resources_and_raises() -> None:
    from atlasrag.domain.errors import RuntimeShutdownFailedError
    from atlasrag.runtime.coordinator import (
        ApplicationCoordinator,
        CoordinatorDependencies,
    )
    from atlasrag.runtime.health import HealthRegistry

    async def ok() -> None:
        return None

    gate = RuntimeGate()
    await gate.mark_serving()
    health = HealthRegistry(
        mode=lambda: gate.mode,
        postgres=ok,
        milvus=ok,
        redis=ok,
        embedding=ok,
    )
    registry = InFlightTaskRegistry()
    closed = False
    released = False

    class Ownership:
        def acquire(self) -> None:
            return None

        def release(self) -> None:
            nonlocal released
            released = True

    async def close() -> None:
        nonlocal closed
        closed = True

    class Freshness:
        async def recover_if_due(self) -> None:
            return None

    class Maintenance:
        async def run(self) -> object:
            raise AssertionError

        async def recover_blocked(self) -> object:
            raise AssertionError

    async def retrieval(request: object) -> object:
        raise AssertionError

    async def reconcile() -> object:
        raise AssertionError

    async def source() -> object:
        raise AssertionError

    async def verify() -> object:
        raise AssertionError

    deps = CoordinatorDependencies(
        ownership=Ownership(),  # type: ignore[arg-type]
        gate=gate,
        tasks=registry,
        health=health,
        freshness=Freshness(),  # type: ignore[arg-type]
        maintenance=Maintenance(),  # type: ignore[arg-type]
        retrieval=retrieval,  # type: ignore[arg-type]
        reconcile=reconcile,  # type: ignore[arg-type]
        source_safety=source,  # type: ignore[arg-type]
        verify=verify,  # type: ignore[arg-type]
        close_resources=close,
        shutdown_timeout_seconds=0.05,
        ownership_acquired=True,
    )
    coordinator = ApplicationCoordinator(deps)

    async def stubborn() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Deliberately settle after the shutdown deadline so shutdown
            # must report failure while retaining resources.
            await asyncio.sleep(0.2)

    task = asyncio.create_task(stubborn())
    registry.register(task, TaskKind.QUERY)
    with pytest.raises(RuntimeShutdownFailedError):
        await coordinator.shutdown()
    assert not closed
    assert not released
    assert gate.mode.value == "SHUTTING_DOWN"
    await task


@pytest.mark.asyncio
async def test_registration_failure_releases_query_lease() -> None:
    from atlasrag.runtime.coordinator import (
        ApplicationCoordinator,
        CoordinatorDependencies,
    )

    class Health:
        async def refresh_if_stale(self) -> object:
            class Snapshot:
                generation = 1
                hard_healthy = True

            return Snapshot()

    class Ownership:
        def acquire(self) -> None:
            return None

        def release(self) -> None:
            return None

    gate = RuntimeGate()
    await gate.mark_serving()
    await gate.set_admission_ready(True)
    registry = InFlightTaskRegistry()
    await registry.close()
    deps = CoordinatorDependencies(
        ownership=Ownership(),  # type: ignore[arg-type]
        gate=gate,
        tasks=registry,
        health=Health(),  # type: ignore[arg-type]
        freshness=object(),  # type: ignore[arg-type]
        maintenance=object(),  # type: ignore[arg-type]
        retrieval=lambda request: None,  # type: ignore[arg-type,return-value]
        reconcile=lambda: None,  # type: ignore[arg-type,return-value]
        source_safety=lambda: None,  # type: ignore[arg-type,return-value]
        verify=lambda: None,  # type: ignore[arg-type,return-value]
        ownership_acquired=True,
    )
    coordinator = ApplicationCoordinator(deps)
    with pytest.raises(RuntimeError):
        await coordinator.run_query(object())  # type: ignore[arg-type]
    assert gate.active_queries == 0
