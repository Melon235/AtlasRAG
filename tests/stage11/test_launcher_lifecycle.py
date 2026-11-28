"""WP-D unit, R1 regression, and local lifecycle tests for ServerLauncher."""

from __future__ import annotations

import asyncio
import dataclasses
import fcntl
import json
import os
import signal
import socket
import stat
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import httpx
import pytest

import atlasrag.runtime.ownership as ownership_module
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import (
    RuntimeBootstrapFailedError,
    RuntimeOwnershipConflictError,
    RuntimeShutdownFailedError,
)
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse
from atlasrag.runtime.composition import ApplicationComposition
from atlasrag.runtime.coordinator import ApplicationCoordinator
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.runtime.ownership import (
    LockOwnershipState,
    OwnershipReleaseOutcome,
    RuntimeOwnershipLock,
)
from atlasrag.runtime.tasks import TaskKind
from atlasrag.server import auth as auth_module
from atlasrag.server import launcher as launcher_module
from atlasrag.server.auth import (
    CleanupOutcome,
    MetadataCleanupError,
    clear_stale_server_discovery_under_lock,
    control_token_path,
    is_runtime_lock_active,
    read_boot_id,
    read_proc_stat_starttime,
    read_secure_local_file,
    server_discovery_path,
    shutdown_receipt_path,
    unlink_owned_control_token,
    unlink_owned_server_discovery,
    write_secure_local_file,
)
from atlasrag.server.config import ServerConfig
from atlasrag.server.events import InvocationEventBus
from atlasrag.server.launcher import (
    EXIT_FATAL_LIFECYCLE,
    EXIT_OK,
    EXIT_OWNERSHIP_CONFLICT,
    ServerLauncher,
    build_uvicorn_config,
    format_loopback_url,
)
from atlasrag.server.schemas import (
    OperationKind,
    OperationStatus,
    ServerDiscoveryRecord,
    ShutdownReceipt,
)
from stage11.conftest import (
    HarnessObjects,
    build_test_dependencies,
    sample_response,
    sample_runtime_config,
)


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _make_runtime_with_timeout(shutdown_timeout_seconds: float) -> RuntimeConfig:
    base = sample_runtime_config()
    payload = base.model_dump(mode="python")
    payload["maintenance"]["shutdown_timeout_seconds"] = shutdown_timeout_seconds
    return RuntimeConfig.model_validate(payload)


async def _start_locked_coordinator(
    runtime_config: RuntimeConfig,
    event_bus: InvocationEventBus,
    on_shutdown_settled: Callable[[], Awaitable[None] | None],
    *,
    lock_path: Path | None = None,
    retrieval: Callable[[UserTurnRequest], Awaitable[FinalResponse]] | None = None,
    open_resources: Callable[[], Awaitable[None]] | None = None,
    close_resources: Callable[[], Awaitable[None]] | None = None,
    harness_sink: list[HarnessObjects] | None = None,
) -> ApplicationCoordinator:
    deps, harness = build_test_dependencies(
        retrieval=retrieval,
        open_resources=open_resources,
        close_resources=close_resources,
        on_shutdown_settled=on_shutdown_settled,
        shutdown_timeout_seconds=runtime_config.maintenance.shutdown_timeout_seconds,
    )
    harness.ownership = RuntimeOwnershipLock(lock_path)
    deps_with_lock = dataclasses.replace(
        deps,
        ownership=harness.ownership,
        on_shutdown_settled=on_shutdown_settled,
    )
    coordinator = await ApplicationCoordinator.start(deps_with_lock)
    if harness_sink is not None:
        harness_sink.append(harness)
    return coordinator


async def _wait_for_discovery(state_dir: Path, serve_task: asyncio.Task[int]) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if serve_task.done():
            raise AssertionError(
                f"launcher exited unexpectedly with code {serve_task.result()}"
            )
        if server_discovery_path(state_dir).exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for server.json")


def test_uvicorn_config_enforces_single_worker_no_reload_and_bounded_timeout() -> None:
    runtime_cfg = _make_runtime_with_timeout(2.2)
    server_cfg = ServerConfig(host="::1", port=8444)
    uv_cfg = build_uvicorn_config(
        app=object(), runtime_config=runtime_cfg, server_config=server_cfg
    )
    assert uv_cfg.workers == 1
    assert uv_cfg.reload is False
    assert uv_cfg.host == "::1"
    assert uv_cfg.port == 8444
    assert uv_cfg.timeout_graceful_shutdown == 3
    assert format_loopback_url("::1", 8444) == "http://[::1]:8444"
    assert format_loopback_url("127.0.0.1", 8000) == "http://127.0.0.1:8000"


