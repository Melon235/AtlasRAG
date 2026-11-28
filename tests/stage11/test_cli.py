"""WP-D unit, R1 security regression, and subprocess tests for the thin CLI."""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
from pydantic import HttpUrl

import atlasrag.runtime.ownership as ownership_module
from atlasrag.cli.main import EXIT_OK, build_parser, main
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.enums import EvidenceType
from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.requests import QueryFilters, UserTurnRequest
from atlasrag.domain.results import (
    FinalLocalCitation,
    FinalResponse,
    FinalWebCitation,
)
from atlasrag.runtime.coordinator import ApplicationCoordinator
from atlasrag.runtime.ownership import RuntimeOwnershipLock
from atlasrag.server.auth import (
    cli_session_path,
    control_token_path,
    read_boot_id,
    read_proc_stat_starttime,
    read_secure_local_file,
    server_discovery_path,
    shutdown_receipt_path,
    validate_loopback_url,
    write_secure_local_file,
)
from atlasrag.server.config import ServerConfig
from atlasrag.server.events import InvocationEventBus
from atlasrag.server.launcher import ServerLauncher
from atlasrag.server.schemas import CliSessionRecord
from atlasrag.services.sync.models import (
    ReconciliationOutcome,
    ReconciliationResult,
    ServingSafety,
    SyncOutcome,
    SyncResult,
)
from stage11.conftest import (
    build_test_dependencies,
    sample_response,
    sample_runtime_config,
)

cli_module = sys.modules["atlasrag.cli.main"]


def _stop_threaded_server(
    *, state_dir: Path, port: int, thread: threading.Thread
) -> None:
    token = read_secure_local_file(control_token_path(state_dir))
    with httpx.Client(follow_redirects=False, trust_env=False) as client:
        resp = client.post(
            f"http://127.0.0.1:{port}/api/v1/control/shutdown",
            json={},
            headers={
                "origin": f"http://127.0.0.1:{port}",
                "authorization": f"Bearer {token}",
            },
        )
        assert resp.status_code == 202
    thread.join(timeout=5.0)


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _make_runtime(shutdown_timeout_seconds: float = 1.5) -> RuntimeConfig:
    payload = sample_runtime_config().model_dump(mode="python")
    payload["maintenance"]["shutdown_timeout_seconds"] = shutdown_timeout_seconds
    return RuntimeConfig.model_validate(payload)


def _run_launcher_in_thread(
    *,
    state_dir: Path,
    port: int,
    sync_fn: Callable[[], Awaitable[SyncResult]] | None = None,
    reconcile_fn: Callable[[], Awaitable[ReconciliationResult]] | None = None,
    retrieval: Callable[[UserTurnRequest], Awaitable[FinalResponse]] | None = None,
    shutdown_timeout_seconds: float = 1.5,
) -> tuple[threading.Thread, list[int], RuntimeOwnershipLock]:
    exit_codes: list[int] = []
    lock = RuntimeOwnershipLock()

    async def factory(
        cfg: RuntimeConfig,
        bus: InvocationEventBus,
        settled: Callable[[], Awaitable[None] | None],
    ) -> ApplicationCoordinator:
        deps, harness = build_test_dependencies(
            retrieval=retrieval,
            sync_fn=sync_fn,
            reconcile_fn=reconcile_fn,
            on_shutdown_settled=settled,
            shutdown_timeout_seconds=cfg.maintenance.shutdown_timeout_seconds,
        )
        harness.ownership = lock
        deps_locked = dataclasses.replace(
            deps,
            ownership=harness.ownership,
            on_shutdown_settled=settled,
        )
        return await ApplicationCoordinator.start(deps_locked)

    launcher = ServerLauncher(
        runtime_config=_make_runtime(shutdown_timeout_seconds),
        server_config=ServerConfig(
            host="127.0.0.1", port=port, state_directory=str(state_dir)
        ),
        coordinator_factory=factory,
        log_level="warning",
    )

    def _target() -> None:
        code = launcher.run()
        exit_codes.append(code)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while (
        time.monotonic() < deadline
        and thread.is_alive()
        and not server_discovery_path(state_dir).exists()
    ):
        time.sleep(0.02)
    assert server_discovery_path(state_dir).exists(), (
        f"server failed early: {exit_codes}"
    )
    return thread, exit_codes, lock


