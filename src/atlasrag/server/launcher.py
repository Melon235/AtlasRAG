"""Ownership-safe single-worker Uvicorn process launcher for AtlasRAG."""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import secrets
import signal
import threading
from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any
from uuid import uuid4

import uvicorn
from uvicorn.server import HANDLED_SIGNALS

from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import (
    ConfigurationError,
    RuntimeOwnershipConflictError,
    RuntimeShutdownFailedError,
)
from atlasrag.runtime.composition import ApplicationComposition
from atlasrag.runtime.coordinator import ApplicationCoordinator
from atlasrag.server.app import create_app
from atlasrag.server.auth import (
    CleanupOutcome,
    MetadataCleanupError,
    canonical_state_directory,
    control_token_path,
    prune_shutdown_receipts,
    read_boot_id,
    read_proc_stat_starttime,
    server_discovery_path,
    shutdown_receipt_path,
    unlink_owned_control_token,
    unlink_owned_server_discovery,
    unlink_owned_shutdown_receipt,
    validate_loopback_url,
    write_secure_local_file,
)
from atlasrag.server.config import ServerConfig, validate_loopback_host
from atlasrag.server.events import InvocationEventBus
from atlasrag.server.schemas import ServerDiscoveryRecord, ShutdownReceipt
from atlasrag.server.service import ShutdownOwner

EXIT_OK = 0
EXIT_OPERATIONAL_ERROR = 1
EXIT_USAGE_OR_CONFIG_ERROR = 2
EXIT_OWNERSHIP_CONFLICT = 3
EXIT_FATAL_LIFECYCLE = 4

LauncherCoordinatorFactory = Callable[
    [
        RuntimeConfig,
        InvocationEventBus,
        Callable[[], Awaitable[None] | None],
    ],
    Awaitable[ApplicationCoordinator],
]


def format_loopback_url(host: str, port: int) -> str:
    """Format a validated loopback URL preserving IPv6 brackets."""

    normalized_host = validate_loopback_host(host)
    if ":" in normalized_host and not normalized_host.startswith("["):
        candidate = f"http://[{normalized_host}]:{port}"
    else:
        candidate = f"http://{normalized_host}:{port}"
    return validate_loopback_url(candidate)


def build_uvicorn_config(
    *,
    app: Any,
    runtime_config: RuntimeConfig,
    server_config: ServerConfig,
    log_level: str | None = None,
) -> uvicorn.Config:
    """Build single-worker, reload-disabled, bounded-shutdown Uvicorn config."""

    graceful_seconds = max(
        1, math.ceil(runtime_config.maintenance.shutdown_timeout_seconds)
    )
    resolved_log_level = "info" if log_level is None else log_level
    return uvicorn.Config(
        app=app,
        host=server_config.host,
        port=server_config.port,
        workers=1,
        reload=False,
        lifespan="on",
        timeout_graceful_shutdown=graceful_seconds,
        log_level=resolved_log_level,
        access_log=resolved_log_level != "warning",
    )


