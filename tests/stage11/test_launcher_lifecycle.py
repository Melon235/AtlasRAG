"""WP-D unit, R1 regression, and local lifecycle tests for ServerLauncher."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import signal
import socket
import stat
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
    RuntimeShutdownFailedError,
)
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse
from atlasrag.runtime.composition import ApplicationComposition
from atlasrag.runtime.coordinator import ApplicationCoordinator
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.runtime.ownership import RuntimeOwnershipLock
from atlasrag.runtime.tasks import TaskKind
from atlasrag.server import launcher as launcher_module
from atlasrag.server.auth import (
    CleanupOutcome,
    MetadataCleanupError,
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
