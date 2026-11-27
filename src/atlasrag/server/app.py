"""Thin FastAPI application factory and local HTTP transport routes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import AtlasRAGError
from atlasrag.runtime.composition import ApplicationComposition
from atlasrag.runtime.coordinator import ApplicationCoordinator
from atlasrag.server.auth import (
    build_allowed_hosts,
    build_allowed_origins,
    validate_bearer_token,
    validate_client_csrf_header,
    validate_host_header,
    validate_origin_and_referer,
)
from atlasrag.server.cancellation import settle_task_no_raise
from atlasrag.server.config import ServerConfig
from atlasrag.server.errors import (
    ServerHttpError,
    TransportErrorCode,
    translate_exception,
)
from atlasrag.server.events import InvocationEventBus
from atlasrag.server.schemas import (
    ApiErrorResponse,
    ClientKind,
    ControlEmptyRequest,
    CreateSessionRequest,
    LivenessResponse,
    OperationAcceptedResponse,
    OperationStatusResponse,
    QueryRequest,
    QueryResponse,
    ReadinessResponse,
    RuntimeStatusResponse,
    SessionResponse,
    ShutdownAcceptedResponse,
)
from atlasrag.server.service import (
    ApplicationServerService,
    DisconnectSignal,
    ShutdownOwner,
    StreamHandle,
    serialize_bounded_response,
)
from atlasrag.server.sessions import SessionRegistry

CoordinatorFactory = Callable[[RuntimeConfig], Awaitable[ApplicationCoordinator]]


class _RequestSizeLimitExceeded(Exception):
    pass


class _LocalSecurityMiddleware:
    """Enforce Host and same-origin/Referer checks without BaseHTTPMiddleware."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: frozenset[str],
        allowed_origins: frozenset[str],
    ) -> None:
        self._app = app
        self._allowed_hosts = allowed_hosts
        self._allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {
            key.decode("latin1").casefold(): value.decode("latin1")
            for key, value in scope.get("headers", ())
        }
        try:
            validate_host_header(headers.get("host"), self._allowed_hosts)
            validate_origin_and_referer(
                method=str(scope.get("method", "GET")),
                origin=headers.get("origin"),
                referer=headers.get("referer"),
                allowed_origins=self._allowed_origins,
            )
        except ServerHttpError as error:
            status, payload = translate_exception(error)
            response = _json_error_response(status, payload)
            await response(scope, _empty_receive, send)
            return
        await self._app(scope, receive, send)


class _RequestBodyLimitMiddleware:
    """Reject HTTP bodies larger than max_request_bytes before JSON parsing."""

    def __init__(self, app: ASGIApp, *, max_request_bytes: int) -> None:
        self._app = app
        self._max_request_bytes = max_request_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {
            key.decode("latin1").casefold(): value.decode("latin1")
            for key, value in scope.get("headers", ())
        }
        raw_length = headers.get("content-length")
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError:
                await self._send_error(
                    scope,
                    send,
                    ServerHttpError(
                        TransportErrorCode.INVALID_REQUEST,
                        "Invalid Content-Length header.",
                        status_code=400,
                    ),
                )
                return
            if content_length < 0:
                await self._send_error(
                    scope,
                    send,
                    ServerHttpError(
                        TransportErrorCode.INVALID_REQUEST,
                        "Invalid Content-Length header.",
                        status_code=400,
                    ),
                )
                return
            if content_length > self._max_request_bytes:
                await self._send_error(
                    scope, send, ServerHttpError(TransportErrorCode.PAYLOAD_TOO_LARGE)
                )
                return

        total_bytes = 0
        size_exceeded = False

        async def bounded_receive() -> Message:
            nonlocal total_bytes, size_exceeded
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                total_bytes += len(body)
                if total_bytes > self._max_request_bytes:
                    size_exceeded = True
                    raise _RequestSizeLimitExceeded
            return message

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, bounded_receive, tracking_send)
        except BaseException:
            if size_exceeded:
                if not response_started:
                    await self._send_error(
                        scope,
                        send,
                        ServerHttpError(TransportErrorCode.PAYLOAD_TOO_LARGE),
                    )
                return
            raise

    async def _send_error(
        self, scope: Scope, send: Send, error: ServerHttpError
    ) -> None:
        status, payload = translate_exception(error)
        response = JSONResponse(
            status_code=status, content=payload.model_dump(mode="json")
        )
        await response(scope, _empty_receive, send)


