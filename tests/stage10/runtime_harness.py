"""Deterministic application-runtime harness for Stage 10-A A-R2 evidence.

The harness keeps the real RuntimeGate, InFlightTaskRegistry, HealthRegistry,
MaintenanceCoordinator and ApplicationCoordinator boundaries. Only technical
providers, persistence and business callbacks are fakes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse
from atlasrag.runtime.coordinator import ApplicationCoordinator, CoordinatorDependencies
from atlasrag.runtime.gate import RuntimeGate
from atlasrag.runtime.health import HealthRegistry, HealthSnapshot
from atlasrag.runtime.maintenance import MaintenanceCoordinator
from atlasrag.runtime.tasks import InFlightTaskRegistry
from atlasrag.services.sync.models import (
    GlobalSafetyResult,
    ReconciliationOutcome,
    ReconciliationResult,
    ServingSafety,
    SyncOutcome,
    SyncResult,
)
from atlasrag.services.sync.source_safety import SourceSafetyResult

ProbeHandler = Callable[[], Awaitable[None]]
Hook = Callable[[], None]


def request(session_id: str = "session", query: str = "question") -> UserTurnRequest:
    return UserTurnRequest(session_id=session_id, query=query)


def response(text: str = "answer") -> FinalResponse:
    return FinalResponse(answer_text=text, citations=())


def safe_result() -> SyncResult:
    return SyncResult(
        outcome=SyncOutcome.SUCCESS, serving_safety=ServingSafety.SAFE_TO_SERVE
    )


def unsafe_result() -> SyncResult:
    return SyncResult(
        outcome=SyncOutcome.FAILED,
        serving_safety=ServingSafety.UNSAFE_TO_SERVE,
        unsafe_reasons=("KNOWLEDGE_UNSAFE",),
        blocked_code="KNOWLEDGE_UNSAFE",
    )


async def pump_until(
    predicate: Callable[[], bool], *, timeout: float = 2.0, label: str = "condition"
) -> None:
    """Yield cooperatively until a plainly observable state is reached."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"{label} was not reached within {timeout}s")
        await asyncio.sleep(0)


class Probe:
    """A probe whose per-call behaviour is scripted by the test."""

    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self._handlers: list[ProbeHandler] = []
        self.default_error: BaseException | None = None

    def on_call(self, handler: ProbeHandler) -> None:
        self._handlers.append(handler)

    def block_call(self, started: asyncio.Event, release: asyncio.Event) -> None:
        async def handler() -> None:
            started.set()
            await release.wait()

        self.on_call(handler)

    def fail_call(self, error: BaseException) -> None:
        async def handler() -> None:
            raise error

        self.on_call(handler)

    async def __call__(self) -> None:
        index = self.calls
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if index < len(self._handlers):
                await self._handlers[index]()
            elif self.default_error is not None:
                raise self.default_error
        finally:
            self.active -= 1


async def healthy() -> None:
    return None


def build_health(
    gate: RuntimeGate,
    *,
    probes: dict[str, Probe] | None = None,
    probe_timeout: float = 1.0,
    refresh_interval: float = 60.0,
    bind_lifecycle: bool = True,
) -> HealthRegistry:
    named = probes or {}

    def probe(name: str) -> ProbeHandler:
        return named.get(name, healthy)

    return HealthRegistry(
        mode=lambda: gate.mode,
        postgres=probe("postgres"),
        milvus=probe("milvus"),
        redis=probe("redis"),
        embedding=probe("embedding"),
        searxng=None,
        reranker=None,
        probe_timeout=probe_timeout,
        refresh_interval=refresh_interval,
        lifecycle_generation=(
            (lambda: gate.lifecycle_generation) if bind_lifecycle else None
        ),
    )


class RecordingOwnership:
    def __init__(self, log: list[str]) -> None:
        self.acquired = 0
        self.released = 0
        self._log = log

    def acquire(self) -> None:
        self.acquired += 1
        self._log.append("acquire")

    def release(self) -> None:
        self.released += 1
        self._log.append("release")


class CloseTracker:
    def __init__(self, log: list[str]) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.hold: asyncio.Event | None = None
        self.error: BaseException | None = None
        self.on_close: Hook | None = None
        self._log = log

    async def __call__(self) -> None:
        self.calls += 1
        self._log.append("close")
        self.started.set()
        try:
            if self.hold is not None:
                await self.hold.wait()
        finally:
            if self.on_close is not None:
                self.on_close()
        if self.error is not None:
            raise self.error


class FakeFreshness:
    def __init__(self) -> None:
        self.recover_calls = 0
        self.enforce_calls = 0
        self.recover_started = asyncio.Event()
        self.recover_hold: asyncio.Event | None = None
        self.enforce_error: BaseException | None = None

    async def recover_if_due(self) -> None:
        self.recover_calls += 1
        self.recover_started.set()
        if self.recover_hold is not None:
            await self.recover_hold.wait()
        return None

    async def enforce(self) -> None:
        self.enforce_calls += 1
        if self.enforce_error is not None:
            raise self.enforce_error
        return None