def test_cli_help_and_import_never_load_runtime_or_web_frameworks(
    tmp_path: Path,
) -> None:
    script = """
import sys
from atlasrag.cli.main import build_parser, main
code = main(["--help"])
forbidden = [
    name for name in sys.modules
    if name.startswith(("atlasrag.runtime.composition", "fastapi", "starlette", "uvicorn"))
]
assert code == 0, code
assert not forbidden, forbidden
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "ATLASRAG_STATE_DIR": str(tmp_path / "s"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "s").exists()


def test_cli_discovery_precedence_and_url_security_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    write_secure_local_file(control_token_path(state_dir), "super-secret-token")
    write_secure_local_file(
        server_discovery_path(state_dir),
        '{"url":"http://127.0.0.1:8123","host":"127.0.0.1","port":8123,"pid":100,"process_start_ticks":1,"boot_id":"b","server_instance_id":"inst1","started_at":"2026-01-01T00:00:00Z"}',
    )

    seen_base_urls: list[str] = []

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            seen_base_urls.append(str(kwargs["base_url"]))
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False
            raise httpx.ConnectError("stub unreachable")

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    assert (
        main(
            [
                "health",
                "--state-dir",
                str(state_dir),
                "--server-url",
                "http://127.0.0.1:9001",
            ],
            environ={"ATLASRAG_SERVER_URL": "http://127.0.0.1:9002"},
        )
        == 3
    )
    assert seen_base_urls[-1] == "http://127.0.0.1:9001"

    assert (
        main(
            ["health", "--state-dir", str(state_dir)],
            environ={"ATLASRAG_SERVER_URL": "http://localhost:9002"},
        )
        == 3
    )
    assert seen_base_urls[-1] == "http://localhost:9002"

    assert main(["health", "--state-dir", str(state_dir)], environ={}) == 3
    assert seen_base_urls[-1] == "http://127.0.0.1:8123"

    # Reject non-loopback, fragments, queries, non-root paths, userinfo BEFORE token access.
    read_calls: list[Path] = []
    orig_read = cli_module.read_secure_local_file

    def _spy_read(path: Path, *, max_bytes: int = 8192) -> str:
        read_calls.append(path)
        return str(orig_read(path, max_bytes=max_bytes))

    monkeypatch.setattr(cli_module, "read_secure_local_file", _spy_read)
    for bad_url in [
        "http://evil.example.com:8000",
        "http://user:pass@127.0.0.1:8000",
        "ftp://127.0.0.1:8000",
        "http://127.0.0.1:8000#frag",
        "http://127.0.0.1:8000?a=1",
        "http://127.0.0.1:8000/subpath",
        "http://127.0.0.1:99999",
    ]:
        with pytest.raises(ValueError):
            validate_loopback_url(bad_url)
        code = main(
            ["sync", "--state-dir", str(state_dir), "--server-url", bad_url],
            environ={},
        )
        assert code == 2
        assert control_token_path(state_dir) not in read_calls
    capsys.readouterr()


def test_cli_serve_invalid_config_exits_2_without_state_dir_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    bad_config = tmp_path / "bad.json"
    bad_config.write_text("{not-json", encoding="utf-8")

    code = main(
        ["serve", "--config", str(bad_config), "--state-dir", str(state_dir), "--json"],
        environ={},
    )
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "INVALID_CONFIGURATION"
    assert not state_dir.exists()


def test_wpd_f02_control_commands_refuse_unrelated_loopback_destination_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """WPD-F02: stop/sync/recover must never send control.token to mismatched loopback URL."""

    state_dir = tmp_path / "state"
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)
    port = _unused_port()

    unrelated_hits: list[tuple[str, dict[str, str]]] = []

    class _DecoyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            unrelated_hits.append(
                (self.path, {k.lower(): v for k, v in self.headers.items()})
            )
            self.send_response(202)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"status":"SHUTTING_DOWN","pid":1,"server_instance_id":"decoy"}'
            )

        def log_message(self, *_args: object) -> None:
            return

    decoy = HTTPServer(("127.0.0.1", 0), _DecoyHandler)
    decoy_port = int(decoy.server_address[1])
    decoy_thread = threading.Thread(target=decoy.serve_forever, daemon=True)
    decoy_thread.start()

    thread, exit_codes, lock = _run_launcher_in_thread(state_dir=state_dir, port=port)
    try:
        decoy_url = f"http://127.0.0.1:{decoy_port}"
        for argv, env in [
            (
                [
                    "stop",
                    "--state-dir",
                    str(state_dir),
                    "--server-url",
                    decoy_url,
                    "--force",
                    "--json",
                ],
                {},
            ),
            (
                ["stop", "--state-dir", str(state_dir), "--force", "--json"],
                {"ATLASRAG_SERVER_URL": decoy_url},
            ),
            (
                [
                    "sync",
                    "--state-dir",
                    str(state_dir),
                    "--server-url",
                    decoy_url,
                    "--json",
                ],
                {},
            ),
            (
                ["recover", "--state-dir", str(state_dir), "--json"],
                {"ATLASRAG_SERVER_URL": decoy_url},
            ),
        ]:
            code = main(argv, environ=env)
            assert code == 4
            out = json.loads(capsys.readouterr().out)
            assert out["error_code"] == "CONTROL_TARGET_MISMATCH"
        assert unrelated_hits == []

        _stop_threaded_server(state_dir=state_dir, port=port, thread=thread)
        assert exit_codes == [EXIT_OK]
    finally:
        decoy.shutdown()
        decoy.server_close()
        lock.release()


def test_wpd_f02_stop_rejects_mismatched_202_identity_and_pre_shutdown_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """WPD-F02: Verify HTTP 202 status/pid/instance_id and pre-shutdown replacement race."""

    state_dir = tmp_path / "state"
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        ticks = read_proc_stat_starttime(sleeper.pid)
        boot = read_boot_id()
        signal_calls: list[int] = []
        monkeypatch.setattr(
            signal, "pidfd_send_signal", lambda _fd, sig: signal_calls.append(sig)
        )
        # Hold canonical lock FROM sleeper's PID? Wait: verify_target_process requires sleeper to hold lock;
        # mock verify_process_holds_flock for the HTTP-202 mismatch & replacement cases while testing real flock elsewhere.
        monkeypatch.setattr(
            cli_module, "verify_process_holds_flock", lambda _p, _lp: True
        )
        orig_verify_target = cli_module.verify_target_process

        # Mock HTTP server returning tampered 202 payloads.
        responses: list[bytes] = [
            json.dumps(
                {
                    "status": "SHUTTING_DOWN",
                    "pid": sleeper.pid + 1,
                    "server_instance_id": "inst-ok",
                }
            ).encode(),
            json.dumps(
                {
                    "status": "SHUTTING_DOWN",
                    "pid": sleeper.pid,
                    "server_instance_id": "wrong-id",
                }
            ).encode(),
            json.dumps(
                {"status": "ALIVE", "pid": sleeper.pid, "server_instance_id": "inst-ok"}
            ).encode(),
        ]

        class _Bad202Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = responses.pop(0)
                self.send_response(202)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        srv = HTTPServer(("127.0.0.1", 0), _Bad202Handler)
        srv_port = int(srv.server_address[1])
        srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
        srv_thread.start()
        try:
            write_secure_local_file(control_token_path(state_dir), "tok-ok")
            write_secure_local_file(
                server_discovery_path(state_dir),
                json.dumps(
                    {
                        "url": f"http://127.0.0.1:{srv_port}",
                        "host": "127.0.0.1",
                        "port": srv_port,
                        "pid": sleeper.pid,
                        "process_start_ticks": ticks,
                        "boot_id": boot,
                        "server_instance_id": "inst-ok",
                        "started_at": "2026-01-01T00:00:00Z",
                    }
                ),
            )
            monkeypatch.setattr(
                cli_module,
                "verify_target_process",
                lambda sd: orig_verify_target(sd, lock_path=lock_path),
            )
            # Even test real verify_process_holds_flock isn't active? Notice cli_module.verify_target_process uses auth module's.
            import atlasrag.server.auth as auth_module

            monkeypatch.setattr(
                auth_module, "verify_process_holds_flock", lambda _p, _lp: True
            )

            for _ in range(3):
                assert (
                    main(
                        ["stop", "--state-dir", str(state_dir), "--force", "--json"],
                        environ={},
                    )
                    == 4
                )
                err = json.loads(capsys.readouterr().out)
                assert err["error_code"] == "SERVER_IDENTITY_MISMATCH"
            assert not signal_calls

            # Pre-shutdown discovery/token replacement race: simulate token rotated right before POST.
            def rotating_verify_stability(**_kwargs: object) -> bool:
                write_secure_local_file(
                    control_token_path(state_dir), "rotated-new-token"
                )
                return False

            monkeypatch.setattr(
                cli_module, "_verify_pre_shutdown_stability", rotating_verify_stability
            )
            assert (
                main(
                    ["stop", "--state-dir", str(state_dir), "--force", "--json"],
                    environ={},
                )
                == 4
            )
            assert (
                json.loads(capsys.readouterr().out)["error_code"]
                == "UNVERIFIED_PROCESS_IDENTITY"
            )
            assert not signal_calls
        finally:
            srv.shutdown()
            srv.server_close()
    finally:
        sleeper.kill()
        sleeper.wait()


def test_cli_ask_session_reuse_auto_renew_stream_filters_and_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)
    port = _unused_port()
    seen_filters: list[QueryFilters | None] = []
    sync_outcomes = [
        SyncResult(
            outcome=SyncOutcome.SUCCESS, serving_safety=ServingSafety.SAFE_TO_SERVE
        ),
        SyncResult(
            outcome=SyncOutcome.PARTIAL_SUCCESS,
            serving_safety=ServingSafety.UNSAFE_TO_SERVE,
            blocked_code="SOURCE_UNREACHABLE",
            unsafe_reasons=("DOCUMENT_SYNC_PARTIAL",),
        ),
    ]

    async def fake_retrieval(req: UserTurnRequest) -> FinalResponse:
        seen_filters.append(req.filters)
        return sample_response()

    async def fake_sync() -> SyncResult:
        return sync_outcomes.pop(0)

    thread, exit_codes, lock = _run_launcher_in_thread(
        state_dir=state_dir,
        port=port,
        sync_fn=fake_sync,
        retrieval=fake_retrieval,
    )
    try:
        assert (
            main(["health", "--state-dir", str(state_dir), "--json"], environ={}) == 0
        )
        assert json.loads(capsys.readouterr().out)["ready"] is True

        assert (
            main(
                ["health", "--live", "--state-dir", str(state_dir), "--json"],
                environ={},
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["status"] == "ALIVE"

        assert (
            main(
                ["status", "--refresh", "--state-dir", str(state_dir), "--json"],
                environ={},
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["mode"] == "SERVING"

        assert (
            main(
                [
                    "ask",
                    "First question",
                    "--file-name",
                    "doc.md",
                    "--source-type",
                    "MD",
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                environ={},
            )
            == 0
        )
        first_out = json.loads(capsys.readouterr().out)
        first_session_id = first_out["session_id"]
        assert first_out["response"]["answer_text"] == sample_response().answer_text
        assert stat.S_IMODE(cli_session_path(state_dir).stat().st_mode) == 0o600
        assert seen_filters[-1] is not None and seen_filters[-1].file_name == "doc.md"

        assert (
            main(
                [
                    "ask",
                    "Streamed",
                    "--stream",
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                environ={},
            )
            == 0
        )
        second_out = json.loads(capsys.readouterr().out)
        assert second_out["session_id"] == first_session_id

        stale_record = CliSessionRecord(
            server_instance_id=None,
            server_url=f"http://127.0.0.1:{port}",
            session_id="sess_expired_unknown",
            expires_at=cli_module.datetime.now(cli_module.UTC).replace(year=2099),
        )
        write_secure_local_file(
            cli_session_path(state_dir), stale_record.model_dump_json()
        )
        assert (
            main(["ask", "Renew", "--state-dir", str(state_dir), "--json"], environ={})
            == 0
        )
        assert (
            json.loads(capsys.readouterr().out)["session_id"] != "sess_expired_unknown"
        )

        assert (
            main(
                [
                    "ask",
                    "Bad sess",
                    "--session-id",
                    "sess_bad",
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                environ={},
            )
            == 1
        )
        assert json.loads(capsys.readouterr().out)["error_code"] == "SESSION_NOT_FOUND"

        assert main(["ask", "   ", "--state-dir", str(state_dir)], environ={}) == 2
        assert (
            main(
                ["ask", "q", "--timeout", "0", "--state-dir", str(state_dir)],
                environ={},
            )
            == 2
        )

        assert (
            main(
                [
                    "sync",
                    "--poll-interval",
                    "0.05",
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                environ={},
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["serving_safety"] == "SAFE_TO_SERVE"

        assert (
            main(
                [
                    "sync",
                    "--poll-interval",
                    "0.05",
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                environ={},
            )
            == 1
        )
        assert (
            json.loads(capsys.readouterr().out)["serving_safety"] == "UNSAFE_TO_SERVE"
        )

        _stop_threaded_server(state_dir=state_dir, port=port, thread=thread)
        assert exit_codes == [EXIT_OK]
    finally:
        lock.release()


def _spawn_test_server_subprocess(
    *, state_dir: Path, lock_path: Path, port: int, behavior: str = "clean"
) -> subprocess.Popen[str]:
    script = f"""