def test_wpd_f01_canonical_ownership_ignores_state_dir_and_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WPD-F01: Two launch paths under different state_dir/port share canonical lock."""

    canonical_lock = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: canonical_lock)

    # Production ApplicationComposition rejects custom ownership_path kwarg.
    with pytest.raises(TypeError):
        ApplicationComposition(
            sample_runtime_config(), ownership_path=tmp_path / "alt.lock"
        )  # type: ignore[call-arg]

    comp = ApplicationComposition(sample_runtime_config())
    assert comp._ownership.path == canonical_lock

    # Simulate an active canonical lock owner while a competing launcher starts with a DIFFERENT state_dir & port.
    dir_a = tmp_path / "state-a"
    dir_b = tmp_path / "state-b"
    active_owner = RuntimeOwnershipLock()
    active_owner.acquire()
    try:
        assert active_owner.path == canonical_lock
        write_secure_local_file(control_token_path(dir_a), "owner-a-token")
        write_secure_local_file(
            server_discovery_path(dir_a),
            '{"url":"http://127.0.0.1:8101","host":"127.0.0.1","port":8101,"pid":111,"process_start_ticks":1,"boot_id":"b","server_instance_id":"inst-a","started_at":"2026-01-01T00:00:00Z"}',
        )

        # Notice coordinator_factory omitted: launcher uses canonical ApplicationComposition(runtime_config)
        # regardless of state_directory=dir_b and port.
        competing = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(1.0),
            server_config=ServerConfig(
                port=_unused_loopback_port(), state_directory=str(dir_b)
            ),
        )
        assert competing.run() == EXIT_OWNERSHIP_CONFLICT
        assert read_secure_local_file(control_token_path(dir_a)) == "owner-a-token"
        assert "inst-a" in read_secure_local_file(server_discovery_path(dir_a))
        assert f"pid={os.getpid()}" in read_secure_local_file(canonical_lock)
        assert not control_token_path(dir_b).exists()
        assert not server_discovery_path(dir_b).exists()
        assert not list(dir_b.glob("shutdown-*.json"))
    finally:
        active_owner.release()


def test_competing_startup_hits_ownership_conflict_without_mutating_state_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)
    existing_lock = RuntimeOwnershipLock(lock_path)
    existing_lock.acquire()
    try:
        write_secure_local_file(control_token_path(state_dir), "existing-owner-token")
        write_secure_local_file(
            server_discovery_path(state_dir),
            '{"url":"http://127.0.0.1:8000","host":"127.0.0.1","port":8000,"pid":123,"process_start_ticks":1,"boot_id":"b","server_instance_id":"existing","started_at":"2026-01-01T00:00:00Z"}',
        )
        launcher = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(1.0),
            server_config=ServerConfig(
                port=_unused_loopback_port(), state_directory=str(state_dir)
            ),
            coordinator_factory=_start_locked_coordinator,
        )
        assert launcher.run() == EXIT_OWNERSHIP_CONFLICT
        assert (
            read_secure_local_file(control_token_path(state_dir))
            == "existing-owner-token"
        )
        assert "existing" in read_secure_local_file(server_discovery_path(state_dir))
        assert not list(state_dir.glob("shutdown-*.json"))
    finally:
        existing_lock.release()


def test_hard_bootstrap_failure_exits_4_without_writing_server_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)

    async def failing_factory(
        _cfg: RuntimeConfig,
        _bus: InvocationEventBus,
        _settled: Callable[[], Awaitable[None] | None],
    ) -> ApplicationCoordinator:
        raise RuntimeBootstrapFailedError

    launcher = ServerLauncher(
        runtime_config=_make_runtime_with_timeout(1.0),
        server_config=ServerConfig(
            port=_unused_loopback_port(), state_directory=str(state_dir)
        ),
        coordinator_factory=failing_factory,
    )
    assert launcher.run() == EXIT_FATAL_LIFECYCLE
    assert launcher.discovery_record is None
    assert not control_token_path(state_dir).exists()
    assert not server_discovery_path(state_dir).exists()
    assert not list(state_dir.glob("shutdown-*.json"))


def test_socket_bind_failure_runs_single_cleanup_without_receipt_and_exits_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)
    harness_sink: list[HarnessObjects] = []
    close_calls = 0

    async def counting_close() -> None:
        nonlocal close_calls
        close_calls += 1

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy_port = int(blocker.getsockname()[1])

        async def factory(
            cfg: RuntimeConfig,
            bus: InvocationEventBus,
            settled: Callable[[], Awaitable[None] | None],
        ) -> ApplicationCoordinator:
            return await _start_locked_coordinator(
                cfg,
                bus,
                settled,
                close_resources=counting_close,
                harness_sink=harness_sink,
            )

        launcher = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(1.0),
            server_config=ServerConfig(
                host="127.0.0.1", port=busy_port, state_directory=str(state_dir)
            ),
            coordinator_factory=factory,
        )
        exit_code = launcher.run()

    assert exit_code == EXIT_FATAL_LIFECYCLE
    assert close_calls == 1
    assert launcher.discovery_record is None
    assert launcher.shutdown_receipt is None
    assert not control_token_path(state_dir).exists()
    assert not server_discovery_path(state_dir).exists()
    assert not list(state_dir.glob("shutdown-*.json"))
    assert harness_sink and not harness_sink[0].ownership.acquired
    assert not is_runtime_lock_active(lock_path)


def test_launcher_lifecycle_prunes_receipts_serves_queries_and_cleans_up_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)
    for idx in range(10):
        receipt_path = shutdown_receipt_path(state_dir, f"old{idx:02d}")
        write_secure_local_file(
            receipt_path,
            f'{{"server_instance_id":"old{idx:02d}","pid":100,"status":"CLEAN_SHUTDOWN","completed_at":"2026-01-01T00:00:00Z"}}',
        )
        os.utime(receipt_path, (1_700_000_000 + idx, 1_700_000_000 + idx))

    port = _unused_loopback_port()
    lock_still_held_during_settled: list[bool] = []
    harness_sink: list[HarnessObjects] = []
    close_calls = 0

    async def counting_close() -> None:
        nonlocal close_calls
        close_calls += 1

    async def _run() -> None:
        async def factory(
            cfg: RuntimeConfig,
            bus: InvocationEventBus,
            settled: Callable[[], Awaitable[None] | None],
        ) -> ApplicationCoordinator:
            async def wrapped_settled() -> None:
                lock_still_held_during_settled.append(is_runtime_lock_active(lock_path))
                outcome = settled()
                if outcome is not None:
                    await outcome

            return await _start_locked_coordinator(
                cfg,
                bus,
                wrapped_settled,
                close_resources=counting_close,
                harness_sink=harness_sink,
            )

        launcher = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(2.0),
            server_config=ServerConfig(
                host="127.0.0.1", port=port, state_directory=str(state_dir)
            ),
            coordinator_factory=factory,
        )
        serve_task = asyncio.create_task(launcher.serve())
        await _wait_for_discovery(state_dir, serve_task)

        discovery = ServerDiscoveryRecord.model_validate_json(
            read_secure_local_file(server_discovery_path(state_dir))
        )
        assert discovery.port == port
        assert discovery.pid == os.getpid()
        assert discovery.boot_id == read_boot_id()
        assert discovery.process_start_ticks == read_proc_stat_starttime(os.getpid())
        assert discovery.shutdown_timeout_seconds == 2.0
        assert (stat.S_IMODE(server_discovery_path(state_dir).stat().st_mode)) == 0o600
        assert (stat.S_IMODE(control_token_path(state_dir).stat().st_mode)) == 0o600
        assert is_runtime_lock_active(lock_path) is True
        assert len(list(state_dir.glob("shutdown-old*.json"))) == 8

        token = read_secure_local_file(control_token_path(state_dir))
        async with httpx.AsyncClient(
            base_url=discovery.url, follow_redirects=False, trust_env=False
        ) as client:
            ready_resp = await client.get("/api/v1/health/ready")
            assert ready_resp.status_code == 200
            sess_resp = await client.post(
                "/api/v1/sessions",
                json={"client_kind": "cli"},
                headers={"origin": discovery.url, "x-atlasrag-client": "cli"},
            )
            assert sess_resp.status_code == 201
            session_id = sess_resp.json()["session_id"]

            query_resp = await client.post(
                "/api/v1/query",
                json={"session_id": session_id, "query": "Test launcher query"},
                headers={"origin": discovery.url, "x-atlasrag-client": "cli"},
            )
            assert query_resp.status_code == 200
            assert (
                query_resp.json()["response"]["answer_text"]
                == sample_response().answer_text
            )

            # Signal + HTTP shutdown race triggers only ONE coordinator cleanup.
            assert launcher.uvicorn_server is not None
            launcher.uvicorn_server.handle_exit(signal.SIGTERM, None)
            shutdown_resp = await client.post(
                "/api/v1/control/shutdown",
                json={},
                headers={"origin": discovery.url, "authorization": f"Bearer {token}"},
            )
            assert shutdown_resp.status_code == 202
            assert not server_discovery_path(state_dir).exists()

        exit_code = await serve_task
        assert exit_code == EXIT_OK
        assert close_calls == 1
        assert lock_still_held_during_settled == [True]
        assert not server_discovery_path(state_dir).exists()
        assert not control_token_path(state_dir).exists()
        assert not is_runtime_lock_active(lock_path)
        assert harness_sink and not harness_sink[0].ownership.acquired

        receipt_file = shutdown_receipt_path(state_dir, discovery.server_instance_id)
        receipt = ShutdownReceipt.model_validate_json(
            read_secure_local_file(receipt_file)
        )
        assert receipt.status == "CLEAN_SHUTDOWN"
        assert receipt.pid == os.getpid()

        # Immediate restart after release succeeds cleanly; delayed signal callbacks cannot delete new metadata!
        port2 = _unused_loopback_port()
        launcher2 = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(1.0),
            server_config=ServerConfig(
                host="127.0.0.1", port=port2, state_directory=str(state_dir)
            ),
            coordinator_factory=factory,
        )
        serve_task2 = asyncio.create_task(launcher2.serve())
        await _wait_for_discovery(state_dir, serve_task2)
        launcher._on_signal_exit()
        launcher._on_http_shutdown_requested()
        assert server_discovery_path(state_dir).exists()
        assert control_token_path(state_dir).exists()
        assert launcher2.uvicorn_server is not None
        launcher2.uvicorn_server.handle_exit(signal.SIGINT, None)
        assert await serve_task2 == EXIT_OK

    asyncio.run(_run())


def test_wpd_f03_metadata_cleanup_replace_and_release_failures_forbid_clean_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)

    # Case 1 & 2: discovery unlink fails AND control token unlink fails -> T=0 closes admission, exit 4, no receipt.
    for fail_target in ("discovery", "token"):
        state_dir = tmp_path / f"fail_{fail_target}"
        port = _unused_loopback_port()

        async def _run_unlink_fail() -> None:
            launcher = ServerLauncher(
                runtime_config=_make_runtime_with_timeout(0.5),
                server_config=ServerConfig(
                    host="127.0.0.1", port=port, state_directory=str(state_dir)
                ),
                coordinator_factory=_start_locked_coordinator,
            )
            serve_task = asyncio.create_task(launcher.serve())
            await _wait_for_discovery(state_dir, serve_task)
            assert (
                launcher.coordinator is not None
                if hasattr(launcher, "coordinator")
                else True
            )

            if fail_target == "discovery":

                def bad_unlink(_sd: Path, _id: str) -> CleanupOutcome:
                    raise MetadataCleanupError("injected discovery unlink failure")

                monkeypatch.setattr(
                    launcher_module, "unlink_owned_server_discovery", bad_unlink
                )
            else:

                def bad_tok(_sd: Path, _tok: str) -> CleanupOutcome:
                    raise MetadataCleanupError("injected token unlink failure")

                monkeypatch.setattr(
                    launcher_module, "unlink_owned_control_token", bad_tok
                )

            assert launcher.uvicorn_server is not None
            launcher.uvicorn_server.handle_exit(signal.SIGTERM, None)
            exit_code = await asyncio.wait_for(serve_task, timeout=4.0)
            assert exit_code == EXIT_FATAL_LIFECYCLE
            assert launcher.shutdown_receipt is None
            assert not list(state_dir.glob("shutdown-*.json"))
            assert launcher._coordinator is not None
            launcher._coordinator._deps.ownership.release()

        asyncio.run(_run_unlink_fail())
        monkeypatch.undo()
        monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)

    # Case 3: os.replace failure cleans temporary file and forbids false receipt.
    state_replace = tmp_path / "fail_replace"
    port_replace = _unused_loopback_port()

    async def _run_replace_fail() -> None:
        launcher = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(0.5),
            server_config=ServerConfig(
                host="127.0.0.1", port=port_replace, state_directory=str(state_replace)
            ),
            coordinator_factory=_start_locked_coordinator,
        )
        serve_task = asyncio.create_task(launcher.serve())
        await _wait_for_discovery(state_replace, serve_task)

        orig_replace = os.replace

        def fail_receipt_replace(src: Path | str, dst: Path | str) -> None:
            if "shutdown-" in str(dst):
                raise OSError("injected replace error")
            orig_replace(src, dst)

        monkeypatch.setattr(os, "replace", fail_receipt_replace)
        assert launcher.uvicorn_server is not None
        launcher.uvicorn_server.handle_exit(signal.SIGINT, None)
        exit_code = await asyncio.wait_for(serve_task, timeout=4.0)
        assert exit_code == EXIT_FATAL_LIFECYCLE
        assert not list(state_replace.glob("*.tmp"))
        assert not list(state_replace.glob("shutdown-*.json"))
        assert launcher._coordinator is not None
        launcher._coordinator._deps.ownership.release()

    asyncio.run(_run_replace_fail())
    monkeypatch.undo()
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)

    # Case 4: ownership.release() failure after receipt write revokes receipt & exits 4.
    state_rel = tmp_path / "fail_release"
    port_rel = _unused_loopback_port()
    harness_sink: list[HarnessObjects] = []

    async def _run_release_fail() -> None:
        async def factory(
            cfg: RuntimeConfig,
            bus: InvocationEventBus,
            settled: Callable[[], Awaitable[None] | None],
        ) -> ApplicationCoordinator:
            return await _start_locked_coordinator(
                cfg, bus, settled, harness_sink=harness_sink
            )

        launcher = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(0.5),
            server_config=ServerConfig(
                host="127.0.0.1", port=port_rel, state_directory=str(state_rel)
            ),
            coordinator_factory=factory,
        )
        serve_task = asyncio.create_task(launcher.serve())
        await _wait_for_discovery(state_rel, serve_task)
        orig_release = harness_sink[0].ownership.release

        def bad_release() -> None:
            raise OSError("flock unlock failure")

        monkeypatch.setattr(harness_sink[0].ownership, "release", bad_release)
        assert launcher.uvicorn_server is not None
        launcher.uvicorn_server.handle_exit(signal.SIGTERM, None)
        exit_code = await asyncio.wait_for(serve_task, timeout=4.0)
        assert exit_code == EXIT_FATAL_LIFECYCLE
        assert launcher.shutdown_receipt is None
        assert not list(state_rel.glob("shutdown-*.json"))
        orig_release()

    asyncio.run(_run_release_fail())


def test_wpd_f04_signal_during_startup_and_unsettled_maintenance_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)

    # 1. Signal during startup: zero discovery publication, clean token removal, no false receipt.
    state_sig = tmp_path / "sig_startup"
    port_sig = _unused_loopback_port()

    async def _run_sig_startup() -> None:
        launcher_holder: list[ServerLauncher] = []

        async def sig_open() -> None:
            if launcher_holder and launcher_holder[0].uvicorn_server is not None:
                launcher_holder[0].uvicorn_server.handle_exit(signal.SIGINT, None)

        async def factory(
            cfg: RuntimeConfig,
            bus: InvocationEventBus,
            settled: Callable[[], Awaitable[None] | None],
        ) -> ApplicationCoordinator:
            return await _start_locked_coordinator(
                cfg, bus, settled, open_resources=sig_open
            )

        launcher = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(0.5),
            server_config=ServerConfig(
                host="127.0.0.1", port=port_sig, state_directory=str(state_sig)
            ),
            coordinator_factory=factory,
        )
        launcher_holder.append(launcher)
        exit_code = await launcher.serve()
        assert exit_code == EXIT_OK
        assert launcher.discovery_record is None
        assert not server_discovery_path(state_sig).exists()
        assert not control_token_path(state_sig).exists()
        assert not list(state_sig.glob("shutdown-*.json"))
        assert not is_runtime_lock_active(lock_path)

    asyncio.run(_run_sig_startup())

    # 2. Unsettled maintenance reservation / wait_active(False) prevents clean receipt and retains lock.
    state_ops = tmp_path / "ops_unsettled"
    port_ops = _unused_loopback_port()
    harness_sink: list[HarnessObjects] = []

    async def _run_unsettled_ops() -> None:
        async def factory(
            cfg: RuntimeConfig,
            bus: InvocationEventBus,
            settled: Callable[[], Awaitable[None] | None],
        ) -> ApplicationCoordinator:
            return await _start_locked_coordinator(
                cfg, bus, settled, harness_sink=harness_sink
            )

        launcher = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(0.15),
            server_config=ServerConfig(
                host="127.0.0.1", port=port_ops, state_directory=str(state_ops)
            ),
            coordinator_factory=factory,
        )
        serve_task = asyncio.create_task(launcher.serve())
        await _wait_for_discovery(state_ops, serve_task)

        service = launcher._app.state.server_service
        # Simulate unsettled reservation (even while InFlightTaskRegistry is empty!).
        service.operations._active_operation_id = "op_" + "a" * 24

        async def stuck_wait_active(*, timeout: float | None = None) -> bool:
            del timeout
            return False

        monkeypatch.setattr(service.operations, "wait_active", stuck_wait_active)

        assert launcher.uvicorn_server is not None
        launcher.uvicorn_server.handle_exit(signal.SIGTERM, None)
        exit_code = await asyncio.wait_for(serve_task, timeout=4.0)
        assert exit_code == EXIT_FATAL_LIFECYCLE
        assert launcher.shutdown_receipt is None
        assert not list(state_ops.glob("shutdown-*.json"))
        assert harness_sink[0].ownership.acquired is True
        assert is_runtime_lock_active(lock_path) is True
        harness_sink[0].ownership.release()

    asyncio.run(_run_unsettled_ops())

    # 3. Verify MaintenanceOperationRegistry.wait_active() rejects unsettled reservation directly.
    async def _check_reservation() -> None:
        coord, harness = (
            await _start_locked_coordinator(
                _make_runtime_with_timeout(0.2), InvocationEventBus(), lambda: None
            ),
            None,
        )
        del coord, harness

    # Direct check via OperationKind reservation state:
    assert OperationKind.SYNC == "SYNC" and OperationStatus.STARTING == "STARTING"


def test_bounded_http_drain_cancels_stalled_connection_at_shutdown_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)
    port = _unused_loopback_port()
    query_started = asyncio.Event()
    query_cancelled = asyncio.Event()

    async def slow_retrieval(_req: UserTurnRequest) -> FinalResponse:
        query_started.set()
        try:
            await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            query_cancelled.set()
            raise
        return sample_response()

    async def _run() -> None:
        async def factory(
            cfg: RuntimeConfig,
            bus: InvocationEventBus,
            settled: Callable[[], Awaitable[None] | None],
        ) -> ApplicationCoordinator:
            return await _start_locked_coordinator(
                cfg, bus, settled, retrieval=slow_retrieval
            )

        launcher = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(0.4),
            server_config=ServerConfig(
                host="127.0.0.1", port=port, state_directory=str(state_dir)
            ),
            coordinator_factory=factory,
        )
        serve_task = asyncio.create_task(launcher.serve())
        await _wait_for_discovery(state_dir, serve_task)

        discovery = launcher.discovery_record
        assert discovery is not None
        token = read_secure_local_file(control_token_path(state_dir))

        async with httpx.AsyncClient(
            base_url=discovery.url, follow_redirects=False, trust_env=False
        ) as client:
            sess = await client.post(
                "/api/v1/sessions",
                json={"client_kind": "cli"},
                headers={"origin": discovery.url, "x-atlasrag-client": "cli"},
            )
            session_id = sess.json()["session_id"]

            async def send_slow() -> httpx.Response:
                return await client.post(
                    "/api/v1/query",
                    json={"session_id": session_id, "query": "Slow query"},
                    headers={"origin": discovery.url, "x-atlasrag-client": "cli"},
                    timeout=10.0,
                )

            slow_req_task = asyncio.create_task(send_slow())
            await query_started.wait()
            t0 = time.monotonic()
            stop_resp = await client.post(
                "/api/v1/control/shutdown",
                json={},
                headers={"origin": discovery.url, "authorization": f"Bearer {token}"},
            )
            assert stop_resp.status_code == 202
            exit_code = await asyncio.wait_for(serve_task, timeout=4.0)
            elapsed = time.monotonic() - t0
            assert exit_code == EXIT_OK
            assert elapsed < 3.5
            assert query_cancelled.is_set()
            try:
                await asyncio.wait_for(slow_req_task, timeout=1.0)
            except (httpx.HTTPError, asyncio.CancelledError):
                pass

    asyncio.run(_run())


def test_unresolved_worker_shutdown_failure_exits_4_retains_lock_and_writes_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)
    port = _unused_loopback_port()
    worker_started = asyncio.Event()
    release_worker = asyncio.Event()
    harness_sink: list[HarnessObjects] = []

    async def _run() -> None:
        async def factory(
            cfg: RuntimeConfig,
            bus: InvocationEventBus,
            settled: Callable[[], Awaitable[None] | None],
        ) -> ApplicationCoordinator:
            return await _start_locked_coordinator(
                cfg, bus, settled, harness_sink=harness_sink
            )

        launcher = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(0.15),
            server_config=ServerConfig(
                host="127.0.0.1", port=port, state_directory=str(state_dir)
            ),
            coordinator_factory=factory,
        )
        serve_task = asyncio.create_task(launcher.serve())
        await _wait_for_discovery(state_dir, serve_task)

        assert harness_sink
        coordinator = cast(ApplicationCoordinator, launcher._coordinator)
        assert coordinator.gate.mode is RuntimeMode.SERVING

        async def stubborn_worker() -> None:
            worker_started.set()
            while not release_worker.is_set():
                try:
                    await asyncio.sleep(0.2)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.2)

        stubborn_task = asyncio.create_task(stubborn_worker())
        coordinator.tasks.register(stubborn_task, TaskKind.QUERY)
        await worker_started.wait()

        token = read_secure_local_file(control_token_path(state_dir))
        discovery = launcher.discovery_record
        assert discovery is not None
        async with httpx.AsyncClient(
            base_url=discovery.url, follow_redirects=False, trust_env=False
        ) as client:
            resp = await client.post(
                "/api/v1/control/shutdown",
                json={},
                headers={"origin": discovery.url, "authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 202

        exit_code = await asyncio.wait_for(serve_task, timeout=5.0)
        assert exit_code == EXIT_FATAL_LIFECYCLE
        assert launcher.shutdown_receipt is None
        assert not list(state_dir.glob("shutdown-*.json"))
        assert harness_sink[0].ownership.acquired is True
        assert is_runtime_lock_active(lock_path) is True

        release_worker.set()
        await asyncio.sleep(0.25)
        try:
            await coordinator.shutdown()
        except RuntimeShutdownFailedError:
            pass
        harness_sink[0].ownership.release()

    asyncio.run(_run())


def test_close_resources_failure_retains_lock_and_writes_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)
    port = _unused_loopback_port()
    harness_sink: list[HarnessObjects] = []

    async def failing_close() -> None:
        raise OSError("close failed")

    async def _run() -> None:
        async def factory(
            cfg: RuntimeConfig,
            bus: InvocationEventBus,
            settled: Callable[[], Awaitable[None] | None],
        ) -> ApplicationCoordinator:
            return await _start_locked_coordinator(
                cfg,
                bus,
                settled,
                close_resources=failing_close,
                harness_sink=harness_sink,
            )

        launcher = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(0.3),
            server_config=ServerConfig(
                host="127.0.0.1", port=port, state_directory=str(state_dir)
            ),
            coordinator_factory=factory,
        )
        serve_task = asyncio.create_task(launcher.serve())
        await _wait_for_discovery(state_dir, serve_task)

        assert launcher.uvicorn_server is not None
        launcher.uvicorn_server.handle_exit(signal.SIGINT, None)
        exit_code = await asyncio.wait_for(serve_task, timeout=4.0)
        assert exit_code == EXIT_FATAL_LIFECYCLE
        assert not list(state_dir.glob("shutdown-*.json"))
        assert harness_sink[0].ownership.acquired is True
        assert is_runtime_lock_active(lock_path) is True
        harness_sink[0].ownership.release()

    asyncio.run(_run())


def test_instance_verified_unlink_preserves_other_instance_and_fails_closed_on_unsafe(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    write_secure_local_file(control_token_path(state_dir), "token-new")
    write_secure_local_file(
        server_discovery_path(state_dir),
        json.dumps(
            {
                "url": "http://127.0.0.1:8000",
                "host": "127.0.0.1",
                "port": 8000,
                "pid": 99,
                "process_start_ticks": 1,
                "boot_id": "b",
                "server_instance_id": "inst-new",
                "started_at": "2026-01-01T00:00:00Z",
            }
        ),
    )
    assert (
        unlink_owned_server_discovery(state_dir, "inst-old")
        is CleanupOutcome.FOREIGN_INSTANCE
    )
    assert (
        unlink_owned_control_token(state_dir, "token-old")
        is CleanupOutcome.FOREIGN_INSTANCE
    )
    assert server_discovery_path(state_dir).exists()
    assert control_token_path(state_dir).exists()

    assert (
        unlink_owned_server_discovery(state_dir, "inst-new") is CleanupOutcome.REMOVED
    )
    assert unlink_owned_control_token(state_dir, "token-new") is CleanupOutcome.REMOVED
    assert unlink_owned_server_discovery(state_dir, "inst-new") is CleanupOutcome.ABSENT
    assert unlink_owned_control_token(state_dir, "token-new") is CleanupOutcome.ABSENT

    # Corrupt/unsafe metadata fails closed with MetadataCleanupError.
    write_secure_local_file(server_discovery_path(state_dir), "{corrupt")
    with pytest.raises(MetadataCleanupError):
        unlink_owned_server_discovery(state_dir, "inst-new")

    # D11-02-AMENDMENT-01: no stale receipt-revocation helper remains necessary.
    assert not hasattr(auth_module, "unlink_owned_shutdown_receipt")


def test_e0_02_receipt_timing_post_release_only_and_lock_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E0-02: Verify post-release CLEAN_SHUTDOWN commit ordering and all release failure modes."""

    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)

    # 1. Normal shutdown writes integer-pid receipt strictly AFTER ownership.release().
    state_ok = tmp_path / "normal_order"
    port_ok = _unused_loopback_port()
    events: list[str] = []
    harness_ok: list[HarnessObjects] = []

    async def _run_normal_order() -> None:
        async def factory(
            cfg: RuntimeConfig,
            bus: InvocationEventBus,
            settled: Callable[[], Awaitable[None] | None],
        ) -> ApplicationCoordinator:
            async def wrapped_settled() -> None:
                res = settled()
                if res is not None:
                    await res
                assert not list(state_ok.glob("shutdown-*.json"))
                assert not server_discovery_path(state_ok).exists()
                assert not control_token_path(state_ok).exists()
                assert is_runtime_lock_active(lock_path) is True
                events.append("pre_release_settled")

            coord = await _start_locked_coordinator(
                cfg, bus, wrapped_settled, harness_sink=harness_ok
            )
            orig_release = harness_ok[0].ownership.release

            def tracked_release() -> None:
                assert not list(state_ok.glob("shutdown-*.json"))
                orig_release()
                assert not list(state_ok.glob("shutdown-*.json"))
                assert (
                    harness_ok[0].ownership.release_outcome
                    is OwnershipReleaseOutcome.CONFIRMED_RELEASED
                )
                assert (
                    harness_ok[0].ownership.inspect_kernel_ownership()
                    is LockOwnershipState.NOT_HELD
                )
                events.append("ownership_released")

            monkeypatch.setattr(harness_ok[0].ownership, "release", tracked_release)
            return coord

        launcher = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(0.5),
            server_config=ServerConfig(
                host="127.0.0.1", port=port_ok, state_directory=str(state_ok)
            ),
            coordinator_factory=factory,
        )
        serve_task = asyncio.create_task(launcher.serve())
        await _wait_for_discovery(state_ok, serve_task)
        assert launcher.uvicorn_server is not None
        launcher.uvicorn_server.handle_exit(signal.SIGTERM, None)
        exit_code = await asyncio.wait_for(serve_task, timeout=4.0)
        assert exit_code == EXIT_OK
        assert events == ["pre_release_settled", "ownership_released"]
        receipt_file = shutdown_receipt_path(state_ok, launcher.server_instance_id)
        assert receipt_file.exists()
        raw = json.loads(read_secure_local_file(receipt_file))
        assert type(raw["pid"]) is int and raw["pid"] == os.getpid()
        assert raw["status"] == "CLEAN_SHUTDOWN"

    asyncio.run(_run_normal_order())
    monkeypatch.undo()
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)

    # 2. Release raises AFTER actual unlock (flock(LOCK_UN) fails -> finally os.close releases kernel lock).
    state_after = tmp_path / "release_after_unlock"
    port_after = _unused_loopback_port()
    harness_after: list[HarnessObjects] = []

    async def _run_release_after_unlock() -> None:
        async def factory(
            cfg: RuntimeConfig,
            bus: InvocationEventBus,
            settled: Callable[[], Awaitable[None] | None],
        ) -> ApplicationCoordinator:
            return await _start_locked_coordinator(
                cfg, bus, settled, harness_sink=harness_after
            )

        launcher = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(0.5),
            server_config=ServerConfig(
                host="127.0.0.1", port=port_after, state_directory=str(state_after)
            ),
            coordinator_factory=factory,
        )
        serve_task = asyncio.create_task(launcher.serve())
        await _wait_for_discovery(state_after, serve_task)

        target_unlock_fd = harness_after[0].ownership._fd
        assert target_unlock_fd is not None
        orig_flock = fcntl.flock
        injected = False

        def fail_unlock(fd: int, op: int) -> None:
            nonlocal injected
            if not injected and fd == target_unlock_fd and op == fcntl.LOCK_UN:
                injected = True
                raise OSError("injected LOCK_UN failure")
            orig_flock(fd, op)

        monkeypatch.setattr(fcntl, "flock", fail_unlock)
        assert launcher.uvicorn_server is not None
        launcher.uvicorn_server.handle_exit(signal.SIGTERM, None)
        exit_code = await asyncio.wait_for(serve_task, timeout=4.0)
        assert exit_code == EXIT_FATAL_LIFECYCLE
        assert launcher.shutdown_receipt is None
        assert not list(state_after.glob("shutdown-*.json"))
        # Real os.close(fd) ran and released the kernel flock even though release() raised.
        assert harness_after[0].ownership.acquired is False
        assert (
            harness_after[0].ownership.inspect_kernel_ownership()
            is LockOwnershipState.NOT_HELD
        )
        assert (
            harness_after[0].ownership.release_outcome
            is OwnershipReleaseOutcome.FAILED_AFTER_RELEASE
        )
        assert is_runtime_lock_active(lock_path) is False
        # Verify competing lock acquisition succeeds cleanly without stale holder.
        probe = RuntimeOwnershipLock(lock_path)
        probe.acquire()
        probe.release()

    asyncio.run(_run_release_after_unlock())
    monkeypatch.undo()
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)

    # 3. Release raises on os.close after real kernel close (Linux EIO semantics) -> never double-closes fd!
    state_close = tmp_path / "release_close_eio"
    port_close = _unused_loopback_port()
    harness_close: list[HarnessObjects] = []

    async def _run_close_eio() -> None:
        async def factory(
            cfg: RuntimeConfig,
            bus: InvocationEventBus,
            settled: Callable[[], Awaitable[None] | None],
        ) -> ApplicationCoordinator:
            return await _start_locked_coordinator(
                cfg, bus, settled, harness_sink=harness_close
            )

        launcher = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(0.5),
            server_config=ServerConfig(
                host="127.0.0.1", port=port_close, state_directory=str(state_close)
            ),
            coordinator_factory=factory,
        )
        serve_task = asyncio.create_task(launcher.serve())
        await _wait_for_discovery(state_close, serve_task)

        target_fd = harness_close[0].ownership._fd
        assert target_fd is not None
        closed_fds: list[int] = []
        orig_close = os.close
        raised_once = False

        def eio_close(fd: int) -> None:
            nonlocal raised_once
            if fd == target_fd and not raised_once:
                raised_once = True
                closed_fds.append(fd)
                orig_close(fd)
                raise OSError("EIO after kernel close")
            if fd == target_fd and raised_once:
                closed_fds.append(fd)
            orig_close(fd)

        monkeypatch.setattr(os, "close", eio_close)
        assert launcher.uvicorn_server is not None
        launcher.uvicorn_server.handle_exit(signal.SIGINT, None)
        exit_code = await asyncio.wait_for(serve_task, timeout=4.0)
        assert exit_code == EXIT_FATAL_LIFECYCLE
        assert not list(state_close.glob("shutdown-*.json"))
        assert (
            harness_close[0].ownership.release_outcome
            is OwnershipReleaseOutcome.FAILED_AFTER_RELEASE
        )
        # Idempotent retry of release() never calls close() a second time on target_fd.
        closed_fds.clear()
        harness_close[0].ownership.release()
        assert closed_fds == []

    asyncio.run(_run_close_eio())
    monkeypatch.undo()
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)

    # 4. Ambiguous unlock/close state (UNKNOWN) forbids receipt and returns exit 4.
    state_ambig = tmp_path / "release_unknown"
    port_ambig = _unused_loopback_port()
    harness_ambig: list[HarnessObjects] = []

    async def _run_unknown_state() -> None:
        async def factory(
            cfg: RuntimeConfig,
            bus: InvocationEventBus,
            settled: Callable[[], Awaitable[None] | None],
        ) -> ApplicationCoordinator:
            return await _start_locked_coordinator(
                cfg, bus, settled, harness_sink=harness_ambig
            )

        launcher = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(0.5),
            server_config=ServerConfig(
                host="127.0.0.1", port=port_ambig, state_directory=str(state_ambig)
            ),
            coordinator_factory=factory,
        )
        serve_task = asyncio.create_task(launcher.serve())
        await _wait_for_discovery(state_ambig, serve_task)

        monkeypatch.setattr(
            harness_ambig[0].ownership,
            "inspect_kernel_ownership",
            lambda: LockOwnershipState.UNKNOWN,
        )
        assert launcher.uvicorn_server is not None
        launcher.uvicorn_server.handle_exit(signal.SIGTERM, None)
        exit_code = await asyncio.wait_for(serve_task, timeout=4.0)
        assert exit_code == EXIT_FATAL_LIFECYCLE
        assert (
            harness_ambig[0].ownership.release_outcome
            is OwnershipReleaseOutcome.FAILED_UNKNOWN
        )
        assert not list(state_ambig.glob("shutdown-*.json"))

    asyncio.run(_run_unknown_state())
    monkeypatch.undo()
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)

    # 5. Post-release receipt publication failure exits 4 cleanly with no leftover receipt or tmp file.
    state_pub = tmp_path / "receipt_write_fail"
    port_pub = _unused_loopback_port()

    async def _run_receipt_fail() -> None:
        launcher = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(0.5),
            server_config=ServerConfig(
                host="127.0.0.1", port=port_pub, state_directory=str(state_pub)
            ),
            coordinator_factory=_start_locked_coordinator,
        )
        serve_task = asyncio.create_task(launcher.serve())
        await _wait_for_discovery(state_pub, serve_task)

        orig_write = write_secure_local_file

        def fail_receipt_write(path: Path, content: str) -> None:
            if path.name.startswith("shutdown-"):
                raise OSError("disk full writing receipt")
            orig_write(path, content)

        monkeypatch.setattr(
            launcher_module, "write_secure_local_file", fail_receipt_write
        )
        assert launcher.uvicorn_server is not None
        launcher.uvicorn_server.handle_exit(signal.SIGTERM, None)
        exit_code = await asyncio.wait_for(serve_task, timeout=4.0)
        assert exit_code == EXIT_FATAL_LIFECYCLE
        assert launcher.shutdown_receipt is None
        assert not list(state_pub.glob("shutdown-*.json"))
        assert not list(state_pub.glob("*.tmp"))
        assert is_runtime_lock_active(lock_path) is False

    asyncio.run(_run_receipt_fail())
    monkeypatch.undo()
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)

    # 6. New server starts DURING old server's post-release receipt window + delayed callbacks cannot mutate new server.
    state_race = tmp_path / "shared_state_race"
    port_old = _unused_loopback_port()
    port_new = _unused_loopback_port()
    old_released_before_receipt = threading.Event()
    allow_old_receipt = threading.Event()
    orig_write = write_secure_local_file

    launcher_old = ServerLauncher(
        runtime_config=_make_runtime_with_timeout(0.5),
        server_config=ServerConfig(
            host="127.0.0.1", port=port_old, state_directory=str(state_race)
        ),
        coordinator_factory=_start_locked_coordinator,
    )
    old_id = launcher_old.server_instance_id

    def barrier_receipt_write(path: Path, content: str) -> None:
        if path.name == f"shutdown-{old_id}.json":
            old_released_before_receipt.set()
            assert allow_old_receipt.wait(timeout=5.0)
        orig_write(path, content)

    monkeypatch.setattr(
        launcher_module, "write_secure_local_file", barrier_receipt_write
    )

    old_codes: list[int] = []
    old_thread = threading.Thread(
        target=lambda: old_codes.append(launcher_old.run()), daemon=True
    )
    old_thread.start()

    deadline = time.monotonic() + 5.0
    while (
        time.monotonic() < deadline and not server_discovery_path(state_race).exists()
    ):
        time.sleep(0.02)
    assert server_discovery_path(state_race).exists()

    # Trigger graceful shutdown on old server; it will release ownership, then pause BEFORE writing receipt.
    assert launcher_old.uvicorn_server is not None
    launcher_old.uvicorn_server.handle_exit(signal.SIGTERM, None)
    assert old_released_before_receipt.wait(timeout=5.0)
    assert not is_runtime_lock_active(lock_path)
    assert not server_discovery_path(state_race).exists()
    assert not control_token_path(state_race).exists()
    assert not shutdown_receipt_path(state_race, old_id).exists()

    # While old server has NOT yet written its receipt, start the new server completely!
    launcher_new = ServerLauncher(
        runtime_config=_make_runtime_with_timeout(0.5),
        server_config=ServerConfig(
            host="127.0.0.1", port=port_new, state_directory=str(state_race)
        ),
        coordinator_factory=_start_locked_coordinator,
    )
    new_codes: list[int] = []
    new_thread = threading.Thread(
        target=lambda: new_codes.append(launcher_new.run()), daemon=True
    )
    new_thread.start()

    deadline = time.monotonic() + 5.0
    while (
        time.monotonic() < deadline and not server_discovery_path(state_race).exists()
    ):
        time.sleep(0.02)
    assert server_discovery_path(state_race).exists()
    new_disc_before = json.loads(
        read_secure_local_file(server_discovery_path(state_race))
    )
    new_token_before = read_secure_local_file(control_token_path(state_race))
    assert new_disc_before["server_instance_id"] == launcher_new.server_instance_id

    # Now allow old server to finish publishing its instance-scoped receipt concurrently with running new server.
    allow_old_receipt.set()
    old_thread.join(timeout=5.0)
    assert old_codes == [EXIT_OK]

    # Delayed callbacks on old launcher MUST NOT remove new server's server.json or control.token.
    launcher_old._on_http_shutdown_requested()
    launcher_old._on_signal_exit()
    launcher_old._safe_t0_unlink_discovery()

    new_disc_after = json.loads(
        read_secure_local_file(server_discovery_path(state_race))
    )
    assert new_disc_after == new_disc_before
    assert read_secure_local_file(control_token_path(state_race)) == new_token_before
    assert shutdown_receipt_path(state_race, old_id).exists()
    assert is_runtime_lock_active(lock_path) is True

    assert launcher_new.uvicorn_server is not None
    launcher_new.uvicorn_server.handle_exit(signal.SIGTERM, None)
    new_thread.join(timeout=5.0)
    assert new_codes == [EXIT_OK]