class FakeSync:
    def __init__(self, *, result: SyncResult | None = None) -> None:
        self.calls = 0
        self.result = safe_result() if result is None else result
        self.error: BaseException | None = None
        self.hold = False
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.cancel_seen = 0
        self.on_call: ProbeHandler | None = None

    async def __call__(self) -> SyncResult:
        self.calls += 1
        self.started.set()
        if self.on_call is not None:
            await self.on_call()
        if self.hold:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancel_seen += 1
                raise
        if self.error is not None:
            raise self.error
        return self.result


class Retrieval:
    """Controlled technical retrieval boundary (also used as a cache-hit fake)."""

    def __init__(self, gate: RuntimeGate, *, text: str = "answer") -> None:
        self.calls = 0
        self.observed_active: list[int] = []
        self.started = asyncio.Event()
        self.settle_gate: asyncio.Event | None = None
        self.finalize_gate: asyncio.Event | None = None
        self.hold = False
        self.release = asyncio.Event()
        self.cancel_seen = 0
        self.settled = asyncio.Event()
        self.on_call: ProbeHandler | None = None
        self._gate = gate
        self._text = text

    async def __call__(self, turn: UserTurnRequest) -> FinalResponse:
        self.calls += 1
        self.observed_active.append(self._gate.active_queries)
        self.started.set()
        if self.on_call is not None:
            await self.on_call()
        try:
            if self.finalize_gate is not None:
                await self.finalize_gate.wait()
            if self.hold:
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_seen += 1
            if self.settle_gate is not None:
                await self.settle_gate.wait()
            raise
        self.settled.set()
        return response(self._text)


class CacheHitRetrieval(Retrieval):
    """A cached FinalResponse boundary that performs no RAG computation."""


class VerifyStub:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> GlobalSafetyResult:
        self.calls += 1
        return GlobalSafetyResult(serving_safety=ServingSafety.SAFE_TO_SERVE)


@dataclass
class Harness:
    gate: RuntimeGate
    tasks: InFlightTaskRegistry
    health: HealthRegistry
    coordinator: ApplicationCoordinator
    ownership: RecordingOwnership
    close: CloseTracker
    retrieval: Retrieval
    sync: FakeSync
    freshness: FakeFreshness
    verify: VerifyStub
    log: list[str] = field(default_factory=list)

    async def start(self) -> None:
        """Run the real coordinator bootstrap over fake technical boundaries."""

        await self.coordinator._start()

    async def bring_serving(self) -> HealthSnapshot:
        """Reach SERVING without the bootstrap path when a test needs control."""

        await self.gate.mark_serving()
        self.health.set_knowledge_verified(True)
        snapshot = await self.health.refresh()
        await self.gate.set_admission_ready(
            snapshot.hard_healthy,
            lifecycle_generation=snapshot.lifecycle_generation,
        )
        return snapshot


def build_harness(
    *,
    gate: RuntimeGate | None = None,
    probes: dict[str, Probe] | None = None,
    health: HealthRegistry | None = None,
    tasks: InFlightTaskRegistry | None = None,
    sync: FakeSync | None = None,
    freshness: FakeFreshness | None = None,
    retrieval: Retrieval | None = None,
    shutdown_timeout: float = 1.0,
    drain_timeout: float = 1.0,
    refresh_interval: float = 60.0,
    bind_lifecycle: bool = True,
    cache_recovery_inside_lease: bool = True,
) -> Harness:
    gate = RuntimeGate() if gate is None else gate
    tasks = InFlightTaskRegistry() if tasks is None else tasks
    health = (
        build_health(
            gate,
            probes=probes,
            refresh_interval=refresh_interval,
            bind_lifecycle=bind_lifecycle,
        )
        if health is None
        else health
    )
    retrieval = Retrieval(gate) if retrieval is None else retrieval
    sync = FakeSync() if sync is None else sync
    freshness = FakeFreshness() if freshness is None else freshness
    log: list[str] = []
    ownership = RecordingOwnership(log)
    close = CloseTracker(log)
    maintenance = MaintenanceCoordinator(
        gate=gate,
        run_sync=sync,
        freshness=freshness,  # type: ignore[arg-type]
        drain_timeout_seconds=drain_timeout,
    )

    async def reconcile() -> ReconciliationResult:
        return ReconciliationResult(
            outcome=ReconciliationOutcome.CLEAN, checked_documents=0
        )

    async def source_safety() -> SourceSafetyResult:
        return SourceSafetyResult(safe=True)

    verify = VerifyStub()

    deps = CoordinatorDependencies(
        ownership=ownership,  # type: ignore[arg-type]
        gate=gate,
        tasks=tasks,
        health=health,
        freshness=freshness,  # type: ignore[arg-type]
        maintenance=maintenance,
        retrieval=retrieval,
        reconcile=reconcile,
        source_safety=source_safety,
        verify=verify,
        close_resources=close,
        shutdown_timeout_seconds=shutdown_timeout,
        cache_recovery_inside_lease=cache_recovery_inside_lease,
        ownership_acquired=True,
    )
    return Harness(
        gate=gate,
        tasks=tasks,
        health=health,
        coordinator=ApplicationCoordinator(deps),
        ownership=ownership,
        close=close,
        retrieval=retrieval,
        sync=sync,
        freshness=freshness,
        verify=verify,
        log=log,
    )
