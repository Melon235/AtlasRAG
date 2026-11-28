"""Layer C isolated real-infrastructure Stage 11 application server E2E tests."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis

import atlasrag.runtime.ownership as ownership_module
from atlasrag.cli.main import EXIT_OK
from atlasrag.cli.main import main as cli_main
from atlasrag.repositories.postgres.pool import PostgresPool
from atlasrag.repositories.postgres.uow import PostgresUnitOfWork
from atlasrag.server.auth import (
    control_token_path,
    is_runtime_lock_active,
    read_secure_local_file,
    server_discovery_path,
)
from integration.stage10_helpers import (
    TcpFaultProxy,
    parse_stage10_endpoints,
    stage10_isolation_guard,
    verify_stage10_service_ownership,
)

pytestmark = pytest.mark.integration
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _require_stage10_ownership() -> None:
    verify_stage10_service_ownership()


GUIDE_V1 = (
    "# AtlasRAG Runtime Guide\n\n"
    "## Query Caching\n\n"
    "Version one documents zephyrite-token-alpha verification in local evidence.\n"
)

GUIDE_V2 = (
    "# AtlasRAG Runtime Guide\n\n"
    "## Query Caching\n\n"
    "Version two introduces solstice-marker-v2 updated local evidence.\n"
)

KEEP_DOC = (
    "# Secondary Reference\n\n"
    "Remaining corpus manual stays available after blocked recovery.\n"
)


def _unused_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _spawn_e2e_server(
    *,
    postgres_dsn: str,
    knowledge_dir: Path,
    redis_host: str,
    redis_port: int,
    milvus_host: str,
    milvus_port: int,
    state_dir: Path,
    lock_file: Path,
    diag_dir: Path,
    port: int,
    answer_file: Path,
    fail_delete_flag: Path | None = None,
    runtime_overrides: dict[str, Any] | None = None,
) -> Iterator[subprocess.Popen[str]]:
    lock_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(lock_file.parent, 0o700)
    diag_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    runner = f"""
import json, os, sys
from pathlib import Path
from unittest.mock import patch
import httpx
from atlasrag.domain.errors import DependencyUnavailableError
from atlasrag.providers.index.milvus import MilvusIndexProvider
from atlasrag.runtime import composition as composition_module
from atlasrag.runtime import ownership as ownership_module
from atlasrag.server.config import ServerConfig
from atlasrag.server.launcher import ServerLauncher
from integration.stage10_helpers import (
    FakeBgeReranker,
    FakeQwenEmbedding,
    HttpMockState,
    build_stage10_runtime_config,
)

ownership_module.canonical_lock_path = lambda: Path({str(lock_file)!r})
answer_file = Path({str(answer_file)!r})
diag_dir = Path({str(diag_dir)!r})
fail_flag = {str(fail_delete_flag)!r}

class _DynamicHttpState(HttpMockState):
    pass

state = _DynamicHttpState(answer_text=answer_file.read_text(encoding="utf-8").strip())
orig_async_client = httpx.AsyncClient

def _make_dynamic_http_factory(mock_state):
    def _handler(request: httpx.Request) -> httpx.Response:
        mock_state.answer_text = answer_file.read_text(encoding="utf-8").strip()
        url_text = str(request.url)
        if "deepseek" in url_text:
            body = json.loads(request.content.decode("utf-8"))
            user_content = body["messages"][1]["content"]
            mock_state.prompts.append(user_content)
            if "UNTRUSTED_EVIDENCE_DATA_JSON" in user_content:
                mock_state.deepseek_generation_calls += 1
                (diag_dir / "gen_calls").write_text(str(mock_state.deepseek_generation_calls))
                payload = json.dumps({{"answer_text": mock_state.answer_text, "used_citation_ids": ["L1"]}})
            else:
                mock_state.deepseek_rewrite_calls += 1
                original_query = json.loads(user_content).get("query", "guide")
                payload = json.dumps({{"retrieval_query": mock_state.rewrite_query or original_query}})
            return httpx.Response(200, json={{"choices": [{{"message": {{"content": payload}}}}]}})
        if "/search" in url_text:
            mock_state.searxng_search_calls += 1
            return httpx.Response(mock_state.searxng_status, json={{"results": []}})
        mock_state.searxng_probe_calls += 1
        return httpx.Response(mock_state.searxng_status, text="ok")

    def _factory(**kwargs):
        timeout = kwargs.get("timeout")
        return orig_async_client(
            transport=httpx.MockTransport(_handler),
            timeout=timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(10.0),
        )
    return _factory