class _ManagedUvicornServer(uvicorn.Server):
    """Uvicorn server subclass wiring post-bind discovery and signal-safe shutdown."""

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        on_bound: Callable[[int], None],
        on_begin_shutdown: Callable[[], Awaitable[None]],
        on_signal_exit: Callable[[], None],
        is_coordinator_ready: Callable[[], bool],
        on_signal_before_publish: Callable[[], None],
    ) -> None:
        super().__init__(config)
        self._on_bound = on_bound
        self._on_begin_shutdown = on_begin_shutdown
        self._on_signal_exit = on_signal_exit
        self._is_coordinator_ready = is_coordinator_ready
        self._on_signal_before_publish = on_signal_before_publish
        self._loop: asyncio.AbstractEventLoop | None = None
        self._signals_enabled = True
        self._shutdown_started = False
        self._lifespan_shutdown_ran = False

    def disable_signals(self) -> None:
        self._signals_enabled = False
        self._loop = None

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        """Handle SIGINT/SIGTERM gracefully without re-raising raw process signals."""

        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original_handlers = {
            sig: signal.signal(sig, self.handle_exit) for sig in HANDLED_SIGNALS
        }
        try:
            yield
        finally:
            self._signals_enabled = False
            for sig, handler in original_handlers.items():
                signal.signal(sig, handler)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._captured_signals.append(sig)
        if self.should_exit and sig == signal.SIGINT:
            self.force_exit = True
        else:
            self.should_exit = True
        if not self._signals_enabled:
            return
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._on_signal_exit)
            except RuntimeError:
                pass

    def _has_bound_socket(self, sockets: Sequence[Any] | None) -> bool:
        if sockets:
            return True
        for server in getattr(self, "servers", ()):
            if getattr(server, "sockets", None):
                return True
        return False

    async def startup(self, sockets: list[Any] | None = None) -> None:
        self._loop = asyncio.get_running_loop()
        await super().startup(sockets=sockets)
        lifespan = getattr(self, "lifespan", None)
        lifespan_ok = (
            lifespan is not None
            and not lifespan.startup_failed
            and not lifespan.error_occurred
            and not lifespan.should_exit
        )
        if (
            not self.started
            or not lifespan_ok
            or not self._has_bound_socket(sockets)
            or not self._is_coordinator_ready()
        ):
            await self.ensure_lifespan_shutdown()
            raise SystemExit(EXIT_FATAL_LIFECYCLE)

        if self.should_exit:
            self._on_signal_before_publish()
            return

        bound_port = self._resolve_bound_port(sockets)
        try:
            self._on_bound(bound_port)
        except BaseException:
            await self.shutdown(sockets=sockets)
            raise

    async def shutdown(self, sockets: list[Any] | None = None) -> None:
        if not self._shutdown_started:
            self._shutdown_started = True
            await self._on_begin_shutdown()
        await super().shutdown(sockets=sockets)
        if not self.force_exit and hasattr(self, "lifespan"):
            self._lifespan_shutdown_ran = True

    async def ensure_lifespan_shutdown(self) -> None:
        """Ensure single lifespan/coordinator teardown still executes on force_exit."""

        lifespan = getattr(self, "lifespan", None)
        if lifespan is None or self._lifespan_shutdown_ran:
            return
        self._lifespan_shutdown_ran = True
        if not lifespan.shutdown_event.is_set() and not lifespan.error_occurred:
            await lifespan.shutdown()

    def _resolve_bound_port(self, sockets: Sequence[Any] | None) -> int:
        if sockets:
            sockname = sockets[0].getsockname()
            if isinstance(sockname, tuple) and len(sockname) >= 2:
                return int(sockname[1])
        for server in getattr(self, "servers", ()):
            server_sockets = getattr(server, "sockets", None) or ()
            if server_sockets:
                sockname = server_sockets[0].getsockname()
                if isinstance(sockname, tuple) and len(sockname) >= 2:
                    return int(sockname[1])
        return int(self.config.port)