import asyncio, dataclasses, os, sys
from pathlib import Path
import atlasrag.runtime.ownership as ownership_mod
ownership_mod.canonical_lock_path = lambda: Path({str(lock_path)!r})
from atlasrag.runtime.coordinator import ApplicationCoordinator
from atlasrag.runtime.ownership import RuntimeOwnershipLock
from atlasrag.server.config import ServerConfig
from atlasrag.server.launcher import ServerLauncher
from tests.stage11.conftest import build_test_dependencies, sample_runtime_config

state_dir = Path({str(state_dir)!r})
behavior = {behavior!r}
payload = sample_runtime_config().model_dump(mode="python")
payload["maintenance"]["shutdown_timeout_seconds"] = 1.0
runtime_cfg = type(sample_runtime_config()).model_validate(payload)
server_cfg = ServerConfig(host="127.0.0.1", port={port}, state_directory=str(state_dir))

async def factory(cfg, bus, settled):
    async def hang_close():
        await asyncio.sleep(30.0)
    deps, harness = build_test_dependencies(
        on_shutdown_settled=(lambda: None) if behavior == "no_receipt" else settled,
        close_resources=hang_close if behavior == "hang_shutdown" else None,
        shutdown_timeout_seconds=1.0,
    )
    harness.ownership = RuntimeOwnershipLock()
    deps_locked = dataclasses.replace(
        deps,
        ownership=harness.ownership,
        on_shutdown_settled=(lambda: None) if behavior == "no_receipt" else settled,
    )
    return await ApplicationCoordinator.start(deps_locked)