orig_delete = MilvusIndexProvider.delete_document
async def _guarded_delete(self, doc_id: str) -> int:
    if fail_flag and Path(fail_flag).exists():
        raise DependencyUnavailableError
    return await orig_delete(self, doc_id)

composition_module.QwenEmbeddingProvider = lambda **_: FakeQwenEmbedding()
composition_module.BgeRerankerProvider = lambda **_: FakeBgeReranker()
httpx.AsyncClient = _make_dynamic_http_factory(state)
MilvusIndexProvider.delete_document = _guarded_delete

cfg = build_stage10_runtime_config(
    postgres_dsn={postgres_dsn!r},
    knowledge_directory=Path({str(knowledge_dir)!r}),
    redis_host={redis_host!r},
    redis_port={redis_port!r},
    milvus_host={milvus_host!r},
    milvus_port={milvus_port!r},
    runtime_overrides={runtime_overrides!r},
)
server_cfg = ServerConfig(
    host="127.0.0.1",
    port={port!r},
    state_directory={str(state_dir)!r},
    min_manual_refresh_interval_seconds=0.0,
)
code = ServerLauncher(runtime_config=cfg, server_config=server_cfg, log_level="warning").run()
raise SystemExit(code)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{REPO_ROOT / 'src'}:{REPO_ROOT / 'tests'}:{REPO_ROOT}"
    proc = subprocess.Popen(
        [sys.executable, "-c", runner],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    discovery = server_discovery_path(state_dir)
    deadline = time.monotonic() + 25.0
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise AssertionError(
                    f"E2E server exited prematurely ({proc.returncode}): {out}\n{err}"
                )
            if discovery.exists():
                try:
                    raw = json.loads(read_secure_local_file(discovery))
                    if raw.get("pid") == proc.pid:
                        break
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
            time.sleep(0.05)
        assert discovery.exists(), "Timed out waiting for Stage 11 E2E server.json"
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)
        if lock_file.exists():
            assert is_runtime_lock_active(lock_file) is False


def _stop_e2e_cli(
    state_dir: Path, lock_file: Path, proc: subprocess.Popen[str]
) -> None:
    orig_lock_fn = ownership_module.canonical_lock_path
    ownership_module.canonical_lock_path = lambda: lock_file
    try:
        rc = cli_main(
            ["stop", "--timeout", "15.0", "--state-dir", str(state_dir), "--json"],
            environ={},
        )
    finally:
        ownership_module.canonical_lock_path = orig_lock_fn
    assert rc == EXIT_OK
    assert proc.wait(timeout=5.0) == EXIT_OK
    assert not server_discovery_path(state_dir).exists()
    assert not control_token_path(state_dir).exists()


def _run_cli_with_lock(lock_file: Path, argv: list[str]) -> int:
    orig_lock_fn = ownership_module.canonical_lock_path
    ownership_module.canonical_lock_path = lambda: lock_file
    try:
        return cli_main(argv, environ={})
    finally:
        ownership_module.canonical_lock_path = orig_lock_fn


