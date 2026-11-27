"""Exclusive maintenance and BLOCKED recovery coordination."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from atlasrag.runtime.cache_freshness import CacheFreshnessBarrier
from atlasrag.runtime.gate import MaintenanceLease, RuntimeGate
from atlasrag.services.sync.models import ServingSafety, SyncResult


class MaintenanceCoordinator:
    def __init__(
        self,
        *,
        gate: RuntimeGate,
        run_sync: Callable[[], Awaitable[SyncResult]],
        freshness: CacheFreshnessBarrier,
        drain_timeout_seconds: float,
    ) -> None:
        self._gate = gate
        self._run_sync = run_sync
        self._freshness = freshness
        self._drain_timeout = drain_timeout_seconds

    async def _finish(self, lease: MaintenanceLease, result: SyncResult) -> SyncResult:
        if result.serving_safety is not ServingSafety.SAFE_TO_SERVE:
            await lease.block()
            return result
        try:
            await self._freshness.enforce()
        except BaseException:
            await lease.block()
            raise
        await lease.complete(safe=True)
        return result

    async def _execute_with_lease(
        self,
        lease: MaintenanceLease,
        *,
        on_draining: Callable[[], None] | None,
    ) -> SyncResult:
        try:
            if on_draining is not None:
                on_draining()
            await lease.wait_for_drain(timeout=self._drain_timeout)
        except BaseException:
            await lease.abort()
            raise
        await lease.enter()
        try:
            result = await self._run_sync()
        except BaseException:
            if not lease._closed:
                await lease.block()
            raise
        return await self._finish(lease, result)

    async def run(self, *, on_draining: Callable[[], None] | None = None) -> SyncResult:
        lease = await self._gate.begin_maintenance()
        return await self._execute_with_lease(lease, on_draining=on_draining)

    async def recover_blocked(
        self, *, on_draining: Callable[[], None] | None = None
    ) -> SyncResult:
        lease = await self._gate.begin_recovery()
        return await self._execute_with_lease(lease, on_draining=on_draining)