code = ServerLauncher(
    runtime_config=runtime_cfg,
    server_config=server_cfg,
    coordinator_factory=factory,
    log_level="warning",
).run()
raise SystemExit(code)
"""
    repo_root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": f"{repo_root / 'src'}:{repo_root}"}
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate()
            raise AssertionError(
                f"test subprocess exited early ({proc.returncode}): {out}\n{err}"
            )
        if server_discovery_path(state_dir).exists():
            break
        time.sleep(0.03)
    assert server_discovery_path(state_dir).exists()
    return proc


def test_cli_stop_verifies_pidfd_and_clean_receipt_and_new_server_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Real child server + stop clean exit, plus old exit & new startup race isolation."""

    state_dir = tmp_path / "state"
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)
    port = _unused_port()
    proc = _spawn_test_server_subprocess(
        state_dir=state_dir, lock_path=lock_path, port=port, behavior="clean"
    )
    try:
        code = main(
            ["stop", "--state-dir", str(state_dir), "--timeout", "5.0", "--json"],
            environ={},
        )
        assert code == 0
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "CLEAN_SHUTDOWN"
        assert result["pid"] == proc.pid
        assert proc.wait(timeout=3.0) == 0
        assert not shutdown_receipt_path(
            state_dir, result["server_instance_id"]
        ).exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_wpd_f02_original_process_exits_and_new_server_starts_while_stop_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """WPD-F02: When old server dies without receipt and new server starts, stop fails 4 and leaves new server intact."""

    state_dir = tmp_path / "state"
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)
    old_port = _unused_port()
    old_proc = _spawn_test_server_subprocess(
        state_dir=state_dir, lock_path=lock_path, port=old_port, behavior="no_receipt"
    )
    new_proc: subprocess.Popen[str] | None = None
    try:
        # Intercept _wait_pidfd_exit so after old_proc exits without receipt, a brand-new server starts in the SAME state_dir.
        orig_wait = cli_module._wait_pidfd_exit

        def wait_then_start_new(pidfd: int, timeout_s: float) -> bool:
            exited = bool(orig_wait(pidfd, timeout_s))
            old_proc.wait(timeout=3.0)
            nonlocal new_proc
            new_proc = _spawn_test_server_subprocess(
                state_dir=state_dir,
                lock_path=lock_path,
                port=_unused_port(),
                behavior="clean",
            )
            return exited

        monkeypatch.setattr(cli_module, "_wait_pidfd_exit", wait_then_start_new)
        code = main(
            ["stop", "--state-dir", str(state_dir), "--timeout", "5.0", "--json"],
            environ={},
        )
        assert code == 4
        assert json.loads(capsys.readouterr().out)["error_code"] == "UNVERIFIED_EXIT"
        assert new_proc is not None and new_proc.poll() is None
        # New server discovery must NOT have been touched or stopped by old stop!
        assert server_discovery_path(state_dir).exists()
    finally:
        if old_proc.poll() is None:
            old_proc.kill()
            old_proc.wait()
        if new_proc is not None and new_proc.poll() is None:
            new_proc.kill()
            new_proc.wait()


