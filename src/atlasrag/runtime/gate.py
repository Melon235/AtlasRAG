"""Atomic runtime mode, admission and maintenance lease gate."""

from __future__ import annotations

import asyncio
from enum import StrEnum
from uuid import uuid4

from atlasrag.domain.errors import (
    MaintenanceBusyError,
    MaintenanceDrainTimeoutError,
    RuntimeNotReadyError,
    RuntimeShuttingDownError,
)
from atlasrag.runtime.modes import RuntimeMode


class MaintenanceOrigin(StrEnum):
    SERVING = "SERVING"
    BLOCKED = "BLOCKED"


class QueryLease:
    def __init__(self, gate: RuntimeGate, trace_id: str) -> None:
        self._gate = gate
        self._released = False
        self.trace_id = trace_id

    @property
    def released(self) -> bool:
        return self._released

    async def release(self) -> None:
        await self._gate.release_query(self)


class MaintenanceLease:
    def __init__(self, gate: RuntimeGate, origin: MaintenanceOrigin) -> None:
        self._gate = gate
        self.origin = origin
        self._state = "DRAINING"
        self._closed = False

    def _assert_open(self) -> None:
        if self._closed:
            raise MaintenanceBusyError

    async def wait_for_drain(self, *, timeout: float) -> None:
        self._assert_open()
        await self._gate.wait_for_drain(self, timeout=timeout)

    async def enter(self) -> None:
        self._assert_open()
        await self._gate.enter_exclusive_maintenance(self)
        self._state = "MAINTENANCE"

    async def complete(self, *, safe: bool) -> None:
        self._assert_open()
        await self._gate.complete_maintenance(self, safe=safe)
        self._closed = True

    async def block(self) -> None:
        self._assert_open()
        await self._gate.block_from_maintenance(self)
        self._closed = True

    async def abort(self) -> None:
        self._assert_open()
        await self._gate.abort_maintenance(self)
        self._closed = True


