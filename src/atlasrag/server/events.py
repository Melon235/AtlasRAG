"""Safe SSE event projection, bounded StreamChannel, and InvocationEventBus."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

from atlasrag.domain.errors import AtlasRAGError, ErrorCode
from atlasrag.domain.observation import ObservationStage, ProductionObservation
from atlasrag.runtime.observation import InvocationObserverMux
from atlasrag.server.config import ServerConfig
from atlasrag.server.errors import (
    ServerHttpError,
    TransportErrorCode,
    translate_exception,
)
from atlasrag.server.schemas import (
    QueryResponse,
    SseAcceptedEvent,
    SseErrorEvent,
    SseProgressEvent,
)

# Strict allowlist of public (stage, status) progress pairs matching genuine graph stages.
ALLOWED_PROGRESS_STATUSES: dict[ObservationStage, frozenset[str]] = {
    ObservationStage.DENSE: frozenset({"OK", "EMPTY", "UNAVAILABLE"}),
    ObservationStage.BM25: frozenset({"OK", "EMPTY", "UNAVAILABLE"}),
    ObservationStage.FUSION: frozenset({"OK", "EMPTY"}),
    ObservationStage.RERANKER: frozenset({"OK", "DEGRADED"}),
    ObservationStage.EVIDENCE: frozenset({"OK"}),
    ObservationStage.SUFFICIENCY: frozenset(
        {"SUFFICIENT", "INSUFFICIENT", "AMBIGUOUS", "UNAVAILABLE"}
    ),
    ObservationStage.ROUTING: frozenset({"ANSWER", "RETRY_LOCAL", "WEB"}),
    ObservationStage.ANSWER: frozenset({"SUCCESS", "SAFE_FAILURE"}),
}


def format_sse_frame(event_name: str, data_json: bytes | str) -> bytes:
    """Format one wire-safe Server-Sent Event frame."""

    payload = data_json.encode("utf-8") if isinstance(data_json, str) else data_json
    return b"event: " + event_name.encode("ascii") + b"\ndata: " + payload + b"\n\n"


def project_observation(
    event: ProductionObservation,
    *,
    trace_id: str,
) -> SseProgressEvent | None:
    """Project a ProductionObservation onto strict public SSE fields or drop it."""

    allowed_statuses = ALLOWED_PROGRESS_STATUSES.get(event.stage)
    if allowed_statuses is None or event.status not in allowed_statuses:
        return None
    attempt = event.attempt_index if event.attempt_index >= 1 else 1
    return SseProgressEvent(
        trace_id=trace_id,
        stage=event.stage,
        status=event.status,
        attempt=attempt,
        degraded=bool(event.degraded),
    )


class StreamChannel:
    """Bounded per-invocation SSE event buffer with reliable terminal slot.

    Producer callbacks (`observe`) are strictly nonblocking and thread-safe
    across event-loop and worker threads. Only allowlisted progress frames fit
    into the bounded queue (evicting oldest progress on overflow); terminal
    (`final` or `error`) outcomes use a separate slot so progress overflow never
    evicts completion or error frames inside the application buffer.
    """

    def __init__(
        self,
        *,
        session_id: str,
        server_config: ServerConfig,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._session_id = session_id
        self._max_progress_bytes = server_config.max_progress_event_bytes
        self._max_response_bytes = server_config.max_response_bytes
        self._loop = asyncio.get_running_loop() if loop is None else loop
        self._loop_thread_id = threading.get_ident()
        self._state_lock = threading.Lock()
        self._accepted_event = asyncio.Event()
        self._wakeup = asyncio.Event()
        self._progress_queue: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=server_config.sse_queue_maxsize
        )
        self._trace_id: str | None = None
        self._accepted_frame: bytes | None = None
        self._terminal_frame: bytes | None = None
        self._closed = False
        self._dropped_progress = 0

    @property
    def trace_id(self) -> str | None:
        return self._trace_id

    @property
    def accepted_event(self) -> asyncio.Event:
        return self._accepted_event

    @property
    def accepted_frame(self) -> bytes | None:
        return self._accepted_frame

    @property
    def terminal_frame(self) -> bytes | None:
        return self._terminal_frame

    @property
    def dropped_progress_count(self) -> int:
        return self._dropped_progress

    @property
    def is_closed(self) -> bool:
        return self._closed

    def mark_admitted(self, trace_id: str) -> None:
        """Bind the authoritative trace_id after real coordinator admission."""

        frame = format_sse_frame(
            "accepted",
            SseAcceptedEvent(
                trace_id=trace_id,
                session_id=self._session_id,
            ).model_dump_json(),
        )
        with self._state_lock:
            if self._closed or self._trace_id is not None:
                return
            self._trace_id = trace_id
            self._accepted_frame = frame
        self._accepted_event.set()
        self._wakeup.set()

    def observe(self, event: ProductionObservation) -> None:
        """Accept an observation without blocking or waiting for a slow consumer."""

        with self._state_lock:
            trace_id = self._trace_id
            if self._closed or self._terminal_frame is not None or trace_id is None:
                return
            if event.invocation_id is not None and event.invocation_id != trace_id:
                return
        projected = project_observation(event, trace_id=trace_id)
        if projected is None:
            return
        frame = format_sse_frame("progress", projected.model_dump_json())
        if len(frame) > self._max_progress_bytes:
            return
        if threading.get_ident() == self._loop_thread_id:
            self._enqueue_progress_on_loop(frame)
            return
        if self._loop.is_closed():
            return
        with self._state_lock:
            if self._closed or self._terminal_frame is not None:
                return
        try:
            self._loop.call_soon_threadsafe(self._enqueue_progress_on_loop, frame)
        except RuntimeError:
            return

    def _enqueue_progress_on_loop(self, frame: bytes) -> None:
        with self._state_lock:
            if self._closed or self._terminal_frame is not None:
                return
        if self._progress_queue.full():
            try:
                self._progress_queue.get_nowait()
                self._dropped_progress += 1
            except asyncio.QueueEmpty:
                pass
        try:
            self._progress_queue.put_nowait(frame)
        except asyncio.QueueFull:
            return
        self._wakeup.set()

    def publish_final(self, response: QueryResponse) -> None:
        """Publish the single terminal final event if serialized size fits bounds."""

        encoded = response.model_dump_json().encode("utf-8")
        if len(encoded) > self._max_response_bytes:
            self.publish_error(
                ServerHttpError(
                    ErrorCode.INVARIANT_VIOLATION,
                    "Serialized response exceeds maximum allowed size.",
                    trace_id=self._trace_id,
                )
            )
            return
        frame = format_sse_frame("final", encoded)
        self._set_terminal_frame(frame)

    def publish_error(self, error: BaseException) -> None:
        """Translate an error and publish the single safe terminal error event."""

        safe_trace_id = self._trace_id
        if isinstance(error, (ServerHttpError, AtlasRAGError)):
            _, api_error = translate_exception(error, trace_id=safe_trace_id)
        elif isinstance(error, asyncio.CancelledError):
            _, api_error = translate_exception(
                ServerHttpError(
                    TransportErrorCode.REQUEST_CANCELLED,
                    trace_id=safe_trace_id,
                ),
                trace_id=safe_trace_id,
            )
        else:
            _, api_error = translate_exception(error, trace_id=safe_trace_id)
        payload = SseErrorEvent(
            trace_id=api_error.trace_id,
            error_code=api_error.error_code,
            message=api_error.message,
        )
        self._set_terminal_frame(format_sse_frame("error", payload.model_dump_json()))

    def publish_error_payload(self, *, error_code: str, message: str) -> None:
        """Publish an explicit safe terminal error payload."""

        payload = SseErrorEvent(
            trace_id=self._trace_id,
            error_code=error_code,
            message=message,
        )
        self._set_terminal_frame(format_sse_frame("error", payload.model_dump_json()))

    def _set_terminal_frame(self, frame: bytes) -> None:
        with self._state_lock:
            if self._terminal_frame is not None:
                return
            self._terminal_frame = frame
        self._wakeup.set()

    def close(self) -> None:
        """Close the channel against subsequent progress emissions."""

        with self._state_lock:
            self._closed = True
        self._wakeup.set()

    def pop_progress_nowait(self) -> bytes | None:
        """Pop the next buffered progress frame if one is available."""

        try:
            return self._progress_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def has_pending_progress(self) -> bool:
        return not self._progress_queue.empty()

    def clear_wakeup(self) -> None:
        self._wakeup.clear()

    async def wait_wakeup(self) -> None:
        await self._wakeup.wait()


class InvocationEventBus:
    """Per-server ProductionObserver router mapping trace_id -> StreamChannel."""

    def __init__(self, mux: InvocationObserverMux | None = None) -> None:
        self._mux = InvocationObserverMux() if mux is None else mux

    def bind(self, trace_id: str, channel: StreamChannel) -> None:
        """Bind a StreamChannel strictly to its authoritative coordinator trace_id."""

        self._mux.bind(trace_id, channel)

    def unbind(self, trace_id: str | None) -> None:
        """Remove a bound StreamChannel after settlement/cleanup."""

        if trace_id:
            self._mux.unbind(trace_id)

    def bound_count(self) -> int:
        return self._mux.bound_count()

    def observe(self, event: ProductionObservation) -> None:
        """Deliver an observation to the matching invocation channel without blocking."""

        self._mux.observe(event)


EventBusFactory = Callable[[], InvocationEventBus]