def test_cli_stop_exits_4_when_process_exits_without_clean_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)
    port = _unused_port()
    proc = _spawn_test_server_subprocess(
        state_dir=state_dir, lock_path=lock_path, port=port, behavior="no_receipt"
    )
    try:
        code = main(
            ["stop", "--state-dir", str(state_dir), "--timeout", "5.0", "--json"],
            environ={},
        )
        assert code == 4
        assert json.loads(capsys.readouterr().out)["error_code"] == "UNVERIFIED_EXIT"
        proc.wait(timeout=3.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_cli_stop_timeout_without_force_and_with_verified_pidfd_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)
    port = _unused_port()
    proc = _spawn_test_server_subprocess(
        state_dir=state_dir, lock_path=lock_path, port=port, behavior="hang_shutdown"
    )
    try:
        code1 = main(
            ["stop", "--state-dir", str(state_dir), "--timeout", "0.3", "--json"],
            environ={},
        )
        assert code1 == 1
        assert json.loads(capsys.readouterr().out)["error_code"] == "SHUTDOWN_TIMEOUT"
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()

    state_dir2 = tmp_path / "state2"
    port2 = _unused_port()
    proc2 = _spawn_test_server_subprocess(
        state_dir=state_dir2, lock_path=lock_path, port=port2, behavior="hang_shutdown"
    )
    try:
        code2 = main(
            [
                "stop",
                "--state-dir",
                str(state_dir2),
                "--timeout",
                "0.3",
                "--force",
                "--json",
            ],
            environ={},
        )
        assert code2 == 4
        out2 = json.loads(capsys.readouterr().out)
        assert out2["status"] == "FORCED_TERMINATION"
        assert out2["signal"] in {"SIGTERM", "SIGKILL"}
        assert out2["terminated"] is True
        assert proc2.wait(timeout=3.0) is not None
    finally:
        if proc2.poll() is None:
            proc2.kill()
            proc2.wait()


