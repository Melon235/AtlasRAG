"""Framework-independent application service boundary over ApplicationCoordinator."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from atlasrag.config.fingerprint import pipeline_fingerprint, runtime_fingerprint
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import (
    DependencyTimeoutError,
    ErrorCode,
    ResourceExhaustedError,
    RuntimeShuttingDownError,
)
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.runtime.coordinator import ApplicationCoordinator, QueryResponseEnvelope
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.server.config import ServerConfig
from atlasrag.server.errors import ServerHttpError, TransportErrorCode
from atlasrag.server.schemas import (
    ClientKind,
    CreateSessionRequest,
    DependencyStatusMap,
    LivenessResponse,
    LivenessStatus,
    QueryRequest,
    QueryResponse,
    ReadinessResponse,
    RuntimeStatusResponse,
    SessionResponse,
)
from atlasrag.server.sessions import SessionLease, SessionRegistry


def serialize_bounded_response(model: BaseModel, *, max_bytes: int) -> bytes:
    """Serialize a response DTO and fail closed if it exceeds max_bytes."""

    encoded = model.model_dump_json().encode("utf-8")
    if len(encoded) > max_bytes:
        raise ServerHttpError(
            ErrorCode.INVARIANT_VIOLATION,
            "Serialized response exceeds maximum allowed size.",
        )
    return encoded


class DisconnectSignal:
    """Framework-independent disconnect signal triggered by transport receive watcher."""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.reason: str | None = None

    def trigger(self, reason: str = "client_disconnect") -> None:
        if self.reason is None:
            self.reason = reason
        self.event.set()


MonitorFactory = Callable[[DisconnectSignal], asyncio.Task[None]]


async def _settle_task_no_raise(
    task: asyncio.Task[Any] | None,
    *,
    cancel: bool,
    already_cancelled: bool = False,
) -> tuple[bool, bool]:
    """Wait for actual task settlement via asyncio.wait, sending at most one cancel."""

    if task is None:
        return False, already_cancelled
    outer_cancelled = False
    cancel_sent = already_cancelled
    if cancel and not cancel_sent and not task.done():
        task.cancel()
        cancel_sent = True
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError:
            outer_cancelled = True
            if not cancel_sent and not task.done():
                task.cancel()
                cancel_sent = True
    if not task.cancelled():
        try:
            task.exception()
        except BaseException:
            pass
    return outer_cancelled, cancel_sent


async def _release_session_shielded(
    sessions: SessionRegistry, lease: SessionLease | None
) -> bool:
    if lease is None:
        return False
    task: asyncio.Task[None] = asyncio.create_task(sessions.release(lease))
    outer_cancelled = False
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError:
            outer_cancelled = True
    if not task.cancelled():
        try:
            task.exception()
        except BaseException:
            pass
    return outer_cancelled


class QuerySupervisor:
    """Framework-independent query task supervisor shared across HTTP and SSE."""

    def __init__(
        self,
        *,
        coordinator: ApplicationCoordinator,
        sessions: SessionRegistry,
        server_config: ServerConfig,
    ) -> None:
        self._coordinator = coordinator
        self._sessions = sessions
        self._server_config = server_config
        self._in_flight_queries = 0

    @property
    def in_flight_queries(self) -> int:
        return self._in_flight_queries

    async def execute(
        self,
        request: QueryRequest,
        *,
        client_kind: ClientKind,
        on_admitted: Callable[[str], None] | None = None,
        monitor_factory: MonitorFactory | None = None,
        disconnect_signal: DisconnectSignal | None = None,
    ) -> QueryResponseEnvelope:
        if len(request.query) > self._server_config.max_query_chars:
            raise ServerHttpError(
                TransportErrorCode.INVALID_REQUEST,
                "Query exceeds configured maximum character length.",
                details=("query: max_length",),
            )
        user_turn = UserTurnRequest(
            session_id=request.session_id,
            query=request.query,
            filters=request.filters,
        )
        initial_mode: RuntimeMode = self._coordinator.gate.mode
        if initial_mode is RuntimeMode.SHUTTING_DOWN:
            raise RuntimeShuttingDownError

        session_lease: SessionLease | None = None
        slot_acquired = False
        monitor_task: asyncio.Task[None] | None = None
        disconnect_wait_task: asyncio.Task[bool] | None = None
        query_task: asyncio.Task[QueryResponseEnvelope] | None = None
        cancel_requested = False
        timed_out = False
        outer_cancelled = False

        try:
            session_lease = await self._sessions.acquire(
                request.session_id, client_kind=client_kind
            )
            if self._in_flight_queries >= self._server_config.max_concurrent_queries:
                raise ResourceExhaustedError
            self._in_flight_queries += 1
            slot_acquired = True

            post_acquire_mode: RuntimeMode = self._coordinator.gate.mode
            if post_acquire_mode is RuntimeMode.SHUTTING_DOWN:
                raise RuntimeShuttingDownError

            signal = (
                DisconnectSignal() if disconnect_signal is None else disconnect_signal
            )
            if monitor_factory is not None:
                monitor_task = monitor_factory(signal)
            disconnect_wait_task = asyncio.create_task(signal.event.wait())
            query_task = asyncio.create_task(
                self._coordinator.run_query(user_turn, on_admitted=on_admitted)
            )

            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._server_config.query_timeout_seconds
            wait_set: set[asyncio.Task[Any]] = {query_task, disconnect_wait_task}

            while not query_task.done():
                remaining = deadline - loop.time()
                if remaining <= 0.0:
                    timed_out = True
                    if not cancel_requested:
                        cancel_requested = True
                        query_task.cancel()
                    break
                pending = {task for task in wait_set if not task.done()}
                if not pending:
                    break
                try:
                    done, _ = await asyncio.wait(
                        pending,
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except asyncio.CancelledError:
                    outer_cancelled = True
                    if not cancel_requested:
                        cancel_requested = True
                        query_task.cancel()
                    break
                if query_task.done():
                    break
                if disconnect_wait_task in done:
                    if not cancel_requested:
                        cancel_requested = True
                        query_task.cancel()
                    break
                if not done:
                    timed_out = True
                    if not cancel_requested:
                        cancel_requested = True
                        query_task.cancel()
                    break

            # Wait for ACTUAL settlement even under repeated outer cancellation.
            shield_cancel, cancel_requested = await _settle_task_no_raise(
                query_task,
                cancel=False,
                already_cancelled=cancel_requested,
            )
            if shield_cancel:
                outer_cancelled = True

            if outer_cancelled:
                raise asyncio.CancelledError
            if signal.event.is_set():
                raise ServerHttpError(TransportErrorCode.REQUEST_CANCELLED)
            if timed_out:
                raise DependencyTimeoutError
            if query_task.cancelled():
                cancelled_mode: RuntimeMode = self._coordinator.gate.mode
                if cancelled_mode is RuntimeMode.SHUTTING_DOWN:
                    raise RuntimeShuttingDownError
                raise ServerHttpError(TransportErrorCode.REQUEST_CANCELLED)
            query_exception = query_task.exception()
            if query_exception is not None:
                raise query_exception
            return query_task.result()
        finally:
            cleanup_cancelled = False
            if query_task is not None and not query_task.done():
                extra_cancel, cancel_requested = await _settle_task_no_raise(
                    query_task,
                    cancel=True,
                    already_cancelled=cancel_requested,
                )
                if extra_cancel:
                    cleanup_cancelled = True
            wait_cancel, _ = await _settle_task_no_raise(
                disconnect_wait_task, cancel=True
            )
            if wait_cancel:
                cleanup_cancelled = True
            mon_cancel, _ = await _settle_task_no_raise(monitor_task, cancel=True)
            if mon_cancel:
                cleanup_cancelled = True
            if slot_acquired:
                self._in_flight_queries = max(0, self._in_flight_queries - 1)
            if await _release_session_shielded(self._sessions, session_lease):
                cleanup_cancelled = True
            if cleanup_cancelled and not outer_cancelled:
                raise asyncio.CancelledError


class ApplicationServerService:
    """Thin transport-agnostic boundary delegating solely to ApplicationCoordinator."""

    def __init__(
        self,
        *,
        coordinator: ApplicationCoordinator,
        runtime_config: RuntimeConfig,
        server_config: ServerConfig,
        sessions: SessionRegistry | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._runtime_config = runtime_config
        self._server_config = server_config
        self._sessions = (
            SessionRegistry(server_config) if sessions is None else sessions
        )
        self._supervisor = QuerySupervisor(
            coordinator=coordinator,
            sessions=self._sessions,
            server_config=server_config,
        )
        self._pipeline_fp = pipeline_fingerprint(runtime_config)
        self._runtime_fp = runtime_fingerprint(runtime_config)

    @property
    def coordinator(self) -> ApplicationCoordinator:
        return self._coordinator

    @property
    def server_config(self) -> ServerConfig:
        return self._server_config

    @property
    def sessions(self) -> SessionRegistry:
        return self._sessions

    @property
    def supervisor(self) -> QuerySupervisor:
        return self._supervisor

    def liveness(self) -> LivenessResponse:
        mode = self._coordinator.gate.mode
        status = (
            LivenessStatus.SHUTTING_DOWN
            if mode is RuntimeMode.SHUTTING_DOWN
            else LivenessStatus.ALIVE
        )
        return LivenessResponse(status=status, mode=mode)

    async def readiness(self) -> ReadinessResponse:
        ready = await self._coordinator.readiness()
        mode = self._coordinator.gate.mode
        return ReadinessResponse(
            ready=ready and mode is RuntimeMode.SERVING,
            mode=mode,
        )

    async def status(self, *, refresh: bool = False) -> RuntimeStatusResponse:
        gate = self._coordinator.gate
        if gate.mode is RuntimeMode.SHUTTING_DOWN:
            raise RuntimeShuttingDownError
        snapshot = await self._coordinator.status(
            refresh=refresh,
            min_refresh_interval_seconds=(
                self._server_config.min_manual_refresh_interval_seconds
            ),
        )
        current_mode: RuntimeMode = self._coordinator.gate.mode
        current_lifecycle_generation = self._coordinator.gate.lifecycle_generation
        if current_mode is RuntimeMode.SHUTTING_DOWN:
            raise RuntimeShuttingDownError
        lifecycle_consistent = (
            snapshot.lifecycle_generation == current_lifecycle_generation
        )
        ready = (
            lifecycle_consistent
            and current_mode is RuntimeMode.SERVING
            and snapshot.runtime is RuntimeMode.SERVING
            and snapshot.hard_healthy
        )
        knowledge_verified = (
            snapshot.knowledge_verified if lifecycle_consistent else False
        )
        query_cache = (
            "BYPASSED"
            if (not lifecycle_consistent and snapshot.query_cache == "ACTIVE")
            else snapshot.query_cache
        )
        return RuntimeStatusResponse(
            mode=current_mode,
            ready=ready,
            knowledge_verified=knowledge_verified,
            active_queries=gate.active_queries,
            dependencies=DependencyStatusMap(
                postgres=snapshot.postgres,
                milvus=snapshot.milvus,
                redis=snapshot.redis,
                searxng=snapshot.searxng,
                embedding=snapshot.embedding,
                reranker=snapshot.reranker,
            ),
            query_cache=query_cache,
            pipeline_fingerprint=self._pipeline_fp,
            runtime_fingerprint=self._runtime_fp,
            checked_at=snapshot.checked_at,
        )

    async def create_session(
        self,
        request: CreateSessionRequest,
        *,
        header_client_kind: ClientKind,
    ) -> SessionResponse:
        initial_mode: RuntimeMode = self._coordinator.gate.mode
        if initial_mode is RuntimeMode.SHUTTING_DOWN:
            raise RuntimeShuttingDownError
        if request.client_kind is not header_client_kind:
            raise ServerHttpError(
                TransportErrorCode.FORBIDDEN,
                "Request client classification does not match requested session kind.",
            )
        info = await self._sessions.create_session(request.client_kind)
        return SessionResponse(
            session_id=info.session_id,
            client_kind=info.client_kind,
            created_at=info.created_at,
            expires_at=info.expires_at,
        )

    async def close_session(
        self,
        session_id: str,
        *,
        header_client_kind: ClientKind,
    ) -> None:
        initial_mode: RuntimeMode = self._coordinator.gate.mode
        if initial_mode is RuntimeMode.SHUTTING_DOWN:
            raise RuntimeShuttingDownError
        await self._sessions.close_session(session_id, client_kind=header_client_kind)

    async def query(
        self,
        request: QueryRequest,
        *,
        header_client_kind: ClientKind,
        monitor_factory: MonitorFactory | None = None,
        disconnect_signal: DisconnectSignal | None = None,
    ) -> QueryResponse:
        envelope = await self._supervisor.execute(
            request,
            client_kind=header_client_kind,
            monitor_factory=monitor_factory,
            disconnect_signal=disconnect_signal,
        )
        return QueryResponse(
            trace_id=envelope.trace_id,
            session_id=request.session_id,
            cache_status=envelope.cache_status,
            elapsed_ms=0.0 if envelope.elapsed_ms is None else envelope.elapsed_ms,
            response=envelope.response,
        )
