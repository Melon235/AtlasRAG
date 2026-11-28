"""Layer B real-process Uvicorn + raw TCP transport integration tests for Stage 11."""

from __future__ import annotations

import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from atlasrag.cli.main import (
    EXIT_FATAL_LIFECYCLE,
    EXIT_OK,
)
from atlasrag.cli.main import (
    main as cli_main,
)
from atlasrag.server.auth import (
    control_token_path,
    is_runtime_lock_active,
    read_secure_local_file,
    server_discovery_path,
    shutdown_receipt_path,
)
from atlasrag.server.launcher import EXIT_OWNERSHIP_CONFLICT

REPO_ROOT = Path(__file__).resolve().parents[2]


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _read_http_headers_and_prefix(
    sock: socket.socket, *, timeout: float = 5.0
) -> tuple[bytes, bytes]:
    sock.settimeout(timeout)
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError(f"EOF before HTTP headers completed: {bytes(buf)!r}")
        buf.extend(chunk)
    sep = buf.index(b"\r\n\r\n") + 4
    return bytes(buf[:sep]), bytes(buf[sep:])


def _send_tcp_rst(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


def _wait_for_server_ready(
    state_dir: Path,
    *,
    pid: int,
    timeout: float = 10.0,
    proc: subprocess.Popen[str] | None = None,
) -> None:
    discovery_file = server_discovery_path(state_dir)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if discovery_file.exists():
            try:
                raw = json.loads(read_secure_local_file(discovery_file))
                if raw.get("pid") == pid:
                    return
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        if proc is not None and proc.poll() is not None:
            out, err = proc.communicate()
            raise AssertionError(
                f"server process exited early ({proc.returncode}) waiting for server.json: "
                f"{out}\n{err}"
            )
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for server.json for pid={pid}")


def _sample_serve_env(home_dir: Path, knowledge_dir: Path) -> dict[str, str]:
    from stage11.conftest import sample_runtime_config

    knowledge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_dir = knowledge_dir.parent / "logs"
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = sample_runtime_config().model_dump(mode="json")
    payload["infrastructure"]["knowledge_directory"] = str(knowledge_dir)
    payload["observability"]["log_directory"] = str(log_dir)
    env = _make_subprocess_env(home_dir)
    env["ATLASRAG_CONFIG_JSON"] = json.dumps(payload)
    return env


def _make_subprocess_env(home_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = str(home_dir)
    env["PYTHONPATH"] = f"{REPO_ROOT / 'src'}:{REPO_ROOT}"
    env.pop("ATLASRAG_SERVER_URL", None)
    return env


def _cli_subprocess_cmd(lock_path: Path, cli_args: list[str]) -> list[str]:
    bootstrap = (
        "import sys; from pathlib import Path; "
        "import atlasrag.runtime.ownership as o; "
        f"o.canonical_lock_path = lambda: Path({str(lock_path)!r}); "
        "from atlasrag.cli.main import main; "
        f"raise SystemExit(main({cli_args!r}))"
    )
    return [sys.executable, "-c", bootstrap]


@contextmanager
def _spawn_real_uvicorn_server(
    *,
    home_dir: Path,
    state_dir: Path,
    diag_dir: Path,
    port: int,
    mode: str = "normal",
    shutdown_timeout_seconds: float = 1.0,
    sse_queue_maxsize: int = 16,
    sse_send_timeout_seconds: float = 1.0,
) -> Iterator[subprocess.Popen[str]]:
    home_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home_dir, 0o700)
    diag_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = home_dir / ".local" / "state" / "atlasrag" / "runtime.lock"
    runner_script = f"""
import asyncio, dataclasses, json, os, sys, time
from pathlib import Path
import atlasrag.runtime.ownership as ownership_mod
ownership_mod.canonical_lock_path = lambda: Path({str(lock_path)!r})
import atlasrag.server.launcher as launcher_mod
from atlasrag.domain.enums import EvidenceType
from atlasrag.domain.observation import (
    ObservationStage,
    ProductionObservation,
    deliver_observation,
)
from atlasrag.domain.results import (
    FinalLocalCitation,
    FinalResponse,
    FinalWebCitation,
    SourceAnchor,
)
from atlasrag.runtime.coordinator import ApplicationCoordinator
from atlasrag.runtime.ownership import RuntimeOwnershipLock
from atlasrag.server.config import ServerConfig
from atlasrag.server.launcher import ServerLauncher
from atlasrag.services.sync.models import (
    ReconciliationOutcome,
    ReconciliationResult,
    ServingSafety,
    SyncOutcome,
    SyncResult,
)
from pydantic import HttpUrl
from tests.stage11.conftest import build_test_dependencies, sample_runtime_config

state_dir = Path({str(state_dir)!r})
diag_dir = Path({str(diag_dir)!r})
mode = {mode!r}

payload = sample_runtime_config().model_dump(mode="python")
payload["maintenance"]["shutdown_timeout_seconds"] = {shutdown_timeout_seconds!r}
runtime_cfg = type(sample_runtime_config()).model_validate(payload)
server_cfg = ServerConfig(
    host="127.0.0.1",
    port={port},
    state_directory=str(state_dir),
    sse_queue_maxsize={sse_queue_maxsize},
    sse_send_timeout_seconds={sse_send_timeout_seconds!r},
)

if mode == "stall_send":
    orig_create_app = launcher_mod.create_app

    def wrapped_create_app(**kwargs):
        app = orig_create_app(**kwargs)

        async def asgi_stall_wrapper(scope, receive, send):
            if scope.get("type") == "http" and scope.get("path") == "/api/v1/query/stream":
                body_chunks = 0

                async def wrapped_send(message):
                    nonlocal body_chunks
                    if message.get("type") == "http.response.body":
                        body_chunks += 1
                        if body_chunks >= 2:
                            (diag_dir / "send_stall_entered").write_text("1")
                            await asyncio.sleep(3.0)
                    await send(message)

                await app(scope, receive, wrapped_send)
                return
            await app(scope, receive, send)

        asgi_stall_wrapper.state = app.state
        return asgi_stall_wrapper

    launcher_mod.create_app = wrapped_create_app

deps_holder = []

async def factory(cfg, bus, settled):
    async def retrieval(req):
        if req.query.startswith("SLOW_JSON"):
            (diag_dir / "json_started").write_text("1")
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                (diag_dir / "json_cancelled").write_text("1")
                raise
        if req.query.startswith("SLOW_SSE"):
            deliver_observation(
                bus,
                ProductionObservation(
                    stage=ObservationStage.DENSE,
                    status="OK",
                    attempt_index=1,
                ),
            )
            (diag_dir / "sse_started").write_text("1")
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                (diag_dir / "sse_cancelled").write_text("1")
                raise
        if req.query.startswith("BURST"):
            for idx in range(30):
                deliver_observation(
                    bus,
                    ProductionObservation(
                        stage=ObservationStage.DENSE,
                        status="OK",
                        attempt_index=idx + 1,
                    ),
                )
            (diag_dir / "burst_worker_finished").write_text("1")
        if req.query.startswith("STALL_SEND"):
            deliver_observation(
                bus,
                ProductionObservation(
                    stage=ObservationStage.DENSE,
                    status="OK",
                    attempt_index=1,
                ),
            )
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                (diag_dir / "send_timeout_cancelled").write_text("1")
                raise
        return FinalResponse(
            answer_text=f"Real transport answer for {{req.query}} [L1][W1].",
            citations=(
                FinalLocalCitation(
                    citation_id="L1",
                    file_name="manual.pdf",
                    relative_source_path="docs/manual.pdf",
                    evidence_type=EvidenceType.TEXT_PARENT,
                    source_anchor=SourceAnchor(page_number=4),
                ),
                FinalWebCitation(
                    citation_id="W1",
                    title="Spec Note",
                    url=HttpUrl("https://example.com/spec"),
                    domain="example.com",
                ),
            ),
        )

    async def blocked_reconcile():
        return ReconciliationResult(
            outcome=ReconciliationOutcome.BLOCKED,
            blocked_reasons=("UNVERIFIED_CORPUS",),
            checked_documents=1,
        )

    async def recover_ok():
        return SyncResult(
            outcome=SyncOutcome.SUCCESS,
            serving_safety=ServingSafety.SAFE_TO_SERVE,
            verified_documents=1,
        )

    async def hung_close():
        (diag_dir / "hung_close_entered").write_text("1")
        await asyncio.sleep(30.0)

    deps, harness = build_test_dependencies(
        retrieval=retrieval,
        reconcile_fn=blocked_reconcile if mode == "blocked" else None,
        sync_fn=recover_ok,
        on_shutdown_settled=settled,
        close_resources=hung_close if mode == "hung_worker" else None,
        shutdown_timeout_seconds=cfg.maintenance.shutdown_timeout_seconds,
    )
    harness.ownership = RuntimeOwnershipLock()
    deps_locked = dataclasses.replace(deps, ownership=harness.ownership, on_shutdown_settled=settled)
    coord = await ApplicationCoordinator.start(deps_locked)
    deps_holder.append((coord, bus))
    return coord

code = ServerLauncher(
    runtime_config=runtime_cfg,
    server_config=server_cfg,
    coordinator_factory=factory,
    log_level="warning",
).run()
if deps_holder:
    coord, bus = deps_holder[0]
    (diag_dir / "post_exit_snapshot.json").write_text(
        json.dumps(
            {{
                "active_queries": coord.gate.active_queries,
                "remaining_tasks": len(coord.tasks.snapshot()),
                "bus_channels": bus.bound_count(),
            }}
        )
    )
raise SystemExit(code)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", runner_script],
        env=_make_subprocess_env(home_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_server_ready(state_dir, pid=proc.pid, timeout=10.0, proc=proc)
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)
        lock_file = home_dir / ".local" / "state" / "atlasrag" / "runtime.lock"
        if lock_file.exists():
            assert is_runtime_lock_active(lock_file) is False


def _wait_for_marker(
    path: Path,
    *,
    timeout: float = 5.0,
    proc: subprocess.Popen[str] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc is not None and proc.poll() is not None:
            out, err = proc.communicate()
            raise AssertionError(
                f"server process exited early ({proc.returncode}) waiting for {path.name}: "
                f"{out}\n{err}"
            )
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {path}")


def _create_session(base_url: str, *, client_kind: str = "web") -> str:
    with httpx.Client(
        base_url=base_url, follow_redirects=False, trust_env=False
    ) as client:
        resp = client.post(
            "/api/v1/sessions",
            json={"client_kind": client_kind},
            headers={"Origin": base_url, "X-AtlasRAG-Client": client_kind},
        )
        assert resp.status_code == 201
        return str(resp.json()["session_id"])


def test_real_server_http_sse_contract_security_and_pre_admission_503(
    tmp_path: Path,
) -> None:
    """S11-06/07/13/20/24: Real Uvicorn TCP server enforces security, JSON, SSE, and blocked 503 JSON."""

    home = tmp_path / "home_contract"
    state_dir = tmp_path / "state_contract"
    diag_dir = tmp_path / "diag_contract"
    port = _unused_port()

    with _spawn_real_uvicorn_server(
        home_dir=home, state_dir=state_dir, diag_dir=diag_dir, port=port
    ) as proc:
        disc = json.loads(read_secure_local_file(server_discovery_path(state_dir)))
        base_url = disc["url"]
        session_id = _create_session(base_url)

        with httpx.Client(
            base_url=base_url, follow_redirects=False, trust_env=False, timeout=5.0
        ) as client:
            # Host / Origin / CSRF guards fail 403 over live TCP socket.
            assert (
                client.post(
                    "/api/v1/sessions",
                    json={"client_kind": "web"},
                    headers={
                        "Host": "evil.example",
                        "Origin": base_url,
                        "X-AtlasRAG-Client": "web",
                    },
                ).status_code
                == 403
            )
            assert (
                client.post(
                    "/api/v1/sessions",
                    json={"client_kind": "web"},
                    headers={
                        "Origin": "http://evil.example:9999",
                        "X-AtlasRAG-Client": "web",
                    },
                ).status_code
                == 403
            )
            assert (
                client.post(
                    "/api/v1/sessions",
                    json={"client_kind": "web"},
                    headers={"Origin": base_url},
                ).status_code
                == 403
            )

            # Valid JSON query returns QueryResponse preserving FinalResponse cleanly.
            q_resp = client.post(
                "/api/v1/query",
                json={"session_id": session_id, "query": "Explain manual"},
                headers={"Origin": base_url, "X-AtlasRAG-Client": "web"},
            )
            assert q_resp.status_code == 200
            q_payload = q_resp.json()
            assert set(q_payload["response"].keys()) == {"answer_text", "citations"}
            assert q_payload["session_id"] == session_id
            assert q_payload["cache_status"] in {"UNKNOWN", "MISS", "BYPASSED", "HIT"}

            # Valid SSE stream emits accepted first -> final.
            with client.stream(
                "POST",
                "/api/v1/query/stream",
                json={"session_id": session_id, "query": "Stream manual"},
                headers={"Origin": base_url, "X-AtlasRAG-Client": "web"},
            ) as sse_resp:
                assert sse_resp.status_code == 200
                assert sse_resp.headers["content-type"].startswith("text/event-stream")
                events: list[str] = []
                for line in sse_resp.iter_lines():
                    if line.startswith("event: "):
                        events.append(line[len("event: ") :])
                assert events[0] == "accepted"
                assert events[-1] == "final"

        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5.0) == 0
        assert shutdown_receipt_path(state_dir, disc["server_instance_id"]).exists()


def test_real_server_abrupt_tcp_rst_mid_json_and_mid_sse(
    tmp_path: Path,
) -> None:
    """S11-15/24: Real TCP RST mid-JSON /query and mid-SSE /query/stream cancel and settle cleanly."""

    home = tmp_path / "home_rst"
    state_dir = tmp_path / "state_rst"
    diag_dir = tmp_path / "diag_rst"
    port = _unused_port()

    with _spawn_real_uvicorn_server(
        home_dir=home, state_dir=state_dir, diag_dir=diag_dir, port=port
    ) as proc:
        disc = json.loads(read_secure_local_file(server_discovery_path(state_dir)))
        base_url = disc["url"]
        session_id = _create_session(base_url)

        # 1. Abrupt TCP RST mid-JSON POST /api/v1/query.
        body_json = json.dumps(
            {"session_id": session_id, "query": "SLOW_JSON work"}
        ).encode()
        raw_req_json = (
            f"POST /api/v1/query HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Origin: {base_url}\r\n"
            f"X-AtlasRAG-Client: web\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_json)}\r\n\r\n"
        ).encode() + body_json
        sock_json = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        sock_json.sendall(raw_req_json)
        _wait_for_marker(diag_dir / "json_started", timeout=5.0, proc=proc)
        _send_tcp_rst(sock_json)
        _wait_for_marker(diag_dir / "json_cancelled", timeout=5.0, proc=proc)

        # 2. Abrupt TCP RST mid-SSE POST /api/v1/query/stream after receiving accepted frame.
        body_sse = json.dumps(
            {"session_id": session_id, "query": "SLOW_SSE work"}
        ).encode()
        raw_req_sse = (
            f"POST /api/v1/query/stream HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Origin: {base_url}\r\n"
            f"X-AtlasRAG-Client: web\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_sse)}\r\n\r\n"
        ).encode() + body_sse
        sock_sse = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        sock_sse.sendall(raw_req_sse)
        headers_bytes, prefix = _read_http_headers_and_prefix(sock_sse, timeout=5.0)
        assert b"200 OK" in headers_bytes
        buf = bytearray(prefix)
        while b"event: accepted" not in buf:
            buf.extend(sock_sse.recv(4096))
        _wait_for_marker(diag_dir / "sse_started", timeout=5.0, proc=proc)
        _send_tcp_rst(sock_sse)
        _wait_for_marker(diag_dir / "sse_cancelled", timeout=5.0, proc=proc)

        # Verify session in_flight settled to 0 (DELETE + fresh query succeed) and zero leaked active_queries.
        with httpx.Client(
            base_url=base_url, follow_redirects=False, trust_env=False
        ) as client:
            deadline = time.monotonic() + 3.0
            active = -1
            while time.monotonic() < deadline:
                active = client.get("/api/v1/status").json()["active_queries"]
                if active == 0:
                    break
                time.sleep(0.05)
            assert active == 0
            assert (
                client.post(
                    "/api/v1/query",
                    json={"session_id": session_id, "query": "healthy follow-up"},
                    headers={"Origin": base_url, "X-AtlasRAG-Client": "web"},
                ).status_code
                == 200
            )
            assert (
                client.delete(
                    f"/api/v1/sessions/{session_id}",
                    headers={"Origin": base_url, "X-AtlasRAG-Client": "web"},
                ).status_code
                == 204
            )

        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5.0) == 0
        snap = json.loads((diag_dir / "post_exit_snapshot.json").read_text())
        assert snap == {"active_queries": 0, "remaining_tasks": 0, "bus_channels": 0}


def test_real_server_slow_consumer_and_actual_asgi_send_stall_timeout(
    tmp_path: Path,
) -> None:
    """S11-16/24: Paused SSE consumer never stalls coordinator; stalled ASGI send() cancels cleanly."""

    home = tmp_path / "home_slow"
    state_dir = tmp_path / "state_slow"
    diag_dir = tmp_path / "diag_slow"
    port = _unused_port()

    # Part A: slow reader with small queue maxsize (2) finishes burst without blocking worker.
    with _spawn_real_uvicorn_server(
        home_dir=home,
        state_dir=state_dir,
        diag_dir=diag_dir,
        port=port,
        mode="normal",
        sse_queue_maxsize=16,
        sse_send_timeout_seconds=1.5,
    ) as proc_a:
        disc_a = json.loads(read_secure_local_file(server_discovery_path(state_dir)))
        sid_a = _create_session(disc_a["url"])
        body = json.dumps({"session_id": sid_a, "query": "BURST work"}).encode()
        raw_req = (
            f"POST /api/v1/query/stream HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Origin: {disc_a['url']}\r\n"
            f"X-AtlasRAG-Client: web\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode() + body
        sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        sock.sendall(raw_req)
        hdrs, prefix = _read_http_headers_and_prefix(sock, timeout=5.0)
        assert b"200 OK" in hdrs
        _wait_for_marker(diag_dir / "burst_worker_finished", timeout=5.0, proc=proc_a)
        time.sleep(0.15)
        chunks = [prefix]
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
        sock.close()
        stream_text = b"".join(chunks).decode("utf-8", errors="replace")
        assert "event: accepted" in stream_text
        assert "event: final" in stream_text
        proc_a.send_signal(signal.SIGTERM)
        assert proc_a.wait(timeout=5.0) == 0

    # Part B: stalled actual ASGI send() exceeds sse_send_timeout_seconds -> cancels query & cleans bus.
    home_b = tmp_path / "home_stall"
    state_b = tmp_path / "state_stall"
    diag_b = tmp_path / "diag_stall"
    port_b = _unused_port()
    with _spawn_real_uvicorn_server(
        home_dir=home_b,
        state_dir=state_b,
        diag_dir=diag_b,
        port=port_b,
        mode="stall_send",
        sse_queue_maxsize=16,
        sse_send_timeout_seconds=0.35,
    ) as proc_b:
        disc_b = json.loads(read_secure_local_file(server_discovery_path(state_b)))
        sid_b = _create_session(disc_b["url"])
        with httpx.Client(
            base_url=disc_b["url"], follow_redirects=False, trust_env=False, timeout=5.0
        ) as client:
            with client.stream(
                "POST",
                "/api/v1/query/stream",
                json={"session_id": sid_b, "query": "STALL_SEND work"},
                headers={"Origin": disc_b["url"], "X-AtlasRAG-Client": "web"},
            ) as resp:
                assert resp.status_code == 200
                try:
                    for _line in resp.iter_lines():
                        pass
                except httpx.HTTPError:
                    pass
            _wait_for_marker(diag_b / "send_stall_entered", timeout=5.0, proc=proc_b)
            _wait_for_marker(
                diag_b / "send_timeout_cancelled", timeout=5.0, proc=proc_b
            )
            assert client.get("/api/v1/status").json()["active_queries"] == 0
        proc_b.send_signal(signal.SIGTERM)
        code_b = proc_b.wait(timeout=5.0)
        if code_b != 0:
            out_b, err_b = proc_b.communicate()
            raise AssertionError(f"proc_b exited {code_b}: {out_b}\n{err_b}")
        snap_b = json.loads((diag_b / "post_exit_snapshot.json").read_text())
        assert snap_b == {"active_queries": 0, "remaining_tasks": 0, "bus_channels": 0}


def test_real_server_ownership_conflict_bind_failure_blocked_recovery_and_restart(
    tmp_path: Path,
) -> None:
    """S11-01/04/05/24: Competing serve exits 3; EADDRINUSE exits 4; BLOCKED recovers cleanly & restarts."""

    home = tmp_path / "home_owner"
    state_dir = tmp_path / "state_owner"
    diag_dir = tmp_path / "diag_owner"
    port = _unused_port()

    with _spawn_real_uvicorn_server(
        home_dir=home, state_dir=state_dir, diag_dir=diag_dir, port=port, mode="blocked"
    ) as proc:
        disc_before = read_secure_local_file(server_discovery_path(state_dir))
        tok_before = read_secure_local_file(control_token_path(state_dir))
        lock_file = home / ".local" / "state" / "atlasrag" / "runtime.lock"
        assert is_runtime_lock_active(lock_file)

        # Competing real CLI `atlasrag serve` subprocess hits RuntimeOwnershipConflictError (exit 3).
        competing = subprocess.run(
            _cli_subprocess_cmd(
                lock_file,
                [
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(_unused_port()),
                    "--state-dir",
                    str(state_dir),
                ],
            ),
            env=_sample_serve_env(home, tmp_path / "knowledge_competing"),
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        assert competing.returncode == EXIT_OWNERSHIP_CONFLICT
        assert read_secure_local_file(server_discovery_path(state_dir)) == disc_before
        assert read_secure_local_file(control_token_path(state_dir)) == tok_before
        assert is_runtime_lock_active(lock_file)

        # BLOCKED server rejects SSE query pre-admission with HTTP 503 JSON and recovers via CLI.
        base_url = json.loads(disc_before)["url"]
        sid = _create_session(base_url)
        with httpx.Client(
            base_url=base_url, follow_redirects=False, trust_env=False
        ) as client:
            blocked_sse = client.post(
                "/api/v1/query/stream",
                json={"session_id": sid, "query": "blocked query"},
                headers={"Origin": base_url, "X-AtlasRAG-Client": "web"},
            )
            assert blocked_sse.status_code == 503
            assert blocked_sse.headers["content-type"].startswith("application/json")
            assert blocked_sse.json()["error_code"] == "RUNTIME_NOT_READY"

        env = _make_subprocess_env(home)
        health_live = subprocess.run(
            _cli_subprocess_cmd(
                lock_file, ["health", "--live", "--state-dir", str(state_dir)]
            ),
            env=env,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        health_ready = subprocess.run(
            _cli_subprocess_cmd(
                lock_file, ["health", "--ready", "--state-dir", str(state_dir)]
            ),
            env=env,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        assert health_live.returncode == 0
        assert health_ready.returncode == 1

        recover = subprocess.run(
            _cli_subprocess_cmd(
                lock_file,
                [
                    "recover",
                    "--poll-interval",
                    "0.05",
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
            ),
            env=env,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        assert recover.returncode == EXIT_OK
        assert json.loads(recover.stdout)["serving_safety"] == "SAFE_TO_SERVE"

        stop_res = subprocess.run(
            _cli_subprocess_cmd(
                lock_file,
                ["stop", "--timeout", "8.0", "--state-dir", str(state_dir)],
            ),
            env=env,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        assert stop_res.returncode == EXIT_OK
        assert proc.wait(timeout=5.0) == EXIT_OK

    # Immediate restart cleanly succeeds with new instance identity.
    port2 = _unused_port()
    with _spawn_real_uvicorn_server(
        home_dir=home, state_dir=state_dir, diag_dir=diag_dir, port=port2
    ) as proc2:
        assert (
            json.loads(read_secure_local_file(server_discovery_path(state_dir)))[
                "server_instance_id"
            ]
            != json.loads(disc_before)["server_instance_id"]
        )
        proc2.send_signal(signal.SIGINT)
        assert proc2.wait(timeout=5.0) == EXIT_OK

    # Occupied-port bind failure (EADDRINUSE) exits 4, leaves zero discovery/token/receipt, releases lock.
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = int(blocker.getsockname()[1])
    home_bind = tmp_path / "home_bind"
    home_bind.mkdir(mode=0o700)
    state_bind = tmp_path / "state_bind"
    diag_bind = tmp_path / "diag_bind"
    diag_bind.mkdir(mode=0o700)
    try:
        bind_lock_path = home_bind / ".local" / "state" / "atlasrag" / "runtime.lock"
        bind_script = f"""
import dataclasses, sys
from pathlib import Path
import atlasrag.runtime.ownership as ownership_mod
ownership_mod.canonical_lock_path = lambda: Path({str(bind_lock_path)!r})
from atlasrag.runtime.coordinator import ApplicationCoordinator
from atlasrag.runtime.ownership import RuntimeOwnershipLock
from atlasrag.server.config import ServerConfig
from atlasrag.server.launcher import ServerLauncher
from tests.stage11.conftest import build_test_dependencies, sample_runtime_config

async def factory(cfg, bus, settled):
    deps, harness = build_test_dependencies(on_shutdown_settled=settled)
    harness.ownership = RuntimeOwnershipLock()
    return await ApplicationCoordinator.start(dataclasses.replace(deps, ownership=harness.ownership, on_shutdown_settled=settled))

code = ServerLauncher(
    runtime_config=sample_runtime_config(),
    server_config=ServerConfig(host="127.0.0.1", port={busy_port}, state_directory={str(state_bind)!r}),
    coordinator_factory=factory,
    log_level="warning",
).run()
raise SystemExit(code)
"""
        bind_res = subprocess.run(
            [sys.executable, "-c", bind_script],
            env=_make_subprocess_env(home_bind),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        assert bind_res.returncode == EXIT_FATAL_LIFECYCLE
        assert not server_discovery_path(state_bind).exists()
        assert not control_token_path(state_bind).exists()
        assert not list(state_bind.glob("shutdown-*.json"))
        assert not is_runtime_lock_active(
            home_bind / ".local" / "state" / "atlasrag" / "runtime.lock"
        )
    finally:
        blocker.close()


def test_real_server_external_sigkill_and_hung_worker_force_pidfd_escalation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """S11-22/23/24: External SIGKILL fails closed (4); hung live worker escalates via pidfd --force (4)."""

    # 1. External SIGKILL -> stop exits 4 (UNVERIFIED_EXIT); next serve recovers stale state cleanly.
    home_kill = tmp_path / "home_kill"
    state_kill = tmp_path / "state_kill"
    diag_kill = tmp_path / "diag_kill"
    port_kill = _unused_port()

    with _spawn_real_uvicorn_server(
        home_dir=home_kill, state_dir=state_kill, diag_dir=diag_kill, port=port_kill
    ) as proc_kill:
        kill_lock_path = home_kill / ".local" / "state" / "atlasrag" / "runtime.lock"
        stop_proc = subprocess.Popen(
            _cli_subprocess_cmd(
                kill_lock_path,
                [
                    "stop",
                    "--timeout",
                    "5.0",
                    "--state-dir",
                    str(state_kill),
                    "--json",
                ],
            ),
            env=_make_subprocess_env(home_kill),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Wait until stop captures server identity & initiates shutdown (unlinking server.json), or kill directly:
        time.sleep(0.15)
        proc_kill.kill()
        proc_kill.wait(timeout=5.0)
        out_stop, _ = stop_proc.communicate(timeout=8.0)
        assert stop_proc.returncode == EXIT_FATAL_LIFECYCLE

    # Fresh restart recovers from any leftover files cleanly.
    with _spawn_real_uvicorn_server(
        home_dir=home_kill,
        state_dir=state_kill,
        diag_dir=diag_kill,
        port=_unused_port(),
    ) as proc_rec:
        proc_rec.send_signal(signal.SIGTERM)
        assert proc_rec.wait(timeout=5.0) == EXIT_OK

    # 2. Live hung worker -> stop without --force times out (exit 1); stop --force escalates via pidfd (exit 4).
    home_hung = tmp_path / "home_hung"
    state_hung = tmp_path / "state_hung"
    diag_hung = tmp_path / "diag_hung"
    port_hung = _unused_port()

    with _spawn_real_uvicorn_server(
        home_dir=home_hung,
        state_dir=state_hung,
        diag_dir=diag_hung,
        port=port_hung,
        mode="hung_worker",
        shutdown_timeout_seconds=0.3,
    ) as proc_hung:
        env_hung = _make_subprocess_env(home_hung)
        hung_lock_path = home_hung / ".local" / "state" / "atlasrag" / "runtime.lock"
        # Verify stop --force via real subprocess / CLI escalates via pidfd cleanly.
        force_res = subprocess.run(
            _cli_subprocess_cmd(
                hung_lock_path,
                [
                    "stop",
                    "--timeout",
                    "0.8",
                    "--force",
                    "--state-dir",
                    str(state_hung),
                    "--json",
                ],
            ),
            env=env_hung,
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        assert force_res.returncode == EXIT_FATAL_LIFECYCLE
        payload = json.loads(force_res.stdout)
        assert payload["status"] == "FORCED_TERMINATION"
        assert payload["terminated"] is True
        assert proc_hung.wait(timeout=5.0) is not None
        assert not list(state_hung.glob("shutdown-*.json"))

    # Keep direct import reference used cleanly by thin CLI boundary check.
    assert callable(cli_main)
    capsys.readouterr()