def test_cli_stop_force_refuses_missing_unsafe_reused_foreign_or_unlocked_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)

    empty_state = tmp_path / "empty"
    empty_state.mkdir()
    assert (
        main(["stop", "--state-dir", str(empty_state), "--force", "--json"], environ={})
        == 4
    )
    assert main(["stop", "--state-dir", str(empty_state), "--json"], environ={}) == 3
    capsys.readouterr()

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        state_dir = tmp_path / "verify_fail"
        ticks = read_proc_stat_starttime(sleeper.pid)
        boot = read_boot_id()
        write_secure_local_file(control_token_path(state_dir), "tok")

        signal_calls: list[int] = []
        monkeypatch.setattr(
            signal, "pidfd_send_signal", lambda _fd, sig: signal_calls.append(sig)
        )

        # 1. Canonical lock held by THIS pytest process (NOT sleeper.pid!) -> even with valid sleeper pid & ticks, must REFUSE!
        lock = RuntimeOwnershipLock(lock_path)
        lock.acquire()
        write_secure_local_file(
            server_discovery_path(state_dir),
            json.dumps(
                {
                    "url": "http://127.0.0.1:8999",
                    "host": "127.0.0.1",
                    "port": 8999,
                    "pid": sleeper.pid,
                    "process_start_ticks": ticks,
                    "boot_id": boot,
                    "server_instance_id": "inst-foreign-lock",
                    "started_at": "2026-01-01T00:00:00Z",
                }
            ),
        )
        assert (
            main(
                ["stop", "--state-dir", str(state_dir), "--force", "--json"], environ={}
            )
            == 4
        )
        foreign_err = json.loads(capsys.readouterr().out)
        assert "canonical runtime ownership lock" in foreign_err["message"]
        assert not signal_calls

        # 2. PID start time mismatch -> exit 4
        write_secure_local_file(
            server_discovery_path(state_dir),
            json.dumps(
                {
                    "url": "http://127.0.0.1:8999",
                    "host": "127.0.0.1",
                    "port": 8999,
                    "pid": sleeper.pid,
                    "process_start_ticks": ticks + 9999,
                    "boot_id": boot,
                    "server_instance_id": "inst-mismatch",
                    "started_at": "2026-01-01T00:00:00Z",
                }
            ),
        )
        assert (
            main(
                ["stop", "--state-dir", str(state_dir), "--force", "--json"], environ={}
            )
            == 4
        )
        assert not signal_calls

        # 3. boot_id mismatch -> exit 4
        write_secure_local_file(
            server_discovery_path(state_dir),
            json.dumps(
                {
                    "url": "http://127.0.0.1:8999",
                    "host": "127.0.0.1",
                    "port": 8999,
                    "pid": sleeper.pid,
                    "process_start_ticks": ticks,
                    "boot_id": "wrong-boot-id",
                    "server_instance_id": "inst-boot",
                    "started_at": "2026-01-01T00:00:00Z",
                }
            ),
        )
        assert (
            main(
                ["stop", "--state-dir", str(state_dir), "--force", "--json"], environ={}
            )
            == 4
        )
        assert not signal_calls

        # 4. Decoy lock placed inside state_dir while canonical lock_path is unlocked -> exit 4
        lock.release()
        decoy_state_lock = RuntimeOwnershipLock(state_dir / "runtime.lock")
        decoy_state_lock.acquire()
        write_secure_local_file(
            server_discovery_path(state_dir),
            json.dumps(
                {
                    "url": "http://127.0.0.1:8999",
                    "host": "127.0.0.1",
                    "port": 8999,
                    "pid": sleeper.pid,
                    "process_start_ticks": ticks,
                    "boot_id": boot,
                    "server_instance_id": "inst-decoy-dir-lock",
                    "started_at": "2026-01-01T00:00:00Z",
                }
            ),
        )
        assert (
            main(
                ["stop", "--state-dir", str(state_dir), "--force", "--json"], environ={}
            )
            == 4
        )
        assert not signal_calls
        decoy_state_lock.release()
        capsys.readouterr()
    finally:
        sleeper.kill()
        sleeper.wait()


def test_cli_signal_exit_codes_for_thin_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"

    def _sigint_client(*_args: object, **_kwargs: object) -> httpx.Client:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "_build_client", _sigint_client)
    assert main(["health", "--state-dir", str(state_dir)], environ={}) == 130

    def _sigterm_client(*_args: object, **_kwargs: object) -> httpx.Client:
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("unreachable")

    monkeypatch.setattr(cli_module, "_build_client", _sigterm_client)
    assert main(["health", "--state-dir", str(state_dir)], environ={}) == 143


def test_parser_subcommands_cover_stage11_contract() -> None:
    parser = build_parser()
    for argv in [
        ["serve", "--host", "127.0.0.1", "--port", "8000"],
        ["ask", "hello", "--stream", "--timeout", "10.0"],
        ["health", "--live"],
        ["health", "--ready"],
        ["status", "--refresh"],
        ["sync", "--no-wait", "--poll-interval", "0.5"],
        ["recover", "--wait", "--poll-interval", "0.5"],
        ["stop", "--timeout", "5.0", "--force"],
    ]:
        ns = parser.parse_args(argv)
        assert ns.command == argv[0]


