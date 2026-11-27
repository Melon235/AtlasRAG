"""Thin FastAPI application factory and WP-A health/status endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, Request, Response
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
    validate_host_header,
    validate_origin_and_referer,
)
from atlasrag.server.config import ServerConfig
from atlasrag.server.errors import (
    ServerHttpError,
    TransportErrorCode,
    translate_exception,
)
from atlasrag.server.schemas import (
    ApiErrorResponse,
    LivenessResponse,
    ReadinessResponse,
    RuntimeStatusResponse,
)
from atlasrag.server.service import ApplicationServerService, serialize_bounded_response

CoordinatorFactory = Callable[[RuntimeConfig], Awaitable[ApplicationCoordinator]]


class _RequestSizeLimitExceeded(Exception):
    pass


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


def create_app(
    *,
    runtime_config: RuntimeConfig,
    server_config: ServerConfig,
    coordinator_factory: CoordinatorFactory | None = None,
    coordinator: ApplicationCoordinator | None = None,
    control_token_provider: Callable[[], str | None] | None = None,
) -> FastAPI:
    """Create the thin FastAPI application wrapping one ApplicationCoordinator."""

    if coordinator is not None and coordinator_factory is not None:
        raise ValueError("Provide at most one of coordinator or coordinator_factory")

    allowed_hosts = build_allowed_hosts(server_config)
    allowed_origins = build_allowed_origins(server_config)
    token_provider: Callable[[], str | None] = (
        (lambda: None) if control_token_provider is None else control_token_provider
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_shutdown = False
        active_coordinator = coordinator
        if active_coordinator is None:
            owns_shutdown = True
            if coordinator_factory is not None:
                active_coordinator = await coordinator_factory(runtime_config)
            else:
                active_coordinator = await ApplicationComposition(
                    runtime_config
                ).start()
        app.state.coordinator = active_coordinator
        app.state.control_token_provider = token_provider
        app.state.server_service = ApplicationServerService(
            coordinator=active_coordinator,
            runtime_config=runtime_config,
            server_config=server_config,
        )
        try:
            yield
        finally:
            if owns_shutdown and active_coordinator is not None:
                await active_coordinator.shutdown()

    app = FastAPI(
        title="AtlasRAG Local Application Server",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.control_token_provider = token_provider

    @app.middleware("http")
    async def local_security_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        try:
            validate_host_header(request.headers.get("host"), allowed_hosts)
            validate_origin_and_referer(
                method=request.method,
                origin=request.headers.get("origin"),
                referer=request.headers.get("referer"),
                allowed_origins=allowed_origins,
            )
        except ServerHttpError as error:
            status, payload = translate_exception(error)
            return _json_error_response(status, payload)
        return await call_next(request)

    app.add_middleware(
        _RequestBodyLimitMiddleware,
        max_request_bytes=server_config.max_request_bytes,
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

    return app