@pytest.mark.asyncio
async def test_s11_e2e_serving_redis_cache_session_isolation_sse_sync_and_cli(
    postgres_dsn: str,
    milvus_uri: str,
    tmp_path: Path,
) -> None:
    """S11-07..10/12/14/18/19..22/25: Real PG/Milvus/Redis serving, Query Cache, SSE hit, /control/sync."""

    _, (redis_host, redis_port), (milvus_host, milvus_port) = parse_stage10_endpoints()
    knowledge_dir = tmp_path / "kb"
    knowledge_dir.mkdir(mode=0o700)
    guide_path = knowledge_dir / "guide.md"
    guide_path.write_text(GUIDE_V1, encoding="utf-8")
    state_dir = tmp_path / "state"
    lock_file = tmp_path / "lock" / "runtime.lock"
    diag_dir = tmp_path / "diag"
    answer_file = tmp_path / "answer.txt"
    answer_file.write_text("Answer from v1 [L1].", encoding="utf-8")

    async with stage10_isolation_guard(
        redis_host=redis_host, redis_port=redis_port, milvus_uri=milvus_uri
    ):
        with _spawn_e2e_server(
            postgres_dsn=postgres_dsn,
            knowledge_dir=knowledge_dir,
            redis_host=redis_host,
            redis_port=redis_port,
            milvus_host=milvus_host,
            milvus_port=milvus_port,
            state_dir=state_dir,
            lock_file=lock_file,
            diag_dir=diag_dir,
            port=_unused_port(),
            answer_file=answer_file,
        ) as proc:
            base_url = json.loads(
                read_secure_local_file(server_discovery_path(state_dir))
            )["url"]
            token = read_secure_local_file(control_token_path(state_dir))

            def _exercise_http_and_cli() -> None:
                # 0. Ingest initial GUIDE_V1 into real PG + Milvus via operational CLI sync.
                assert (
                    _run_cli_with_lock(
                        lock_file,
                        [
                            "sync",
                            "--poll-interval",
                            "0.05",
                            "--state-dir",
                            str(state_dir),
                        ],
                    )
                    == 0
                )

                with httpx.Client(
                    base_url=base_url,
                    follow_redirects=False,
                    trust_env=False,
                    timeout=15.0,
                ) as client:
                    # 1. Health, readiness, status, and security guards.
                    assert client.get("/api/v1/health/live").json()["mode"] == "SERVING"
                    assert client.get("/api/v1/health/ready").json()["ready"] is True
                    status_body = client.get("/api/v1/status").json()
                    assert status_body["query_cache"] == "ACTIVE"
                    assert status_body["dependencies"]["postgres"] == "HEALTHY"
                    assert status_body["dependencies"]["milvus"] == "HEALTHY"
                    assert status_body["dependencies"]["redis"] == "HEALTHY"
                    assert (
                        client.post(
                            "/api/v1/control/sync",
                            json={},
                            headers={"Origin": base_url},
                        ).status_code
                        == 401
                    )
                    assert (
                        client.post(
                            "/api/v1/sessions",
                            json={"client_kind": "web"},
                            headers={
                                "Host": "untrusted.local",
                                "Origin": base_url,
                                "X-AtlasRAG-Client": "web",
                            },
                        ).status_code
                        == 403
                    )

                    # 2. Mandatory server-issued sessions & same-session Redis Query Cache MISS -> HIT.
                    s1 = client.post(
                        "/api/v1/sessions",
                        json={"client_kind": "web"},
                        headers={"Origin": base_url, "X-AtlasRAG-Client": "web"},
                    ).json()["session_id"]
                    s2 = client.post(
                        "/api/v1/sessions",
                        json={"client_kind": "web"},
                        headers={"Origin": base_url, "X-AtlasRAG-Client": "web"},
                    ).json()["session_id"]

                    q1 = client.post(
                        "/api/v1/query",
                        json={"session_id": s1, "query": "Explain zephyrite"},
                        headers={"Origin": base_url, "X-AtlasRAG-Client": "web"},
                    )
                    assert q1.status_code == 200
                    d1 = q1.json()
                    assert d1["cache_status"] == "MISS"
                    assert set(d1["response"].keys()) == {"answer_text", "citations"}
                    assert any(
                        c["citation_id"] == "L1" for c in d1["response"]["citations"]
                    )
                    gen_after_miss = int((diag_dir / "gen_calls").read_text())

                    q2 = client.post(
                        "/api/v1/query",
                        json={"session_id": s1, "query": "Explain zephyrite"},
                        headers={"Origin": base_url, "X-AtlasRAG-Client": "web"},
                    )
                    assert q2.status_code == 200
                    d2 = q2.json()
                    assert d2["cache_status"] == "HIT"
                    assert d2["trace_id"] != d1["trace_id"]
                    assert int((diag_dir / "gen_calls").read_text()) == gen_after_miss

                    # 3. SSE cache hit emits accepted -> final without fabricating DENSE/BM25/RERANKER/ANSWER stages.
                    sse_events: list[str] = []
                    sse_stages: list[str] = []
                    final_sse: dict[str, Any] = {}
                    with client.stream(
                        "POST",
                        "/api/v1/query/stream",
                        json={"session_id": s1, "query": "Explain zephyrite"},
                        headers={"Origin": base_url, "X-AtlasRAG-Client": "web"},
                    ) as stream_resp:
                        assert stream_resp.status_code == 200
                        cur_event = ""
                        for line in stream_resp.iter_lines():
                            if line.startswith("event: "):
                                cur_event = line[len("event: ") :]
                                sse_events.append(cur_event)
                            elif line.startswith("data: "):
                                parsed = json.loads(line[len("data: ") :])
                                if cur_event == "progress":
                                    sse_stages.append(parsed["stage"])
                                elif cur_event == "final":
                                    final_sse = parsed
                    assert sse_events[0] == "accepted"
                    assert sse_events[-1] == "final"
                    assert not any(
                        st in {"DENSE", "BM25", "RERANKER", "ANSWER"}
                        for st in sse_stages
                    )
                    assert final_sse["cache_status"] == "HIT"
                    assert final_sse["trace_id"] != d2["trace_id"]

                    # 4. Cross-session isolation: s2 must MISS on identical raw query.
                    q_other = client.post(
                        "/api/v1/query",
                        json={"session_id": s2, "query": "Explain zephyrite"},
                        headers={"Origin": base_url, "X-AtlasRAG-Client": "web"},
                    )
                    assert q_other.status_code == 200
                    assert q_other.json()["cache_status"] == "MISS"

                    # 5. CLI ask reuses cli_session.json: MISS then HIT.
                    assert (
                        _run_cli_with_lock(
                            lock_file,
                            [
                                "ask",
                                "Explain CLI caching",
                                "--state-dir",
                                str(state_dir),
                            ],
                        )
                        == 0
                    )
                    assert (
                        _run_cli_with_lock(
                            lock_file,
                            [
                                "ask",
                                "Explain CLI caching",
                                "--state-dir",
                                str(state_dir),
                            ],
                        )
                        == 0
                    )
                    gen_after_cli = int((diag_dir / "gen_calls").read_text())

                    # 6. Knowledge mutation -> /control/sync with immediate client disconnect survival.
                    guide_path.write_text(GUIDE_V2, encoding="utf-8")
                    answer_file.write_text(
                        "Updated v2 solstice-marker-v2 answer [L1].",
                        encoding="utf-8",
                    )
                    sync_accepted = client.post(
                        "/api/v1/control/sync",
                        json={},
                        headers={
                            "Origin": base_url,
                            "Authorization": f"Bearer {token}",
                        },
                    )
                    assert sync_accepted.status_code == 202
                    op_id = sync_accepted.json()["operation_id"]

                # Previous client closed immediately; fresh client polls until terminal.
                with httpx.Client(
                    base_url=base_url,
                    follow_redirects=False,
                    trust_env=False,
                    timeout=15.0,
                ) as poll_client:
                    terminal: dict[str, Any] = {}
                    deadline = time.monotonic() + 90.0
                    while time.monotonic() < deadline:
                        op_resp = poll_client.get(
                            f"/api/v1/control/operations/{op_id}",
                            headers={
                                "Origin": base_url,
                                "Authorization": f"Bearer {token}",
                            },
                        )
                        assert op_resp.status_code == 200
                        body = op_resp.json()
                        if body["status"] in {"SUCCEEDED", "FAILED"}:
                            terminal = body
                            break
                        time.sleep(0.05)
                    assert terminal["status"] == "SUCCEEDED"
                    assert terminal["sync_outcome"] == "SUCCESS"
                    assert terminal["serving_safety"] == "SAFE_TO_SERVE"

                    # Pre-sync cached entry for s1 must MISS after CacheFreshnessBarrier invalidation.
                    after_sync = poll_client.post(
                        "/api/v1/query",
                        json={"session_id": s1, "query": "Explain zephyrite"},
                        headers={"Origin": base_url, "X-AtlasRAG-Client": "web"},
                    )
                    assert after_sync.status_code == 200
                    after_data = after_sync.json()
                    assert after_data["cache_status"] == "MISS"
                    assert "solstice-marker-v2" in after_data["response"]["answer_text"]
                    assert int((diag_dir / "gen_calls").read_text()) > gen_after_cli

            await asyncio.to_thread(_exercise_http_and_cli)

            # Verify PG runtime_metadata invalidation marker cleared after Redis invalidation.
            pool = PostgresPool(postgres_dsn, min_size=1, max_size=4)
            await pool.open()
            try:
                async with PostgresUnitOfWork(pool) as unit:
                    meta = await unit.runtime_metadata.get()
                    assert meta.query_cache_invalidation_required is False
            finally:
                await pool.close()

            await asyncio.to_thread(_stop_e2e_cli, state_dir, lock_file, proc)