def test_cli_recover_no_wait_and_human_text_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)
    port = _unused_port()

    async def blocked_reconcile() -> ReconciliationResult:
        return ReconciliationResult(
            outcome=ReconciliationOutcome.BLOCKED,
            blocked_reasons=("UNVERIFIED_CORPUS",),
            checked_documents=1,
        )

    async def cited_retrieval(_req: UserTurnRequest) -> FinalResponse:
        return FinalResponse(
            answer_text="Answer with local and web citations.",
            citations=(
                FinalLocalCitation(
                    citation_id="L1",
                    file_name="manual.pdf",
                    relative_source_path="docs/manual.pdf",
                    evidence_type=EvidenceType.TEXT_PARENT,
                    source_anchor=SourceAnchor(page_number=2),
                ),
                FinalWebCitation(
                    citation_id="W1",
                    title="Example Reference",
                    url=HttpUrl("https://example.com/ref"),
                    domain="example.com",
                ),
            ),
        )

    async def healthy_recover() -> SyncResult:
        return SyncResult(
            outcome=SyncOutcome.SUCCESS, serving_safety=ServingSafety.SAFE_TO_SERVE
        )

    thread, exit_codes, lock = _run_launcher_in_thread(
        state_dir=state_dir,
        port=port,
        sync_fn=healthy_recover,
        reconcile_fn=blocked_reconcile,
        retrieval=cited_retrieval,
    )
    try:
        assert main(["health", "--state-dir", str(state_dir)], environ={}) == 1
        assert "ready=False" in capsys.readouterr().out

        assert (
            main(["health", "--live", "--state-dir", str(state_dir)], environ={}) == 0
        )
        assert "status=ALIVE" in capsys.readouterr().out

        assert main(["status", "--state-dir", str(state_dir)], environ={}) == 0
        status_text = capsys.readouterr().out
        assert "mode=BLOCKED" in status_text
        assert "dependencies:" in status_text

        assert (
            main(
                ["recover", "--poll-interval", "0.05", "--state-dir", str(state_dir)],
                environ={},
            )
            == 0
        )
        assert "serving_safety=SAFE_TO_SERVE" in capsys.readouterr().out

        assert (
            main(["ask", "Explain manual", "--state-dir", str(state_dir)], environ={})
            == 0
        )
        ask_text = capsys.readouterr().out
        assert "Answer with local and web citations." in ask_text
        assert "[L1] manual.pdf (docs/manual.pdf) (p.2)" in ask_text
        assert "[W1] Example Reference: https://example.com/ref" in ask_text

        assert (
            main(["sync", "--no-wait", "--state-dir", str(state_dir)], environ={}) == 0
        )
        assert "operation=op_" in capsys.readouterr().out

        time.sleep(0.15)
        _stop_threaded_server(state_dir=state_dir, port=port, thread=thread)
        assert exit_codes == [EXIT_OK]
    finally:
        lock.release()


def test_e0_01_maintenance_control_stability_and_replacement_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """E0-01: sync and recover verify instance/pidfd/flock/discovery/token stability before POST and each poll."""

    lock_path = tmp_path / "canonical" / "runtime.lock"
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_path)
    orig_verify = cli_module.verify_target_process
    orig_stability = cli_module._verify_pre_shutdown_stability

    for cmd in ("sync", "recover"):
        state_dir = tmp_path / f"state_{cmd}"
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        try:
            ticks = read_proc_stat_starttime(sleeper.pid)
            boot = read_boot_id()
            auth_headers_seen: list[str] = []
            get_requests = 0
            post_requests = 0
            op_id = "op_" + "a" * 24
            expected_kind = cmd.upper()

            class _ControlRaceHandler(BaseHTTPRequestHandler):
                def do_POST(self) -> None:
                    nonlocal post_requests
                    post_requests += 1
                    auth_headers_seen.append(str(self.headers.get("authorization")))
                    body = json.dumps(
                        {
                            "operation_id": op_id,
                            "kind": expected_kind,
                            "status": "RUNNING",
                            "accepted_at": "2026-01-01T00:00:00Z",
                        }
                    ).encode()
                    self.send_response(202)
                    self.send_header("content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self) -> None:
                    nonlocal get_requests
                    get_requests += 1
                    body = json.dumps(
                        {
                            "operation_id": op_id,
                            "kind": expected_kind,
                            "status": "RUNNING",
                            "runtime_mode": "MAINTENANCE",
                            "started_at": "2026-01-01T00:00:00Z",
                        }
                    ).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *_args: object) -> None:
                    return

            srv = HTTPServer(("127.0.0.1", 0), _ControlRaceHandler)
            srv_port = int(srv.server_address[1])
            srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
            srv_thread.start()
            try:
                monkeypatch.setattr(
                    cli_module,
                    "verify_target_process",
                    lambda sd: orig_verify(sd, lock_path=lock_path),
                )
                import atlasrag.server.auth as auth_mod

                monkeypatch.setattr(
                    auth_mod, "verify_process_holds_flock", lambda _p, _lp: True
                )
                monkeypatch.setattr(
                    cli_module, "verify_process_holds_flock", lambda _p, _lp: True
                )

                write_secure_local_file(control_token_path(state_dir), "orig-token")
                write_secure_local_file(
                    server_discovery_path(state_dir),
                    json.dumps(
                        {
                            "url": f"http://127.0.0.1:{srv_port}",
                            "host": "127.0.0.1",
                            "port": srv_port,
                            "pid": sleeper.pid,
                            "process_start_ticks": ticks,
                            "boot_id": boot,
                            "server_instance_id": "inst-orig",
                            "started_at": "2026-01-01T00:00:00Z",
                        }
                    ),
                )

                # Case A: Pre-POST replacement rotated server_instance_id -> refuse HTTP send altogether.
                def rotate_before_post(**kwargs: object) -> bool:
                    write_secure_local_file(
                        control_token_path(state_dir), "replacement-token"
                    )
                    return bool(orig_stability(**kwargs))

                monkeypatch.setattr(
                    cli_module, "_verify_pre_shutdown_stability", rotate_before_post
                )
                code_a = main(
                    [
                        cmd,
                        "--state-dir",
                        str(state_dir),
                        "--poll-interval",
                        "0.02",
                        "--json",
                    ],
                    environ={},
                )
                assert code_a == 4
                assert post_requests == 0
                assert get_requests == 0
                assert (
                    json.loads(capsys.readouterr().out)["error_code"]
                    == "UNVERIFIED_PROCESS_IDENTITY"
                )

                # Restore orig token and test Case B: replacement occurs DURING polling after 202.
                write_secure_local_file(control_token_path(state_dir), "orig-token")
                checks = 0

                def rotate_after_post(**kwargs: object) -> bool:
                    nonlocal checks
                    checks += 1
                    if checks >= 2:
                        write_secure_local_file(
                            server_discovery_path(state_dir),
                            json.dumps(
                                {
                                    "url": f"http://127.0.0.1:{srv_port}",
                                    "host": "127.0.0.1",
                                    "port": srv_port,
                                    "pid": sleeper.pid,
                                    "process_start_ticks": ticks,
                                    "boot_id": boot,
                                    "server_instance_id": "inst-replacement",
                                    "started_at": "2026-01-01T00:00:00Z",
                                }
                            ),
                        )
                    return bool(orig_stability(**kwargs))

                monkeypatch.setattr(
                    cli_module, "_verify_pre_shutdown_stability", rotate_after_post
                )
                code_b = main(
                    [
                        cmd,
                        "--state-dir",
                        str(state_dir),
                        "--poll-interval",
                        "0.02",
                        "--json",
                    ],
                    environ={},
                )
                assert code_b == 4
                assert post_requests == 1
                # Must detect replacement BEFORE sending GET poll carrying orig-token!
                assert get_requests == 0
                assert (
                    json.loads(capsys.readouterr().out)["error_code"]
                    == "UNVERIFIED_PROCESS_IDENTITY"
                )
            finally:
                srv.shutdown()
                srv.server_close()
        finally:
            sleeper.kill()
            sleeper.wait()


