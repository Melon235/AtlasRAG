"""In-process ApplicationCoordinator boundary for AtlasRAG."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import uuid4

from atlasrag.domain.errors import (
    RuntimeBootstrapFailedError,
    RuntimeShutdownFailedError,
)
from atlasrag.domain.observation import observation_invocation
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse
from atlasrag.runtime.cache_freshness import CacheFreshnessBarrier
from atlasrag.runtime.gate import RuntimeGate
from atlasrag.runtime.health import HealthRegistry
from atlasrag.runtime.maintenance import MaintenanceCoordinator
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


async def _noop() -> None:
    return None


class ApplicationCoordinator:
    def __init__(self, dependencies: CoordinatorDependencies) -> None:
        self._deps = dependencies
        self._started = False
        self._shutdown_done = False
        self._last_health_generation = 0

    @property
    def gate(self) -> RuntimeGate:
        return self._deps.gate

    @property
    def tasks(self) -> InFlightTaskRegistry:
        return self._deps.tasks

    @classmethod
    async def start(cls, deps: CoordinatorDependencies) -> ApplicationCoordinator:
        coordinator = cls(deps)
        await coordinator._start()
        return coordinator

    async def _start(self) -> None:
        try:
            self._deps.ownership.acquire()
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
            await self._deps.gate.set_admission_ready(snapshot.hard_healthy)
            self._last_health_generation = snapshot.generation
            self._started = True
        except BaseException as error:
            try:
                await self._deps.close_resources()
            finally:
                self._deps.ownership.release()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise RuntimeBootstrapFailedError from error

    async def run_query(self, request: UserTurnRequest) -> QueryResponseEnvelope:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("query requires an asyncio task")
        self._deps.tasks.register(current, TaskKind.HEALTH)
        try:
            snapshot = await self._deps.health.refresh_if_stale()
            if snapshot.generation > self._last_health_generation:
                self._last_health_generation = snapshot.generation
                await self._deps.gate.set_admission_ready(snapshot.hard_healthy)
        finally:
            self._deps.tasks.unregister(current)
        lease = await self._deps.gate.acquire_query()
        self._deps.tasks.register(current, TaskKind.QUERY)
        invocation = uuid4().hex
        try:
            with observation_invocation(invocation):
                if self._deps.cache_recovery_inside_lease:
                    await self._deps.freshness.recover_if_due()
                response = await self._deps.retrieval(request)
                return QueryResponseEnvelope(trace_id=invocation, response=response)
        finally:
            self._deps.tasks.unregister(current)
            await lease.release()

    async def run_maintenance(self) -> SyncResult:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("maintenance requires an asyncio task")
        self._deps.tasks.register(current, TaskKind.MAINTENANCE)
        try:
            result = await self._deps.maintenance.run()
            await self._refresh_admission_after_maintenance()
            return result
        finally:
            self._deps.tasks.unregister(current)

    async def recover(self) -> SyncResult:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("recovery requires an asyncio task")
        self._deps.tasks.register(current, TaskKind.MAINTENANCE)
        try:
            result = await self._deps.maintenance.recover_blocked()
            await self._refresh_admission_after_maintenance()
            return result
        finally:
            self._deps.tasks.unregister(current)

    async def _refresh_admission_after_maintenance(self) -> None:
        snapshot = await self._deps.health.refresh()
        self._last_health_generation = snapshot.generation
        if snapshot.runtime.value == "SERVING":
            await self._deps.gate.set_admission_ready(snapshot.hard_healthy)

    async def health(self) -> object:
        return await self._deps.health.refresh()

    async def readiness(self) -> bool:
        return await self._deps.health.readiness()

    async def shutdown(self) -> None:
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
                await asyncio.gather(*remaining, return_exceptions=True)
            settled = await self._deps.tasks.wait_empty(timeout=1.0)
        if not settled:
            raise RuntimeShutdownFailedError
        try:
            await self._deps.close_resources()
        finally:
            self._deps.ownership.release()
            self._shutdown_done = True
