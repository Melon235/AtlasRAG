"""In-process ApplicationCoordinator boundary for AtlasRAG."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import uuid4

from atlasrag.domain.errors import (
    RuntimeBootstrapFailedError,
    RuntimeShutdownFailedError,
    RuntimeShuttingDownError,
)
from atlasrag.domain.observation import observation_invocation
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse
from atlasrag.runtime.cache_freshness import CacheFreshnessBarrier
from atlasrag.runtime.gate import RuntimeGate
from atlasrag.runtime.health import HealthRegistry, HealthSnapshot
from atlasrag.runtime.maintenance import MaintenanceCoordinator
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.runtime.ownership import RuntimeOwnershipLock
from atlasrag.runtime.tasks import InFlightTaskRegistry, TaskKind
from atlasrag.services.sync.models import (
    GlobalSafetyResult,
    ReconciliationOutcome,
    ReconciliationResult,
    ServingSafety,
    SyncResult,
)
from atlasrag.services.sync.source_safety import SourceSafetyResult


@dataclass(frozen=True)
class QueryResponseEnvelope:
    trace_id: str
    response: FinalResponse


@dataclass
class CoordinatorDependencies:
    ownership: RuntimeOwnershipLock
    gate: RuntimeGate
    tasks: InFlightTaskRegistry
    health: HealthRegistry
    freshness: CacheFreshnessBarrier
    maintenance: MaintenanceCoordinator
    retrieval: Callable[[UserTurnRequest], Awaitable[FinalResponse]]
    reconcile: Callable[[], Awaitable[ReconciliationResult]]
    source_safety: Callable[[], Awaitable[SourceSafetyResult]]
    verify: Callable[[], Awaitable[GlobalSafetyResult]]
    open_resources: Callable[[], Awaitable[None]] = field(default=lambda: _noop())
    close_resources: Callable[[], Awaitable[None]] = field(default=lambda: _noop())
    shutdown_timeout_seconds: float = 30.0
    cache_recovery_inside_lease: bool = True
    ownership_acquired: bool = False


async def _noop() -> None:
    return None


class ApplicationCoordinator:
    def __init__(self, dependencies: CoordinatorDependencies) -> None:
        self._deps = dependencies
        self._started = False
        self._shutdown_done = False
        self._last_health_generation = 0
        self._ownership_acquired_here = False
        self._shutdown_lock = asyncio.Lock()

    @property
    def gate(self) -> RuntimeGate:
        return self._deps.gate

    @property
    def tasks(self) -> InFlightTaskRegistry:
        return self._deps.tasks

    def _begin_operation(self, kind: TaskKind) -> asyncio.Task[object]:
        """Track the current task as owning runtime resources until settlement."""

        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("runtime operation requires an asyncio task")
        try:
            self._deps.tasks.register(current, kind)
        except RuntimeError:
            if self._deps.gate.mode is RuntimeMode.SHUTTING_DOWN:
                raise RuntimeShuttingDownError from None
            raise
        return current

    @classmethod
    async def start(cls, deps: CoordinatorDependencies) -> ApplicationCoordinator:
        coordinator = cls(deps)
        await coordinator._start()
        return coordinator

    async def _start(self) -> None:
        try:
            if not self._deps.ownership_acquired:
                self._deps.ownership.acquire()
                self._ownership_acquired_here = True
            await self._deps.open_resources()
            reconciliation = await self._deps.reconcile()
            if reconciliation.outcome is ReconciliationOutcome.BLOCKED:
                await self._deps.gate.mark_blocked()
                self._started = True
                return
            source = await self._deps.source_safety()
            if not source.safe:
                await self._deps.gate.mark_blocked()
                self._started = True
                return
            safety = await self._deps.verify()
            if safety.serving_safety is not ServingSafety.SAFE_TO_SERVE:
                await self._deps.gate.mark_blocked()
                self._started = True
                return
            await self._deps.freshness.enforce()
            self._deps.health.set_knowledge_verified(True)
            await self._deps.gate.mark_serving()
            snapshot = await self._deps.health.refresh()
            await self._deps.gate.set_admission_ready(
                snapshot.hard_healthy,
                lifecycle_generation=snapshot.lifecycle_generation,
            )
            self._last_health_generation = snapshot.generation
            self._started = True
        except BaseException as error:
            try:
                await self._deps.close_resources()
            finally:
                if self._ownership_acquired_here:
                    self._deps.ownership.release()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise RuntimeBootstrapFailedError from error

    async def run_query(self, request: UserTurnRequest) -> QueryResponseEnvelope:
        current = self._begin_operation(TaskKind.HEALTH)
        try:
            snapshot = await self._deps.health.refresh_if_stale()
            if snapshot.generation > self._last_health_generation:
                self._last_health_generation = snapshot.generation
                if snapshot.runtime is RuntimeMode.SERVING:
                    await self._deps.gate.set_admission_ready(
                        snapshot.hard_healthy,
                        lifecycle_generation=snapshot.lifecycle_generation,
                    )
        finally:
            self._deps.tasks.unregister(current)
        lease = await self._deps.gate.acquire_query()
        try:
            current = self._begin_operation(TaskKind.QUERY)
        except BaseException:
            await lease.release()
            raise
        invocation = uuid4().hex
        try:
            with observation_invocation(invocation):
                if self._deps.cache_recovery_inside_lease:
                    await self._deps.freshness.recover_if_due()
                response = await self._deps.retrieval(request)
                return QueryResponseEnvelope(trace_id=invocation, response=response)
        finally:
            try:
                await lease.release()
            finally:
                self._deps.tasks.unregister(current)

    async def run_maintenance(self) -> SyncResult:
        current = self._begin_operation(TaskKind.MAINTENANCE)
        try:
            result = await self._deps.maintenance.run()
            await self._refresh_admission_after_maintenance()
            return result
        finally:
            self._deps.tasks.unregister(current)

    async def recover(self) -> SyncResult:
        current = self._begin_operation(TaskKind.MAINTENANCE)
        try:
            result = await self._deps.maintenance.recover_blocked()
            await self._refresh_admission_after_maintenance()
            return result
        finally:
            self._deps.tasks.unregister(current)

    async def _refresh_admission_after_maintenance(self) -> None:
        snapshot = await self._deps.health.refresh()
        self._last_health_generation = snapshot.generation
        if snapshot.runtime is RuntimeMode.SERVING:
            await self._deps.gate.set_admission_ready(
                snapshot.hard_healthy,
                lifecycle_generation=snapshot.lifecycle_generation,
            )

    async def health(self) -> HealthSnapshot:
        current = self._begin_operation(TaskKind.HEALTH)
        try:
            return await self._deps.health.refresh()
        finally:
            self._deps.tasks.unregister(current)

    async def readiness(self) -> bool:
        current = self._begin_operation(TaskKind.HEALTH)
        try:
            return await self._deps.health.readiness()
        finally:
            self._deps.tasks.unregister(current)

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if self._shutdown_done:
                return
            await self._deps.gate.begin_shutdown()
            await self._deps.tasks.close()
            settled = await self._deps.tasks.wait_empty(
                timeout=self._deps.shutdown_timeout_seconds
            )
            if not settled:
                self._deps.tasks.cancel_all()
                remaining = self._deps.tasks.snapshot()
                if remaining:
                    _, pending = await asyncio.wait(
                        remaining,
                        timeout=self._deps.shutdown_timeout_seconds,
                    )
                    if pending:
                        raise RuntimeShutdownFailedError
                settled = await self._deps.tasks.wait_empty(timeout=0.0)
            if not settled:
                raise RuntimeShutdownFailedError
            await self._deps.close_resources()
            self._deps.ownership.release()
            self._shutdown_done = True