def test_e0_02_stop_against_real_subprocesses_post_release_exit_and_release_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """E0-02 cases 9, 14, 15: subprocess exit post-release without receipt or release failure fails closed (4)."""

    lock_path = tmp_path / "canonical" / "runtime.lock"

    def _spawn_variant(state_dir: Path, variant: str) -> subprocess.Popen[str]:
        port = _unused_port()
        repo_root = Path(__file__).resolve().parents[2]
        script = f"""
import asyncio, dataclasses, fcntl, os, signal, sys
from pathlib import Path
import atlasrag.runtime.ownership as ownership_mod
ownership_mod.canonical_lock_path = lambda: Path({str(lock_path)!r})
from atlasrag.runtime.coordinator import ApplicationCoordinator
from atlasrag.runtime.ownership import RuntimeOwnershipLock
from atlasrag.server import launcher as launcher_mod
from atlasrag.server.config import ServerConfig
from atlasrag.server.launcher import ServerLauncher
from tests.stage11.conftest import build_test_dependencies, sample_runtime_config

state_dir = Path({str(state_dir)!r})
variant = {variant!r}
payload = sample_runtime_config().model_dump(mode="python")
payload["maintenance"]["shutdown_timeout_seconds"] = 1.0
runtime_cfg = type(sample_runtime_config()).model_validate(payload)
server_cfg = ServerConfig(host="127.0.0.1", port={port}, state_directory=str(state_dir))

async def factory(cfg, bus, settled):
    deps, harness = build_test_dependencies(
        on_shutdown_settled=settled,
        shutdown_timeout_seconds=1.0,
    )
    harness.ownership = RuntimeOwnershipLock()
    deps_locked = dataclasses.replace(deps, ownership=harness.ownership, on_shutdown_settled=settled)
    coord = await ApplicationCoordinator.start(deps_locked)
    orig_release = harness.ownership.release
    if variant == "exit_after_release_before_receipt":
        def exit_early():
            orig_release()
            os._exit(0)
        harness.ownership.release = exit_early
    elif variant == "release_fails_after_unlock":
        orig_flock = ownership_mod.fcntl.flock
        def bad_unlock(fd, op):
            if op == fcntl.LOCK_UN:
                raise OSError("unlock error")
            return orig_flock(fd, op)
        ownership_mod.fcntl.flock = bad_unlock
    return coord

code = ServerLauncher(
    runtime_config=runtime_cfg,
    server_config=server_cfg,
    coordinator_factory=factory,
    log_level="warning",
).run()
raise SystemExit(code)
"""
        env = {**os.environ, "PYTHONPATH": f"{repo_root / 'src'}:{repo_root}"}
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise AssertionError(f"subprocess exited early: {out}\n{err}")
            if server_discovery_path(state_dir).exists():
                return proc
            time.sleep(0.03)
        raise AssertionError("timed out waiting for server.json")

    for variant in ("exit_after_release_before_receipt", "release_fails_after_unlock"):
        sd = tmp_path / variant
        proc = _spawn_variant(sd, variant)
        try:
            old_fn = ownership_module.canonical_lock_path
            ownership_module.canonical_lock_path = lambda: lock_path
            try:
                stop_code = main(
                    ["stop", "--state-dir", str(sd), "--timeout", "5.0", "--json"],
                    environ={},
                )
            finally:
                ownership_module.canonical_lock_path = old_fn
            assert stop_code == 4
            assert (
                json.loads(capsys.readouterr().out)["error_code"] == "UNVERIFIED_EXIT"
            )
            assert proc.wait(timeout=4.0) is not None
            assert not list(sd.glob("shutdown-*.json"))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
