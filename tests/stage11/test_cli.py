"""WP-D unit and local subprocess tests for the thin AtlasRAG CLI."""

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
from pathlib import Path

import httpx
import pytest
from pydantic import HttpUrl

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
    ready_event = threading.Event()
    lock = RuntimeOwnershipLock(state_dir / "runtime.lock")

    async def factory(
        cfg: RuntimeConfig,
        bus: InvocationEventBus,
        settled: Callable[[], Awaitable[None] | None],
        _lp: Path | None,
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
    ready_event.set()
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


def test_cli_discovery_precedence_and_non_loopback_token_guard(
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

    # 1. --server-url beats ATLASRAG_SERVER_URL and server.json
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

    # 2. ATLASRAG_SERVER_URL beats server.json
    assert (
        main(
            ["health", "--state-dir", str(state_dir)],
            environ={"ATLASRAG_SERVER_URL": "http://localhost:9002"},
        )
        == 3
    )
    assert seen_base_urls[-1] == "http://localhost:9002"

    # 3. server.json beats default :8000
    assert main(["health", "--state-dir", str(state_dir)], environ={}) == 3
    assert seen_base_urls[-1] == "http://127.0.0.1:8123"

    # 4. Non-loopback target fails BEFORE reading control.token!
    read_calls: list[Path] = []
    orig_read = read_secure_local_file

    def _spy_read(path: Path, *, max_bytes: int = 8192) -> str:
        read_calls.append(path)
        return orig_read(path, max_bytes=max_bytes)

    monkeypatch.setattr(cli_module, "read_secure_local_file", _spy_read)
    for bad_url in [
        "http://evil.example.com:8000",
        "http://user:pass@127.0.0.1:8000",
        "ftp://127.0.0.1:8000",
    ]:
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


def test_cli_ask_session_reuse_auto_renew_stream_filters_and_maintenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
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
        # 1. health --live / --ready and status --refresh
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
        status_json = json.loads(capsys.readouterr().out)
        assert status_json["mode"] == "SERVING"

        # 2. First ask creates cli_session.json (0o600)
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

        # 3. Second ask reuses the same cli_session.json session_id and streams SSE cleanly
        assert (
            main(
                [
                    "ask",
                    "Streamed question",
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

        # 4. Stale cached session_id -> server returns 404 SESSION_NOT_FOUND -> CLI auto-renews once!
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
            main(
                ["ask", "Renew question", "--state-dir", str(state_dir), "--json"],
                environ={},
            )
            == 0
        )
        renewed_out = json.loads(capsys.readouterr().out)
        assert renewed_out["session_id"] != "sess_expired_unknown"

        # 5. Pinned --session-id unknown fails exit 1 without silently renewing.
        assert (
            main(
                [
                    "ask",
                    "Bad session",
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

        # 6. Invalid ask args -> exit 2
        assert main(["ask", "   ", "--state-dir", str(state_dir)], environ={}) == 2
        assert (
            main(
                ["ask", "q", "--timeout", "0", "--state-dir", str(state_dir)],
                environ={},
            )
            == 2
        )

        # 7. sync SAFE_TO_SERVE -> exit 0; UNSAFE_TO_SERVE -> exit 1!
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
        sync_ok = json.loads(capsys.readouterr().out)
        assert sync_ok["serving_safety"] == "SAFE_TO_SERVE"

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
        sync_unsafe = json.loads(capsys.readouterr().out)
        assert sync_unsafe["serving_safety"] == "UNSAFE_TO_SERVE"

        # 8. HTTP shutdown ends thread cleanly
        token = read_secure_local_file(control_token_path(state_dir))
        with httpx.Client(follow_redirects=False, trust_env=False) as client:
            stop_r = client.post(
                f"http://127.0.0.1:{port}/api/v1/control/shutdown",
                json={},
                headers={
                    "origin": f"http://127.0.0.1:{port}",
                    "authorization": f"Bearer {token}",
                },
            )
            assert stop_r.status_code == 202
        thread.join(timeout=5.0)
        assert exit_codes == [EXIT_OK]
    finally:
        lock.release()


def _spawn_test_server_subprocess(
    *, state_dir: Path, port: int, behavior: str = "clean"
) -> subprocess.Popen[str]:
    script = f"""
import asyncio, dataclasses, os, sys
from pathlib import Path
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

async def factory(cfg, bus, settled, lp):
    async def hang_close():
        await asyncio.sleep(30.0)
    deps, harness = build_test_dependencies(
        on_shutdown_settled=(lambda: None) if behavior == "no_receipt" else settled,
        close_resources=hang_close if behavior == "hang_shutdown" else None,
        shutdown_timeout_seconds=1.0,
    )
    if lp is not None:
        harness.ownership = RuntimeOwnershipLock(lp)
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


def test_cli_stop_verifies_pidfd_and_clean_receipt_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    port = _unused_port()
    proc = _spawn_test_server_subprocess(
        state_dir=state_dir, port=port, behavior="clean"
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
        # Receipt consumed and cleaned up after verified exit.
        assert not shutdown_receipt_path(
            state_dir, result["server_instance_id"]
        ).exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_cli_stop_exits_4_when_process_exits_without_clean_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    port = _unused_port()
    proc = _spawn_test_server_subprocess(
        state_dir=state_dir, port=port, behavior="no_receipt"
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
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    port = _unused_port()
    proc = _spawn_test_server_subprocess(
        state_dir=state_dir, port=port, behavior="hang_shutdown"
    )
    try:
        # 1. Without --force: times out -> exit 1; process stays alive!
        code1 = main(
            ["stop", "--state-dir", str(state_dir), "--timeout", "0.3", "--json"],
            environ={},
        )
        assert code1 == 1
        assert json.loads(capsys.readouterr().out)["error_code"] == "SHUTDOWN_TIMEOUT"
        assert proc.poll() is None

        # 2. Server.json already removed on graceful request, or write same identity so pre-shutdown verify works for --force test
        # Re-test --force against a fresh hanging subprocess before first shutdown:
    finally:
        proc.kill()
        proc.wait()

    state_dir2 = tmp_path / "state2"
    port2 = _unused_port()
    proc2 = _spawn_test_server_subprocess(
        state_dir=state_dir2, port=port2, behavior="hang_shutdown"
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
        assert proc2.wait(timeout=3.0) is not None
    finally:
        if proc2.poll() is None:
            proc2.kill()
            proc2.wait()


def test_cli_stop_force_refuses_missing_unsafe_reused_or_unlocked_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. Missing server.json with --force -> exit 4 without HTTP/signal
    empty_state = tmp_path / "empty"
    empty_state.mkdir()
    assert (
        main(["stop", "--state-dir", str(empty_state), "--force", "--json"], environ={})
        == 4
    )
    assert main(["stop", "--state-dir", str(empty_state), "--json"], environ={}) == 3
    capsys.readouterr()

    # 2. Prepare valid live child sleeping process + tampered metadata checks
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        state_dir = tmp_path / "verify_fail"
        lock = RuntimeOwnershipLock(state_dir / "runtime.lock")
        lock.acquire()
        ticks = read_proc_stat_starttime(sleeper.pid)
        boot = read_boot_id()
        write_secure_local_file(control_token_path(state_dir), "tok")

        signal_calls: list[int] = []
        monkeypatch.setattr(
            signal, "pidfd_send_signal", lambda fd, sig: signal_calls.append(sig)
        )

        # 2a. PID start time mismatch (PID reuse simulation) -> exit 4, zero signal
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

        # 2b. boot_id mismatch -> exit 4
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

        # 2c. runtime.lock released (not held) -> exit 4
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
                    "server_instance_id": "inst-unlocked",
                    "started_at": "2026-01-01T00:00:00Z",
                }
            ),
        )
        lock.release()
        assert (
            main(
                ["stop", "--state-dir", str(state_dir), "--force", "--json"], environ={}
            )
            == 4
        )
        assert not signal_calls
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
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
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
        # 1. Blocked server -> health --ready returns 1, health --live returns 0, status returns 0
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

        # 2. recover restores SERVING (with --wait -> exit 0)
        assert (
            main(
                ["recover", "--poll-interval", "0.05", "--state-dir", str(state_dir)],
                environ={},
            )
            == 0
        )
        recover_text = capsys.readouterr().out
        assert "serving_safety=SAFE_TO_SERVE" in recover_text

        # 3. ask text output prints answer_text + local/web citations + footer
        assert (
            main(
                ["ask", "Explain manual", "--state-dir", str(state_dir)],
                environ={},
            )
            == 0
        )
        ask_text = capsys.readouterr().out
        assert "Answer with local and web citations." in ask_text
        assert "[L1] manual.pdf (docs/manual.pdf) (p.2)" in ask_text
        assert "[W1] Example Reference: https://example.com/ref" in ask_text
        assert "[cache=" in ask_text

        # 4. sync --no-wait exits 0 immediately after 202 acceptance
        assert (
            main(["sync", "--no-wait", "--state-dir", str(state_dir)], environ={}) == 0
        )
        assert "operation=op_" in capsys.readouterr().out

        # Wait briefly so background no-wait sync finishes before shutdown
        time.sleep(0.15)
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
        assert exit_codes == [EXIT_OK]
    finally:
        lock.release()