async def _empty_receive() -> Message:
    return {"type": "http.disconnect"}


async def _watch_http_disconnect(
    receive: Callable[[], Awaitable[Message]], signal: DisconnectSignal
) -> None:
    """Single authoritative post-body ASGI receive consumer for HTTP query disconnect."""

    try:
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                signal.trigger("client_disconnect")
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        signal.trigger("receive_error")


class SseStreamResponse(Response):
    """Minimal custom ASGI SSE response with no duplicate receive consumer."""

    def __init__(
        self,
        *,
        handle: StreamHandle,
        server_config: ServerConfig,
    ) -> None:
        self._handle = handle
        self._heartbeat_seconds = server_config.sse_heartbeat_seconds
        self._send_timeout_seconds = server_config.sse_send_timeout_seconds
        super().__init__(
            content=None,
            status_code=200,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        if "content-length" in self.headers:
            del self.headers["content-length"]
        self._response_headers = [
            (key, value)
            for key, value in self.raw_headers
            if key.lower() != b"content-length"
        ]

    async def _send_with_timeout(self, send: Send, message: Message) -> None:
        await asyncio.wait_for(
            send(message),
            timeout=self._send_timeout_seconds,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        channel = self._handle.channel
        signal = self._handle.disconnect_signal
        accepted_frame = channel.accepted_frame
        if accepted_frame is None:
            await self._handle.close_and_settle()
            return
        send_failed = False
        outer_cancelled = False
        try:
            try:
                await self._send_with_timeout(
                    send,
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": self._response_headers,
                    },
                )
                await self._send_with_timeout(
                    send,
                    {
                        "type": "http.response.body",
                        "body": accepted_frame,
                        "more_body": True,
                    },
                )
                while not signal.event.is_set():
                    progress_frame = channel.pop_progress_nowait()
                    if progress_frame is not None:
                        await self._send_with_timeout(
                            send,
                            {
                                "type": "http.response.body",
                                "body": progress_frame,
                                "more_body": True,
                            },
                        )
                        continue
                    terminal_frame = channel.terminal_frame
                    if terminal_frame is not None:
                        await self._send_with_timeout(
                            send,
                            {
                                "type": "http.response.body",
                                "body": terminal_frame,
                                "more_body": True,
                            },
                        )
                        await self._send_with_timeout(
                            send,
                            {
                                "type": "http.response.body",
                                "body": b"",
                                "more_body": False,
                            },
                        )
                        break

                    channel.clear_wakeup()
                    if (
                        channel.has_pending_progress()
                        or channel.terminal_frame is not None
                        or signal.event.is_set()
                    ):
                        continue

                    wakeup_task = asyncio.create_task(channel.wait_wakeup())
                    disconnect_task = asyncio.create_task(signal.event.wait())
                    done: set[asyncio.Task[bool | None]] | set[asyncio.Task[Any]] = (
                        set()
                    )
                    try:
                        done, _ = await asyncio.wait(
                            {wakeup_task, disconnect_task},
                            timeout=self._heartbeat_seconds,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    except asyncio.CancelledError:
                        outer_cancelled = True
                        raise
                    finally:
                        w_cancel, _ = await settle_task_no_raise(
                            wakeup_task, cancel=True
                        )
                        d_cancel, _ = await settle_task_no_raise(
                            disconnect_task, cancel=True
                        )
                        if (w_cancel or d_cancel) and not outer_cancelled:
                            outer_cancelled = True
                            raise asyncio.CancelledError

                    if signal.event.is_set():
                        break
                    if not done:
                        await self._send_with_timeout(
                            send,
                            {
                                "type": "http.response.body",
                                "body": b": heartbeat\n\n",
                                "more_body": True,
                            },
                        )
            except TimeoutError:
                send_failed = True
                signal.trigger("send_timeout")
            except Exception:
                send_failed = True
                signal.trigger("send_error")
        finally:
            if (
                not send_failed
                and not self._handle.supervisor_task.done()
                and not signal.event.is_set()
            ):
                signal.trigger("stream_closed")
            await self._handle.close_and_settle()


def _json_error_response(status_code: int, payload: ApiErrorResponse) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=payload.model_dump(mode="json")
    )


def _service_from_request(request: Request) -> ApplicationServerService:
    return cast(ApplicationServerService, request.app.state.server_service)


def require_control_bearer(request: Request) -> None:
    """FastAPI dependency enforcing privileged Control Plane Bearer token auth."""

    provider = cast(Callable[[], str | None], request.app.state.control_token_provider)
    validate_bearer_token(request.headers.get("authorization"), provider())


def require_knowledge_client(request: Request) -> ClientKind:
    """FastAPI dependency enforcing mandatory X-AtlasRAG-Client classification."""

    return ClientKind(
        validate_client_csrf_header(request.headers.get("x-atlasrag-client"))
    )


def create_app(
    *,
    runtime_config: RuntimeConfig,
    server_config: ServerConfig,
    coordinator_factory: CoordinatorFactory | None = None,
    coordinator: ApplicationCoordinator | None = None,
    control_token_provider: Callable[[], str | None] | None = None,
    event_bus: InvocationEventBus | None = None,
    shutdown_owner: ShutdownOwner | None = None,
) -> FastAPI:
    """Create the thin FastAPI application wrapping one ApplicationCoordinator."""

    if coordinator is not None and coordinator_factory is not None:
        raise ValueError("Provide at most one of coordinator or coordinator_factory")

    allowed_hosts = build_allowed_hosts(server_config)
    allowed_origins = build_allowed_origins(server_config)
    token_provider: Callable[[], str | None] = (
        (lambda: None) if control_token_provider is None else control_token_provider
    )
    active_event_bus = InvocationEventBus() if event_bus is None else event_bus

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_shutdown = False
        active_coordinator = coordinator
        if active_coordinator is None:
            owns_shutdown = True
            if coordinator_factory is not None:
                active_coordinator = await coordinator_factory(runtime_config)
            else:
                active_coordinator = await ApplicationComposition(runtime_config).start(
                    observer=active_event_bus
                )
        sessions = SessionRegistry(server_config)
        service = ApplicationServerService(
            coordinator=active_coordinator,
            runtime_config=runtime_config,
            server_config=server_config,
            sessions=sessions,
            event_bus=active_event_bus,
            shutdown_owner=shutdown_owner,
        )
        app.state.coordinator = active_coordinator
        app.state.control_token_provider = token_provider
        app.state.event_bus = active_event_bus
        app.state.sessions = sessions
        app.state.server_service = service
        try:
            yield
        finally:
            if owns_shutdown and active_coordinator is not None:
                await active_coordinator.shutdown()
            await service.operations.wait_active(
                timeout=runtime_config.maintenance.shutdown_timeout_seconds
            )

    app = FastAPI(
        title="AtlasRAG Local Application Server",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.control_token_provider = token_provider

    # Pure ASGI middleware ordering: _LocalSecurityMiddleware outermost,
    # then _RequestBodyLimitMiddleware inside it; zero BaseHTTPMiddleware.
    app.add_middleware(
        _RequestBodyLimitMiddleware,
        max_request_bytes=server_config.max_request_bytes,
    )
    app.add_middleware(
        _LocalSecurityMiddleware,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )

    @app.exception_handler(ServerHttpError)
    async def handle_server_http_error(
        _request: Request, exc: ServerHttpError
    ) -> JSONResponse:
        status, payload = translate_exception(exc)
        return _json_error_response(status, payload)

    @app.exception_handler(AtlasRAGError)
    async def handle_atlasrag_error(
        _request: Request, exc: AtlasRAGError
    ) -> JSONResponse:
        status, payload = translate_exception(exc)
        return _json_error_response(status, payload)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = tuple(
            f"{'.'.join(str(part) for part in err.get('loc', ())) or 'request'}: "
            f"{err.get('type', 'validation_error')}"
            for err in exc.errors()
        )[:16]
        return _json_error_response(
            422,
            ApiErrorResponse(
                error_code=TransportErrorCode.INVALID_REQUEST.value,
                message="Request validation failed.",
                details=details,
            ),
        )

    @app.exception_handler(ValidationError)
    async def handle_pydantic_validation_error(
        _request: Request, exc: ValidationError
    ) -> JSONResponse:
        status, payload = translate_exception(exc)
        return _json_error_response(status, payload)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if exc.status_code == 401:
            code = TransportErrorCode.UNAUTHORIZED
            msg = "Valid local control authorization is required."
        elif exc.status_code == 403:
            code = TransportErrorCode.FORBIDDEN
            msg = "Request rejected by local transport security policy."
        elif exc.status_code in {400, 404, 405, 422}:
            code = TransportErrorCode.INVALID_REQUEST
            msg = "Request is invalid or endpoint was not found."
        else:
            code = TransportErrorCode.INTERNAL_ERROR
            msg = "An internal server error occurred."
        return _json_error_response(
            exc.status_code,
            ApiErrorResponse(error_code=code.value, message=msg),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        status, payload = translate_exception(exc)
        return _json_error_response(status, payload)

    @app.get("/api/v1/health/live", response_model=LivenessResponse)
    async def health_live(request: Request) -> Response:
        service = _service_from_request(request)
        payload = service.liveness()
        raw = serialize_bounded_response(
            payload, max_bytes=service.server_config.max_response_bytes
        )
        return Response(content=raw, status_code=200, media_type="application/json")

    @app.get("/api/v1/health/ready", response_model=ReadinessResponse)
    async def health_ready(request: Request) -> Response:
        service = _service_from_request(request)
        payload = await service.readiness()
        raw = serialize_bounded_response(
            payload, max_bytes=service.server_config.max_response_bytes
        )
        return Response(
            content=raw,
            status_code=200 if payload.ready else 503,
            media_type="application/json",
        )

    @app.get("/api/v1/status", response_model=RuntimeStatusResponse)
    async def status_endpoint(request: Request, refresh: bool = False) -> Response:
        service = _service_from_request(request)
        payload = await service.status(refresh=refresh)
        raw = serialize_bounded_response(
            payload, max_bytes=service.server_config.max_response_bytes
        )
        return Response(content=raw, status_code=200, media_type="application/json")

    @app.post("/api/v1/sessions", response_model=SessionResponse, status_code=201)
    async def create_session_endpoint(
        request: Request,
        client_kind: ClientKind = Depends(require_knowledge_client),
    ) -> Response:
        service = _service_from_request(request)
        raw_body = await request.body()
        dto = (
            CreateSessionRequest()
            if not raw_body.strip()
            else CreateSessionRequest.model_validate_json(raw_body)
        )
        payload = await service.create_session(dto, header_client_kind=client_kind)
        raw = serialize_bounded_response(
            payload, max_bytes=service.server_config.max_response_bytes
        )
        return Response(content=raw, status_code=201, media_type="application/json")

    @app.delete("/api/v1/sessions/{session_id}", status_code=204)
    async def delete_session_endpoint(
        request: Request,
        session_id: str,
        client_kind: ClientKind = Depends(require_knowledge_client),
    ) -> Response:
        service = _service_from_request(request)
        await service.close_session(session_id, header_client_kind=client_kind)
        return Response(status_code=204)

    @app.post("/api/v1/query", response_model=QueryResponse)
    async def query_endpoint(
        request: Request,
        client_kind: ClientKind = Depends(require_knowledge_client),
    ) -> Response:
        service = _service_from_request(request)
        raw_body = await request.body()
        dto = QueryRequest.model_validate_json(raw_body)

        def _spawn_monitor(signal: DisconnectSignal) -> asyncio.Task[None]:
            return asyncio.create_task(_watch_http_disconnect(request.receive, signal))

        payload = await service.query(
            dto,
            header_client_kind=client_kind,
            monitor_factory=_spawn_monitor,
        )
        raw = serialize_bounded_response(
            payload, max_bytes=service.server_config.max_response_bytes
        )
        return Response(content=raw, status_code=200, media_type="application/json")

    @app.post("/api/v1/query/stream")
    async def stream_query_endpoint(
        request: Request,
        client_kind: ClientKind = Depends(require_knowledge_client),
    ) -> Response:
        service = _service_from_request(request)
        raw_body = await request.body()
        dto = QueryRequest.model_validate_json(raw_body)

        def _spawn_monitor(signal: DisconnectSignal) -> asyncio.Task[None]:
            return asyncio.create_task(_watch_http_disconnect(request.receive, signal))

        handle = await service.start_stream_query(
            dto,
            header_client_kind=client_kind,
            monitor_factory=_spawn_monitor,
        )
        return SseStreamResponse(handle=handle, server_config=service.server_config)

    @app.post(
        "/api/v1/control/sync",
        response_model=OperationAcceptedResponse,
        status_code=202,
        dependencies=[Depends(require_control_bearer)],
    )
    async def control_sync_endpoint(request: Request) -> Response:
        service = _service_from_request(request)
        raw_body = await request.body()
        if raw_body.strip():
            ControlEmptyRequest.model_validate_json(raw_body)
        payload = await service.start_sync()
        raw = serialize_bounded_response(
            payload, max_bytes=service.server_config.max_response_bytes
        )
        return Response(content=raw, status_code=202, media_type="application/json")

    @app.post(
        "/api/v1/control/recover",
        response_model=OperationAcceptedResponse,
        status_code=202,
        dependencies=[Depends(require_control_bearer)],
    )
    async def control_recover_endpoint(request: Request) -> Response:
        service = _service_from_request(request)
        raw_body = await request.body()
        if raw_body.strip():
            ControlEmptyRequest.model_validate_json(raw_body)
        payload = await service.start_recover()
        raw = serialize_bounded_response(
            payload, max_bytes=service.server_config.max_response_bytes
        )
        return Response(content=raw, status_code=202, media_type="application/json")

    @app.get(
        "/api/v1/control/operations/{operation_id}",
        response_model=OperationStatusResponse,
        dependencies=[Depends(require_control_bearer)],
    )
    async def control_operation_status_endpoint(
        request: Request,
        operation_id: str,
    ) -> Response:
        service = _service_from_request(request)
        payload = await service.get_operation(operation_id)
        raw = serialize_bounded_response(
            payload, max_bytes=service.server_config.max_response_bytes
        )
        return Response(content=raw, status_code=200, media_type="application/json")

    @app.post(
        "/api/v1/control/shutdown",
        response_model=ShutdownAcceptedResponse,
        status_code=202,
        dependencies=[Depends(require_control_bearer)],
    )
    async def control_shutdown_endpoint(request: Request) -> Response:
        service = _service_from_request(request)
        raw_body = await request.body()
        if raw_body.strip():
            ControlEmptyRequest.model_validate_json(raw_body)
        payload = await service.request_shutdown()
        raw = serialize_bounded_response(
            payload, max_bytes=service.server_config.max_response_bytes
        )
        return Response(content=raw, status_code=202, media_type="application/json")

    return app
