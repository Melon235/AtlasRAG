"""Exclusive maintenance and BLOCKED recovery coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from atlasrag.domain.errors import MaintenanceDrainTimeoutError
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
        await self._freshness.enforce()
        await lease.complete(safe=True)
        return result

    async def run(self) -> SyncResult:
        lease = await self._gate.begin_maintenance()
        try:
            await lease.wait_for_drain(timeout=self._drain_timeout)
        except (MaintenanceDrainTimeoutError, asyncio.CancelledError):
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

    async def recover_blocked(self) -> SyncResult:
        lease = await self._gate.begin_recovery()
        try:
            await lease.wait_for_drain(timeout=self._drain_timeout)
        except (MaintenanceDrainTimeoutError, asyncio.CancelledError):
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
