"""Framework-independent application service boundary over ApplicationCoordinator."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
from atlasrag.server.events import InvocationEventBus, StreamChannel
from atlasrag.server.operations import MaintenanceOperationRegistry
from atlasrag.server.schemas import (
    ClientKind,
    CreateSessionRequest,
    DependencyStatusMap,
    LivenessResponse,
    LivenessStatus,
    OperationAcceptedResponse,
    OperationKind,
    OperationStatusResponse,
    QueryRequest,
    QueryResponse,
    ReadinessResponse,
    RuntimeStatusResponse,
    SessionResponse,
    ShutdownAcceptedResponse,
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


def _request_cancel_once(task: asyncio.Task[Any]) -> bool:
    """Cancel task iff it is active and not already in a cancelling state."""

    if task.done():
        return False
    if task.cancelling() == 0:
        task.cancel()
    return True


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
    cancel_sent = already_cancelled or task.cancelling() > 0
    if cancel and not cancel_sent and not task.done():
        _request_cancel_once(task)
        cancel_sent = True
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError:
            outer_cancelled = True
            if not cancel_sent and not task.done():
                _request_cancel_once(task)
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


@dataclass(frozen=True)
class ShutdownOwner:
    """Process-owner shutdown boundary injected by the WP-D launcher."""

    pid: int
    server_instance_id: str
    request_shutdown: Callable[[], Awaitable[None] | None]


class _StreamSlot:
    """Single-use bounded stream-admission reservation tied to QuerySupervisor."""

    def __init__(
        self, release_fn: Callable[[], None], acquire_fn: Callable[[], None]
    ) -> None:
        self._acquire_fn = acquire_fn
        self._release_fn = release_fn
        self.acquired = False
        self.released = False

    def acquire(self) -> None:
        if not self.acquired:
            self._acquire_fn()
            self.acquired = True

    def release(self) -> None:
        if self.acquired and not self.released:
            self.released = True
            self._release_fn()


@dataclass
class StreamHandle:
    """Authoritative ownership handle bridging admitted SSE execution to SseStreamResponse."""

    channel: StreamChannel
    disconnect_signal: DisconnectSignal
    supervisor_task: asyncio.Task[None]
    event_bus: InvocationEventBus
    _stream_slot: _StreamSlot
    _closed: bool = False

    async def close_and_settle(self) -> None:
        """Close the channel, settle query execution if unfinished, and free stream slot."""

        if self._closed:
            return
        self._closed = True
        self.channel.close()
        if not self.supervisor_task.done():
            self.disconnect_signal.trigger("stream_closed")
        outer_cancelled, _ = await _settle_task_no_raise(
            self.supervisor_task, cancel=False
        )
        self.event_bus.unbind(self.channel.trace_id)
        self._stream_slot.release()
        if outer_cancelled:
            raise asyncio.CancelledError


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
        self._in_flight_streams = 0

    @property
    def in_flight_queries(self) -> int:
        return self._in_flight_queries

    @property
    def in_flight_streams(self) -> int:
        return self._in_flight_streams

    def _acquire_stream_slot_count(self) -> None:
        if self._in_flight_streams >= self._server_config.max_concurrent_streams:
            raise ResourceExhaustedError
        self._in_flight_streams += 1

    def _release_stream_slot_count(self) -> None:
        self._in_flight_streams = max(0, self._in_flight_streams - 1)

    def reserve_stream_slot(self) -> _StreamSlot:
        return _StreamSlot(
            release_fn=self._release_stream_slot_count,
            acquire_fn=self._acquire_stream_slot_count,
        )

    async def execute(
        self,
        request: QueryRequest,
        *,
        client_kind: ClientKind,
        on_admitted: Callable[[str], None] | None = None,
        monitor_factory: MonitorFactory | None = None,
        disconnect_signal: DisconnectSignal | None = None,
        stream_slot: _StreamSlot | None = None,
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
            if stream_slot is not None:
                stream_slot.acquire()
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
                        _request_cancel_once(query_task)
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
                        _request_cancel_once(query_task)
                    break
                if query_task.done():
                    break
                if disconnect_wait_task in done:
                    if not cancel_requested:
                        cancel_requested = True
                        _request_cancel_once(query_task)
                    break
                if not done:
                    timed_out = True
                    if not cancel_requested:
                        cancel_requested = True
                        _request_cancel_once(query_task)
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
        event_bus: InvocationEventBus | None = None,
        operations: MaintenanceOperationRegistry | None = None,
        shutdown_owner: ShutdownOwner | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._runtime_config = runtime_config
        self._server_config = server_config
        self._sessions = (
            SessionRegistry(server_config) if sessions is None else sessions
        )
        self._event_bus = InvocationEventBus() if event_bus is None else event_bus
        self._operations = (
            MaintenanceOperationRegistry(
                coordinator=coordinator,
                server_config=server_config,
            )
            if operations is None
            else operations
        )
        self._shutdown_owner = shutdown_owner
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
    def event_bus(self) -> InvocationEventBus:
        return self._event_bus

    @property
    def operations(self) -> MaintenanceOperationRegistry:
        return self._operations

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

    async def start_stream_query(
        self,
        request: QueryRequest,
        *,
        header_client_kind: ClientKind,
        monitor_factory: MonitorFactory | None = None,
        disconnect_signal: DisconnectSignal | None = None,
    ) -> StreamHandle:
        """Admit a streaming query via coordinator.on_admitted before committing SSE headers."""

        if len(request.query) > self._server_config.max_query_chars:
            raise ServerHttpError(
                TransportErrorCode.INVALID_REQUEST,
                "Query exceeds configured maximum character length.",
                details=("query: max_length",),
            )
        if self._coordinator.gate.mode is RuntimeMode.SHUTTING_DOWN:
            raise RuntimeShuttingDownError

        signal = DisconnectSignal() if disconnect_signal is None else disconnect_signal
        channel = StreamChannel(
            session_id=request.session_id,
            server_config=self._server_config,
        )
        stream_slot = self._supervisor.reserve_stream_slot()
        pre_admission_error: BaseException | None = None

        def _on_admitted(trace_id: str) -> None:
            channel.mark_admitted(trace_id)
            self._event_bus.bind(trace_id, channel)

        async def _drive_stream_query() -> None:
            nonlocal pre_admission_error
            try:
                envelope = await self._supervisor.execute(
                    request,
                    client_kind=header_client_kind,
                    on_admitted=_on_admitted,
                    monitor_factory=monitor_factory,
                    disconnect_signal=signal,
                    stream_slot=stream_slot,
                )
            except BaseException as exc:
                if not channel.accepted_event.is_set():
                    pre_admission_error = exc
                    return
                channel.publish_error(exc)
            else:
                channel.publish_final(
                    QueryResponse(
                        trace_id=envelope.trace_id,
                        session_id=request.session_id,
                        cache_status=envelope.cache_status,
                        elapsed_ms=(
                            0.0 if envelope.elapsed_ms is None else envelope.elapsed_ms
                        ),
                        response=envelope.response,
                    )
                )
            finally:
                self._event_bus.unbind(channel.trace_id)

        supervisor_task: asyncio.Task[None] = asyncio.create_task(_drive_stream_query())
        accepted_wait: asyncio.Task[bool] = asyncio.create_task(
            channel.accepted_event.wait()
        )
        disconnect_wait: asyncio.Task[bool] = asyncio.create_task(signal.event.wait())
        try:
            await asyncio.wait(
                {accepted_wait, supervisor_task, disconnect_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            await _settle_task_no_raise(accepted_wait, cancel=True)
            await _settle_task_no_raise(disconnect_wait, cancel=True)
            signal.trigger("pre_admission_cancelled")
            await _settle_task_no_raise(supervisor_task, cancel=False)
            self._event_bus.unbind(channel.trace_id)
            channel.close()
            stream_slot.release()
            raise
        finally:
            await _settle_task_no_raise(accepted_wait, cancel=True)
            await _settle_task_no_raise(disconnect_wait, cancel=True)

        if not channel.accepted_event.is_set():
            if signal.event.is_set():
                signal.trigger("client_disconnect")
            await _settle_task_no_raise(supervisor_task, cancel=False)
            channel.close()
            stream_slot.release()
            if pre_admission_error is not None:
                raise pre_admission_error
            if signal.event.is_set():
                raise ServerHttpError(TransportErrorCode.REQUEST_CANCELLED)
            raise RuntimeShuttingDownError

        if signal.event.is_set():
            channel.close()
            await _settle_task_no_raise(supervisor_task, cancel=False)
            self._event_bus.unbind(channel.trace_id)
            stream_slot.release()
            raise ServerHttpError(TransportErrorCode.REQUEST_CANCELLED)

        return StreamHandle(
            channel=channel,
            disconnect_signal=signal,
            supervisor_task=supervisor_task,
            event_bus=self._event_bus,
            _stream_slot=stream_slot,
        )

    async def start_sync(
        self,
        *,
        on_draining_hook: Callable[[], None] | None = None,
    ) -> OperationAcceptedResponse:
        return await self._operations.start_operation(
            OperationKind.SYNC,
            on_draining_hook=on_draining_hook,
        )

    async def start_recover(
        self,
        *,
        on_draining_hook: Callable[[], None] | None = None,
    ) -> OperationAcceptedResponse:
        return await self._operations.start_operation(
            OperationKind.RECOVER,
            on_draining_hook=on_draining_hook,
        )

    async def get_operation(self, operation_id: str) -> OperationStatusResponse:
        return await self._operations.get_operation(operation_id)

    async def request_shutdown(self) -> ShutdownAcceptedResponse:
        owner = self._shutdown_owner
        if owner is None:
            raise ServerHttpError(
                ErrorCode.RUNTIME_NOT_READY,
                "Server shutdown owner is not configured for this runtime.",
            )
        if owner.pid < 1 or not owner.server_instance_id.strip():
            raise ServerHttpError(
                ErrorCode.INVARIANT_VIOLATION,
                "Invalid server shutdown owner identity.",
            )
        await self._coordinator.gate.begin_shutdown()
        outcome = owner.request_shutdown()
        if inspect.isawaitable(outcome):
            await outcome
        return ShutdownAcceptedResponse(
            status=LivenessStatus.SHUTTING_DOWN,
            pid=owner.pid,
            server_instance_id=owner.server_instance_id,
        )
