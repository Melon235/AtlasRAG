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