class ServerLauncher:
    """Own single-instance Uvicorn startup, discovery, shutdown, and receipt lifecycle."""

    def __init__(
        self,
        *,
        runtime_config: RuntimeConfig,
        server_config: ServerConfig,
        coordinator_factory: LauncherCoordinatorFactory | None = None,
        log_level: str | None = None,
    ) -> None:
        self._runtime_config = runtime_config
        self._server_config = server_config
        self._coordinator_factory = coordinator_factory
        self._log_level = log_level
        self._state_dir = canonical_state_directory(server_config)
        self._event_bus = InvocationEventBus()
        self._server_instance_id = uuid4().hex
        self._control_token: str | None = None
        self._coordinator: ApplicationCoordinator | None = None
        self._discovery_record: ServerDiscoveryRecord | None = None
        self._shutdown_receipt: ShutdownReceipt | None = None
        self._server_published = False
        self._receipt_written = False
        self._settled_cleanly = False
        self._signal_before_publish = False
        self._cleanup_closed = False
        self._startup_exit_code: int | None = None
        self._shutdown_failed = False
        self._t0_discovery_cleanup_error: BaseException | None = None
        self._uvicorn_server: _ManagedUvicornServer | None = None
        self._app: Any = None
        self._signal_begin_task: asyncio.Task[None] | None = None

    @property
    def server_instance_id(self) -> str:
        return self._server_instance_id

    @property
    def state_directory(self) -> Path:
        return self._state_dir

    @property
    def discovery_record(self) -> ServerDiscoveryRecord | None:
        return self._discovery_record

    @property
    def shutdown_receipt(self) -> ShutdownReceipt | None:
        return self._shutdown_receipt

    @property
    def uvicorn_server(self) -> _ManagedUvicornServer | None:
        return self._uvicorn_server

    def _safe_t0_unlink_discovery(self) -> None:
        if self._cleanup_closed:
            return
        try:
            outcome = unlink_owned_server_discovery(
                self._state_dir, self._server_instance_id
            )
            if outcome is CleanupOutcome.FOREIGN_INSTANCE:
                self._t0_discovery_cleanup_error = MetadataCleanupError(
                    "foreign server.json observed at shutdown initiation"
                )
        except MetadataCleanupError as error:
            self._t0_discovery_cleanup_error = error

    async def _on_shutdown_settled(self) -> None:
        """Verify operations settlement and clean owned metadata strictly under lock."""

        service = (
            getattr(self._app.state, "server_service", None)
            if self._app is not None
            else None
        )
        if service is not None:
            ops_settled = await service.operations.wait_active(
                timeout=self._runtime_config.maintenance.shutdown_timeout_seconds
            )
            if not ops_settled:
                raise RuntimeShutdownFailedError

        disc_outcome = unlink_owned_server_discovery(
            self._state_dir, self._server_instance_id
        )
        if disc_outcome is CleanupOutcome.FOREIGN_INSTANCE:
            raise MetadataCleanupError(
                "refusing clean receipt with foreign server.json present"
            )
        if self._control_token is not None:
            token_outcome = unlink_owned_control_token(
                self._state_dir, self._control_token
            )
            if token_outcome is CleanupOutcome.FOREIGN_INSTANCE:
                raise MetadataCleanupError(
                    "refusing clean receipt with foreign control.token present"
                )
        if (
            self._t0_discovery_cleanup_error is not None
            and disc_outcome is not CleanupOutcome.REMOVED
        ):
            raise self._t0_discovery_cleanup_error

        if self._server_published:
            receipt = ShutdownReceipt(
                server_instance_id=self._server_instance_id,
                pid=os.getpid(),
                status="CLEAN_SHUTDOWN",
                completed_at=datetime.now(UTC),
            )
            write_secure_local_file(
                shutdown_receipt_path(self._state_dir, self._server_instance_id),
                receipt.model_dump_json(indent=2),
            )
            self._shutdown_receipt = receipt
            self._receipt_written = True
            self._settled_cleanly = True
        else:
            # Startup/bind failure or signal-before-publish: metadata cleaned, no clean receipt.
            self._settled_cleanly = self._signal_before_publish

        self._cleanup_closed = True

    async def _start_coordinator(
        self, runtime_config: RuntimeConfig
    ) -> ApplicationCoordinator:
        try:
            if self._coordinator_factory is not None:
                coordinator = await self._coordinator_factory(
                    runtime_config,
                    self._event_bus,
                    self._on_shutdown_settled,
                )
            else:
                coordinator = await ApplicationComposition(runtime_config).start(
                    observer=self._event_bus,
                    on_shutdown_settled=self._on_shutdown_settled,
                )
        except RuntimeOwnershipConflictError:
            self._startup_exit_code = EXIT_OWNERSHIP_CONFLICT
            raise
        except ConfigurationError:
            self._startup_exit_code = EXIT_USAGE_OR_CONFIG_ERROR
            raise
        except BaseException as error:
            if isinstance(error.__cause__, RuntimeOwnershipConflictError):
                self._startup_exit_code = EXIT_OWNERSHIP_CONFLICT
            else:
                self._startup_exit_code = EXIT_FATAL_LIFECYCLE
            raise

        self._coordinator = coordinator
        token = secrets.token_hex(32)
        self._control_token = token
        try:
            prune_shutdown_receipts(self._state_dir, keep=8)
            write_secure_local_file(control_token_path(self._state_dir), token)
        except BaseException:
            self._startup_exit_code = EXIT_FATAL_LIFECYCLE
            try:
                await coordinator.shutdown()
            except BaseException:
                pass
            raise
        return coordinator

    def _on_bound(self, bound_port: int) -> None:
        pid = os.getpid()
        url = format_loopback_url(self._server_config.host, bound_port)
        record = ServerDiscoveryRecord(
            url=url,
            host=self._server_config.host,
            port=bound_port,
            pid=pid,
            process_start_ticks=read_proc_stat_starttime(pid),
            boot_id=read_boot_id(),
            server_instance_id=self._server_instance_id,
            started_at=datetime.now(UTC),
            shutdown_timeout_seconds=self._runtime_config.maintenance.shutdown_timeout_seconds,
        )
        write_secure_local_file(
            server_discovery_path(self._state_dir),
            record.model_dump_json(indent=2),
        )
        self._discovery_record = record
        self._server_published = True

    def _on_http_shutdown_requested(self) -> None:
        self._safe_t0_unlink_discovery()
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True

    def _on_signal_exit(self) -> None:
        if self._cleanup_closed:
            return
        self._safe_t0_unlink_discovery()
        coordinator = self._coordinator
        if coordinator is not None and (
            self._signal_begin_task is None or self._signal_begin_task.done()
        ):
            self._signal_begin_task = asyncio.create_task(
                coordinator.gate.begin_shutdown()
            )

    def _on_signal_before_publish(self) -> None:
        self._signal_before_publish = True

    async def _on_begin_shutdown(self) -> None:
        if self._cleanup_closed:
            return
        if self._coordinator is not None:
            await self._coordinator.gate.begin_shutdown()
        self._safe_t0_unlink_discovery()

    async def serve(self, *, sockets: list[Any] | None = None) -> int:
        """Start the server, serve until shutdown, and return the exit code."""

        shutdown_owner = ShutdownOwner(
            pid=os.getpid(),
            server_instance_id=self._server_instance_id,
            request_shutdown=self._on_http_shutdown_requested,
        )
        app = create_app(
            runtime_config=self._runtime_config,
            server_config=self._server_config,
            coordinator_factory=self._start_coordinator,
            control_token_provider=lambda: self._control_token,
            event_bus=self._event_bus,
            shutdown_owner=shutdown_owner,
        )
        self._app = app
        config = build_uvicorn_config(
            app=app,
            runtime_config=self._runtime_config,
            server_config=self._server_config,
            log_level=self._log_level,
        )
        server = _ManagedUvicornServer(
            config,
            on_bound=self._on_bound,
            on_begin_shutdown=self._on_begin_shutdown,
            on_signal_exit=self._on_signal_exit,
            is_coordinator_ready=lambda: self._coordinator is not None,
            on_signal_before_publish=self._on_signal_before_publish,
        )
        self._uvicorn_server = server

        startup_system_exit = False
        try:
            await server.serve(sockets=sockets)
        except SystemExit:
            startup_system_exit = True
        except BaseException:
            self._shutdown_failed = True
        finally:
            server.disable_signals()
            if (
                self._signal_begin_task is not None
                and not self._signal_begin_task.done()
            ):
                self._signal_begin_task.cancel()
                try:
                    await self._signal_begin_task
                except BaseException:
                    pass
            try:
                await server.ensure_lifespan_shutdown()
            except BaseException:
                self._shutdown_failed = True
            self._cleanup_closed = True

        lifespan = getattr(server, "lifespan", None)
        if lifespan is not None and (
            lifespan.error_occurred or lifespan.shutdown_failed
        ):
            self._shutdown_failed = True

        # WPD-F03: if ownership.release() failed AFTER on_shutdown_settled wrote receipt,
        # coordinator._shutdown_done remains False (lock never released); revoke the
        # receipt so no false CLEAN_SHUTDOWN exists, and exit 4.
        if (
            self._receipt_written
            and self._coordinator is not None
            and not getattr(self._coordinator, "_shutdown_done", False)
        ):
            self._shutdown_failed = True
            self._settled_cleanly = False
            self._shutdown_receipt = None
            try:
                unlink_owned_shutdown_receipt(self._state_dir, self._server_instance_id)
            except BaseException:
                pass

        if self._startup_exit_code is not None:
            return self._startup_exit_code
        if startup_system_exit:
            return EXIT_FATAL_LIFECYCLE
        if self._shutdown_failed:
            return EXIT_FATAL_LIFECYCLE
        if self._settled_cleanly:
            return EXIT_OK
        return EXIT_FATAL_LIFECYCLE

    def run(self, *, sockets: list[Any] | None = None) -> int:
        """Execute the managed single-worker Uvicorn server synchronously."""

        return asyncio.run(self.serve(sockets=sockets))