@pytest.mark.asyncio
async def test_s11_e2e_redis_outage_bypass_recovery_and_blocked_control_recover(
    postgres_dsn: str,
    milvus_uri: str,
    tmp_path: Path,
) -> None:
    """S11-05/11/17/25: Real Redis outage bypass & stale-free recovery; BLOCKED -> /control/recover."""

    _, (redis_host, redis_port), (milvus_host, milvus_port) = parse_stage10_endpoints()
    knowledge_dir = tmp_path / "kb2"
    knowledge_dir.mkdir(mode=0o700)
    removable_path = knowledge_dir / "removable.md"
    removable_path.write_text(GUIDE_V1, encoding="utf-8")
    (knowledge_dir / "keep.md").write_text(KEEP_DOC, encoding="utf-8")
    state_dir = tmp_path / "state2"
    lock_file = tmp_path / "lock2" / "runtime.lock"
    diag_dir = tmp_path / "diag2"
    answer_file = tmp_path / "answer2.txt"
    answer_file.write_text("Outage v1 answer [L1].", encoding="utf-8")
    fail_flag = tmp_path / "fail_delete.flag"

    redis_proxy = TcpFaultProxy(redis_host, redis_port)
    await redis_proxy.start()
    try:
        async with stage10_isolation_guard(
            redis_host=redis_host, redis_port=redis_port, milvus_uri=milvus_uri
        ):
            # Phase A: Redis outage BYPASSED + knowledge mutation + Redis recovery + BLOCKED trigger.
            with _spawn_e2e_server(
                postgres_dsn=postgres_dsn,
                knowledge_dir=knowledge_dir,
                redis_host="127.0.0.1",
                redis_port=redis_proxy.port,
                milvus_host=milvus_host,
                milvus_port=milvus_port,
                state_dir=state_dir,
                lock_file=lock_file,
                diag_dir=diag_dir,
                port=_unused_port(),
                answer_file=answer_file,
                fail_delete_flag=fail_flag,
                runtime_overrides={
                    "health_refresh_interval_seconds": 0.2,
                    "cache_recovery_interval_seconds": 0.1,
                    "redis_connect_timeout_seconds": 0.4,
                    "redis_socket_timeout_seconds": 0.4,
                },
            ) as proc1:
                url1 = json.loads(
                    read_secure_local_file(server_discovery_path(state_dir))
                )["url"]
                tok1 = read_secure_local_file(control_token_path(state_dir))

                def _setup_session_and_cache() -> str:
                    assert (
                        _run_cli_with_lock(
                            lock_file,
                            [
                                "sync",
                                "--poll-interval",
                                "0.05",
                                "--state-dir",
                                str(state_dir),
                            ],
                        )
                        == 0
                    )
                    with httpx.Client(
                        base_url=url1,
                        follow_redirects=False,
                        trust_env=False,
                        timeout=15.0,
                    ) as c:
                        sid = c.post(
                            "/api/v1/sessions",
                            json={"client_kind": "web"},
                            headers={"Origin": url1, "X-AtlasRAG-Client": "web"},
                        ).json()["session_id"]
                        assert (
                            c.post(
                                "/api/v1/query",
                                json={"session_id": sid, "query": "Check outage cache"},
                                headers={"Origin": url1, "X-AtlasRAG-Client": "web"},
                            ).json()["cache_status"]
                            == "MISS"
                        )
                        assert (
                            c.post(
                                "/api/v1/query",
                                json={"session_id": sid, "query": "Check outage cache"},
                                headers={"Origin": url1, "X-AtlasRAG-Client": "web"},
                            ).json()["cache_status"]
                            == "HIT"
                        )
                        return str(sid)

                sid1 = await asyncio.to_thread(_setup_session_and_cache)

                redis_proxy.mode = "refuse"
                await redis_proxy.drop_connections()

                def _outage_checks_and_mutate() -> None:
                    with httpx.Client(
                        base_url=url1,
                        follow_redirects=False,
                        trust_env=False,
                        timeout=15.0,
                    ) as c:
                        assert c.get("/api/v1/health/ready").status_code == 200
                        st = c.get("/api/v1/status", params={"refresh": "true"}).json()
                        assert st["dependencies"]["redis"] == "UNAVAILABLE"
                        assert st["query_cache"] == "BYPASSED"
                        q_outage = c.post(
                            "/api/v1/query",
                            json={"session_id": sid1, "query": "Check outage cache"},
                            headers={"Origin": url1, "X-AtlasRAG-Client": "web"},
                        )
                        assert q_outage.status_code == 200
                        assert q_outage.json()["cache_status"] == "BYPASSED"

                        removable_path.write_text(GUIDE_V2, encoding="utf-8")
                        answer_file.write_text(
                            "Outage-recovered v2 [L1].", encoding="utf-8"
                        )
                        assert (
                            _run_cli_with_lock(
                                lock_file,
                                [
                                    "sync",
                                    "--poll-interval",
                                    "0.05",
                                    "--state-dir",
                                    str(state_dir),
                                ],
                            )
                            == 0
                        )

                await asyncio.to_thread(_outage_checks_and_mutate)

                redis_proxy.mode = "passthrough"

                def _recovered_checks_and_trigger_blocked() -> None:
                    with httpx.Client(
                        base_url=url1,
                        follow_redirects=False,
                        trust_env=False,
                        timeout=15.0,
                    ) as c:
                        deadline = time.monotonic() + 10.0
                        while time.monotonic() < deadline:
                            st = c.get(
                                "/api/v1/status", params={"refresh": "true"}
                            ).json()
                            if st["dependencies"]["redis"] == "HEALTHY":
                                break
                            time.sleep(0.1)
                        assert st["dependencies"]["redis"] == "HEALTHY"
                        time.sleep(0.15)
                        q_post = c.post(
                            "/api/v1/query",
                            json={"session_id": sid1, "query": "Check outage cache"},
                            headers={"Origin": url1, "X-AtlasRAG-Client": "web"},
                        ).json()
                        assert q_post["cache_status"] == "MISS"
                        assert (
                            "Outage-recovered v2" in q_post["response"]["answer_text"]
                        )
                        st_after = c.get(
                            "/api/v1/status", params={"refresh": "true"}
                        ).json()
                        assert st_after["query_cache"] == "ACTIVE"
                        assert (
                            c.post(
                                "/api/v1/query",
                                json={
                                    "session_id": sid1,
                                    "query": "Check outage cache",
                                },
                                headers={"Origin": url1, "X-AtlasRAG-Client": "web"},
                            ).json()["cache_status"]
                            == "HIT"
                        )

                    # Induce UNSAFE_TO_SERVE on deletion failure -> enters BLOCKED.
                    removable_path.unlink()
                    fail_flag.write_text("1", encoding="utf-8")
                    assert (
                        _run_cli_with_lock(
                            lock_file,
                            [
                                "sync",
                                "--poll-interval",
                                "0.05",
                                "--state-dir",
                                str(state_dir),
                            ],
                        )
                        == 1
                    )
                    with httpx.Client(
                        base_url=url1,
                        follow_redirects=False,
                        trust_env=False,
                        timeout=15.0,
                    ) as c:
                        assert c.get("/api/v1/health/live").json()["mode"] == "BLOCKED"
                        assert c.get("/api/v1/health/ready").status_code == 503
                        assert (
                            c.post(
                                "/api/v1/control/sync",
                                json={},
                                headers={
                                    "Origin": url1,
                                    "Authorization": f"Bearer {tok1}",
                                },
                            ).status_code
                            == 503
                        )

                await asyncio.to_thread(_recovered_checks_and_trigger_blocked)
                fail_flag.unlink(missing_ok=True)
                await asyncio.to_thread(_stop_e2e_cli, state_dir, lock_file, proc1)

            # Phase B: Restart against real PG/Milvus -> boots directly BLOCKED, then recovers via CLI!
            with _spawn_e2e_server(
                postgres_dsn=postgres_dsn,
                knowledge_dir=knowledge_dir,
                redis_host=redis_host,
                redis_port=redis_port,
                milvus_host=milvus_host,
                milvus_port=milvus_port,
                state_dir=state_dir,
                lock_file=lock_file,
                diag_dir=diag_dir,
                port=_unused_port(),
                answer_file=answer_file,
            ) as proc2:

                def _recover_blocked_server() -> None:
                    url2 = json.loads(
                        read_secure_local_file(server_discovery_path(state_dir))
                    )["url"]
                    with httpx.Client(
                        base_url=url2,
                        follow_redirects=False,
                        trust_env=False,
                        timeout=15.0,
                    ) as c:
                        assert c.get("/api/v1/health/live").json()["mode"] == "BLOCKED"
                        assert c.get("/api/v1/health/ready").status_code == 503
                        sid2 = c.post(
                            "/api/v1/sessions",
                            json={"client_kind": "web"},
                            headers={"Origin": url2, "X-AtlasRAG-Client": "web"},
                        ).json()["session_id"]
                        blocked_stream = c.post(
                            "/api/v1/query/stream",
                            json={"session_id": sid2, "query": "Blocked query"},
                            headers={"Origin": url2, "X-AtlasRAG-Client": "web"},
                        )
                        assert blocked_stream.status_code == 503
                        assert blocked_stream.headers["content-type"].startswith(
                            "application/json"
                        )

                    assert (
                        _run_cli_with_lock(
                            lock_file,
                            [
                                "recover",
                                "--poll-interval",
                                "0.05",
                                "--state-dir",
                                str(state_dir),
                            ],
                        )
                        == 0
                    )
                    with httpx.Client(
                        base_url=url2,
                        follow_redirects=False,
                        trust_env=False,
                        timeout=15.0,
                    ) as c:
                        assert c.get("/api/v1/health/ready").status_code == 200
                        q_ok = c.post(
                            "/api/v1/query",
                            json={"session_id": sid2, "query": "Secondary manual"},
                            headers={"Origin": url2, "X-AtlasRAG-Client": "web"},
                        )
                        assert q_ok.status_code == 200

                await asyncio.to_thread(_recover_blocked_server)
                await asyncio.to_thread(_stop_e2e_cli, state_dir, lock_file, proc2)

            r = Redis.from_url(
                f"redis://{redis_host}:{redis_port}/0", decode_responses=False
            )
            try:
                assert await r.ping() is True
            finally:
                await r.aclose()
    finally:
        await redis_proxy.close()