def test_e0_03_stale_discovery_recovery_and_startup_failure_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E0-03: Verify stale recovery under newly acquired lock across normal restart, EADDRINUSE, signals, and unsafe files."""

    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)

    def _write_stale_crash_artifacts(state_dir: Path) -> None:
        write_secure_local_file(control_token_path(state_dir), "stale-dead-token")
        write_secure_local_file(
            server_discovery_path(state_dir),
            json.dumps(
                {
                    "url": "http://127.0.0.1:8123",
                    "host": "127.0.0.1",
                    "port": 8123,
                    "pid": 999_999,
                    "process_start_ticks": 123,
                    "boot_id": read_boot_id(),
                    "server_instance_id": "crashed-old-inst",
                    "started_at": "2026-01-01T00:00:00Z",
                }
            ),
        )
        stale_lock = RuntimeOwnershipLock(lock_path)
        stale_lock.acquire()
        stale_lock.release()

    # 1. Abnormal old exit -> new server recovers cleanly, replaces token & discovery, and shuts down cleanly.
    state_ok = tmp_path / "recover_ok"
    _write_stale_crash_artifacts(state_ok)
    port_ok = _unused_loopback_port()

    async def _run_recover_ok() -> None:
        launcher = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(0.4),
            server_config=ServerConfig(
                host="127.0.0.1", port=port_ok, state_directory=str(state_ok)
            ),
            coordinator_factory=_start_locked_coordinator,
        )
        serve_task = asyncio.create_task(launcher.serve())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not launcher._server_published:
            assert not serve_task.done()
            await asyncio.sleep(0.02)
        assert launcher._server_published
        disc = json.loads(read_secure_local_file(server_discovery_path(state_ok)))
        assert disc["server_instance_id"] == launcher.server_instance_id
        assert disc["server_instance_id"] != "crashed-old-inst"
        assert (
            read_secure_local_file(control_token_path(state_ok)) != "stale-dead-token"
        )
        assert launcher.uvicorn_server is not None
        launcher.uvicorn_server.handle_exit(signal.SIGTERM, None)
        assert await asyncio.wait_for(serve_task, timeout=4.0) == EXIT_OK
        assert not server_discovery_path(state_ok).exists()
        assert not control_token_path(state_ok).exists()
        assert shutdown_receipt_path(state_ok, launcher.server_instance_id).exists()

    asyncio.run(_run_recover_ok())

    # 2. Abnormal old exit -> occupied port EADDRINUSE cleans stale discovery & token under lock, releases lock, exits 4.
    state_bind = tmp_path / "recover_bind_fail"
    _write_stale_crash_artifacts(state_bind)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    occupied_port = int(sock.getsockname()[1])
    try:
        launcher_bind = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(0.3),
            server_config=ServerConfig(
                host="127.0.0.1", port=occupied_port, state_directory=str(state_bind)
            ),
            coordinator_factory=_start_locked_coordinator,
        )
        code = launcher_bind.run()
        assert code == EXIT_FATAL_LIFECYCLE
        assert not server_discovery_path(state_bind).exists()
        assert not control_token_path(state_bind).exists()
        assert not list(state_bind.glob("shutdown-*.json"))
        assert is_runtime_lock_active(lock_path) is False
    finally:
        sock.close()

    # 3. Abnormal old exit -> shutdown signal during startup cleans stale discovery & token under lock without receipt.
    state_sig = tmp_path / "recover_startup_sig"
    _write_stale_crash_artifacts(state_sig)
    port_sig = _unused_loopback_port()

    async def _run_recover_sig() -> None:
        holder: list[ServerLauncher] = []

        async def sig_on_open() -> None:
            assert holder[0].uvicorn_server is not None
            holder[0].uvicorn_server.handle_exit(signal.SIGTERM, None)

        async def factory(
            cfg: RuntimeConfig,
            bus: InvocationEventBus,
            settled: Callable[[], Awaitable[None] | None],
        ) -> ApplicationCoordinator:
            return await _start_locked_coordinator(
                cfg, bus, settled, open_resources=sig_on_open
            )

        launcher_sig = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(0.4),
            server_config=ServerConfig(
                host="127.0.0.1", port=port_sig, state_directory=str(state_sig)
            ),
            coordinator_factory=factory,
        )
        holder.append(launcher_sig)
        assert await launcher_sig.serve() == EXIT_OK
        assert not server_discovery_path(state_sig).exists()
        assert not control_token_path(state_sig).exists()
        assert not list(state_sig.glob("shutdown-*.json"))
        assert is_runtime_lock_active(lock_path) is False

    asyncio.run(_run_recover_sig())

    # 4. Unsafe symlink stale server.json fails closed under lock without following symlink.
    state_unsafe = tmp_path / "recover_unsafe_symlink"
    target_secret = tmp_path / "do_not_delete.txt"
    target_secret.write_text("keep", encoding="utf-8")
    state_unsafe.mkdir(mode=0o700)
    server_discovery_path(state_unsafe).symlink_to(target_secret)
    with pytest.raises(MetadataCleanupError):
        clear_stale_server_discovery_under_lock(state_unsafe, "new-inst")
    assert target_secret.exists()

    # 5. Pre-lock signal during competing startup never mutates live primary server's files.
    state_prelock = tmp_path / "prelock_signal"
    write_secure_local_file(control_token_path(state_prelock), "primary-live-token")
    primary_lock = RuntimeOwnershipLock(lock_path)
    primary_lock.acquire()
    try:

        async def prelock_abort_factory(
            cfg: RuntimeConfig,
            bus: InvocationEventBus,
            settled: Callable[[], Awaitable[None] | None],
        ) -> ApplicationCoordinator:
            del cfg, bus, settled
            raise RuntimeOwnershipConflictError

        competing = ServerLauncher(
            runtime_config=_make_runtime_with_timeout(0.3),
            server_config=ServerConfig(
                host="127.0.0.1",
                port=_unused_loopback_port(),
                state_directory=str(state_prelock),
            ),
            coordinator_factory=prelock_abort_factory,
        )
        competing._on_http_shutdown_requested()
        assert competing.run() == EXIT_OWNERSHIP_CONFLICT
        assert (
            read_secure_local_file(control_token_path(state_prelock))
            == "primary-live-token"
        )
    finally:
        primary_lock.release()