class RuntimeGate:
    """One condition protects mode, admission and active query accounting."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._mode = RuntimeMode.STARTING
        self._active_queries = 0
        self._admission_ready = False
        self._maintenance: MaintenanceLease | None = None
        self._shutdown = False
        self._lifecycle_generation = 0
        self._admission_before_maintenance = False

    @property
    def mode(self) -> RuntimeMode:
        return self._mode

    @property
    def active_queries(self) -> int:
        return self._active_queries

    @property
    def lifecycle_generation(self) -> int:
        """Epoch of admission-authority transitions, sampled by health probes."""

        return self._lifecycle_generation

    def _advance_lifecycle(self) -> None:
        self._lifecycle_generation += 1

    def _check_not_shutdown(self) -> None:
        if self._shutdown or self._mode is RuntimeMode.SHUTTING_DOWN:
            raise RuntimeShuttingDownError

    async def set_admission_ready(
        self, ready: bool, *, lifecycle_generation: int | None = None
    ) -> None:
        """Apply an admission decision, ignoring decisions from older epochs."""

        async with self._condition:
            self._check_not_shutdown()
            if (
                lifecycle_generation is not None
                and lifecycle_generation != self._lifecycle_generation
            ):
                return
            self._admission_ready = ready
            self._condition.notify_all()

    async def mark_serving(self) -> None:
        async with self._condition:
            self._check_not_shutdown()
            if self._mode is not RuntimeMode.STARTING:
                raise RuntimeNotReadyError
            self._mode = RuntimeMode.SERVING
            self._admission_ready = False
            self._advance_lifecycle()
            self._condition.notify_all()

    async def mark_blocked(self) -> None:
        async with self._condition:
            if self._mode is RuntimeMode.SHUTTING_DOWN:
                return
            if self._mode is not RuntimeMode.BLOCKED:
                self._advance_lifecycle()
            self._mode = RuntimeMode.BLOCKED
            self._admission_ready = False
            self._maintenance = None
            self._condition.notify_all()

    async def acquire_query(self) -> QueryLease:
        async with self._condition:
            self._check_not_shutdown()
            if self._mode is not RuntimeMode.SERVING or not self._admission_ready:
                raise RuntimeNotReadyError
            self._active_queries += 1
            return QueryLease(self, uuid4().hex)

    async def release_query(self, lease: QueryLease) -> None:
        async with self._condition:
            if lease._released:
                return
            lease._released = True
            if self._active_queries > 0:
                self._active_queries -= 1
            self._condition.notify_all()

    async def begin_maintenance(self) -> MaintenanceLease:
        async with self._condition:
            self._check_not_shutdown()
            if self._mode is not RuntimeMode.SERVING:
                raise RuntimeNotReadyError
            if self._maintenance is not None:
                raise MaintenanceBusyError
            self._mode = RuntimeMode.DRAINING
            self._admission_before_maintenance = self._admission_ready
            self._admission_ready = False
            self._advance_lifecycle()
            lease = MaintenanceLease(self, MaintenanceOrigin.SERVING)
            self._maintenance = lease
            self._condition.notify_all()
            return lease

    async def begin_recovery(self) -> MaintenanceLease:
        async with self._condition:
            self._check_not_shutdown()
            if self._mode is not RuntimeMode.BLOCKED:
                raise RuntimeNotReadyError
            if self._maintenance is not None:
                raise MaintenanceBusyError
            self._mode = RuntimeMode.DRAINING
            self._admission_before_maintenance = self._admission_ready
            self._admission_ready = False
            self._advance_lifecycle()
            lease = MaintenanceLease(self, MaintenanceOrigin.BLOCKED)
            self._maintenance = lease
            self._condition.notify_all()
            return lease

    async def wait_for_drain(self, lease: MaintenanceLease, *, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        async def drained() -> None:
            async with self._condition:
                while self._active_queries != 0:
                    await self._condition.wait()

        try:
            await asyncio.wait_for(drained(), timeout=max(0.0, deadline - loop.time()))
        except TimeoutError as error:
            await self.abort_maintenance(lease)
            raise MaintenanceDrainTimeoutError from error

    async def enter_exclusive_maintenance(self, lease: MaintenanceLease) -> None:
        async with self._condition:
            self._check_not_shutdown()
            if self._maintenance is not lease or self._mode is not RuntimeMode.DRAINING:
                raise MaintenanceBusyError
            self._mode = RuntimeMode.MAINTENANCE
            self._advance_lifecycle()
            self._condition.notify_all()

    async def complete_maintenance(
        self, lease: MaintenanceLease, *, safe: bool
    ) -> None:
        async with self._condition:
            if self._mode is RuntimeMode.SHUTTING_DOWN:
                return
            if (
                self._maintenance is not lease
                or self._mode is not RuntimeMode.MAINTENANCE
            ):
                raise MaintenanceBusyError
            self._maintenance = None
            self._mode = RuntimeMode.SERVING if safe else RuntimeMode.BLOCKED
            self._admission_ready = False
            self._advance_lifecycle()
            self._condition.notify_all()

    async def block_from_maintenance(self, lease: MaintenanceLease) -> None:
        async with self._condition:
            if self._maintenance is not lease:
                return
            self._maintenance = None
            if self._mode is not RuntimeMode.SHUTTING_DOWN:
                self._mode = RuntimeMode.BLOCKED
                self._advance_lifecycle()
            self._admission_ready = False
            self._condition.notify_all()

    async def abort_maintenance(self, lease: MaintenanceLease) -> None:
        async with self._condition:
            if self._maintenance is not lease:
                return
            self._maintenance = None
            if self._mode is RuntimeMode.SHUTTING_DOWN:
                return
            if self._mode is not RuntimeMode.DRAINING:
                self._mode = RuntimeMode.BLOCKED
                self._admission_ready = False
                self._advance_lifecycle()
            elif lease.origin is MaintenanceOrigin.SERVING:
                # No mutation occurred; restore the pre-drain state exactly.
                self._mode = RuntimeMode.SERVING
                self._admission_ready = self._admission_before_maintenance
                self._advance_lifecycle()
            else:
                self._mode = RuntimeMode.BLOCKED
                self._admission_ready = False
                self._advance_lifecycle()
            self._condition.notify_all()

    async def begin_shutdown(self) -> None:
        async with self._condition:
            if self._mode is not RuntimeMode.SHUTTING_DOWN:
                self._advance_lifecycle()
            self._shutdown = True
            self._mode = RuntimeMode.SHUTTING_DOWN
            self._admission_ready = False
            self._condition.notify_all()
