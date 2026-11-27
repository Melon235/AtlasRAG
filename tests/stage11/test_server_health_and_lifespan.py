"""Tests for FastAPI lifespan, health/status endpoints, and minimal CLI scaffold."""

from __future__ import annotations

import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from atlasrag.cli.main import main as cli_main
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import (
    RuntimeBootstrapFailedError,
    RuntimeOwnershipConflictError,
)
from atlasrag.runtime.coordinator import ApplicationCoordinator
from atlasrag.runtime.health import DependencyHealth
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.server.app import create_app, require_control_bearer
from atlasrag.server.config import ServerConfig
from atlasrag.services.sync.models import (
    ReconciliationOutcome,
    ReconciliationResult,
)
from stage11.conftest import (
    build_test_dependencies,
    sample_runtime_config,
    start_test_coordinator,
)


@pytest.mark.asyncio
async def test_fastapi_lifespan_starts_and_shuts_down_owned_coordinator() -> None:
    events: list[str] = []
    deps, harness = build_test_dependencies(
        on_shutdown_settled=lambda: events.append("settled")
    )

    async def factory(_cfg: RuntimeConfig) -> ApplicationCoordinator:
        events.append("started")
        return await ApplicationCoordinator.start(deps)

    app = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        coordinator_factory=factory,
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert events == ["started"]
        live = client.get("/api/v1/health/live")
        assert live.status_code == 200
        assert live.json() == {"status": "ALIVE", "mode": "SERVING"}

        ready = client.get("/api/v1/health/ready")
        assert ready.status_code == 200
        assert ready.json() == {"ready": True, "mode": "SERVING"}

        status = client.get("/api/v1/status")
        assert status.status_code == 200
        body = status.json()
        assert body["mode"] == "SERVING"
        assert body["ready"] is True
        assert body["knowledge_verified"] is True
        assert body["dependencies"]["postgres"] == "HEALTHY"
    assert events == ["started", "settled"]
    assert not harness.ownership.acquired


def test_lifespan_surfaces_bootstrap_failure_and_ownership_conflict() -> None:
    async def fail_bootstrap(_cfg: RuntimeConfig) -> ApplicationCoordinator:
        raise RuntimeBootstrapFailedError

    app_fail = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        coordinator_factory=fail_bootstrap,
    )
    with pytest.raises(RuntimeBootstrapFailedError):
        with TestClient(app_fail, base_url="http://127.0.0.1:8000"):
            pass

    async def fail_ownership(_cfg: RuntimeConfig) -> ApplicationCoordinator:
        raise RuntimeOwnershipConflictError

    app_conflict = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        coordinator_factory=fail_ownership,
    )
    with pytest.raises(RuntimeOwnershipConflictError):
        with TestClient(app_conflict, base_url="http://127.0.0.1:8000"):
            pass


@pytest.mark.asyncio
async def test_blocked_startup_and_soft_degradation_and_refresh_cooldown() -> None:
    async def blocked_reconcile() -> ReconciliationResult:
        return ReconciliationResult(
            outcome=ReconciliationOutcome.BLOCKED,
            blocked_reasons=("SYNC_FAILED",),
        )

    blocked_coord, _ = await start_test_coordinator(reconcile_fn=blocked_reconcile)
    try:
        app = create_app(
            runtime_config=sample_runtime_config(),
            server_config=ServerConfig(),
            coordinator=blocked_coord,
        )
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            live = client.get("/api/v1/health/live")
            assert live.status_code == 200
            assert live.json() == {"status": "ALIVE", "mode": "BLOCKED"}

            ready = client.get("/api/v1/health/ready")
            assert ready.status_code == 503
            assert ready.json() == {"ready": False, "mode": "BLOCKED"}

            status = client.get("/api/v1/status")
            assert status.status_code == 200
            assert status.json()["mode"] == "BLOCKED"
            assert status.json()["ready"] is False
            assert status.json()["knowledge_verified"] is False
    finally:
        await blocked_coord.shutdown()

    probe_calls = {"postgres": 0, "redis": 0}

    async def count_pg() -> None:
        probe_calls["postgres"] += 1

    async def failing_soft() -> None:
        probe_calls["redis"] += 1
        raise ConnectionError("soft dep unavailable")

    degraded_coord, _ = await start_test_coordinator(
        probes={
            "postgres": count_pg,
            "redis": failing_soft,
            "searxng": failing_soft,
            "reranker": failing_soft,
        },
        query_cache_status=lambda redis_health: (
            "BYPASSED" if redis_health is not DependencyHealth.HEALTHY else "TRUSTED"
        ),
        refresh_interval=60.0,
        manual_cooldown=1.0,
    )
    try:
        app2 = create_app(
            runtime_config=sample_runtime_config(),
            server_config=ServerConfig(),
            coordinator=degraded_coord,
        )
        with TestClient(app2, base_url="http://127.0.0.1:8000") as client:
            calls_after_start = probe_calls["postgres"]
            r1 = client.get("/api/v1/status")
            assert r1.status_code == 200
            body = r1.json()
            assert body["ready"] is True
            assert body["dependencies"]["redis"] == "UNAVAILABLE"
            assert body["dependencies"]["searxng"] == "UNAVAILABLE"
            assert body["dependencies"]["reranker"] == "UNAVAILABLE"
            assert body["query_cache"] == "BYPASSED"
            # Cached status must not trigger extra probes
            assert probe_calls["postgres"] == calls_after_start

            # Immediate ?refresh=true is cooldown-bounded
            client.get("/api/v1/status?refresh=true")
            assert probe_calls["postgres"] == calls_after_start

            # Unknown WP-B/C endpoints are not exposed in WP-A
            assert client.post("/api/v1/sessions", json={}).status_code == 404
            assert client.post("/api/v1/query", json={}).status_code == 404
    finally:
        await degraded_coord.shutdown()


@pytest.mark.asyncio
async def test_manual_refresh_and_status_lifecycle_generation_consistency() -> None:
    pg_calls = 0

    async def count_pg() -> None:
        nonlocal pg_calls
        pg_calls += 1

    coordinator, harness = await start_test_coordinator(
        probes={"postgres": count_pg},
        refresh_interval=60.0,
        manual_cooldown=0.0,
    )
    try:
        assert harness.gate.mode is RuntimeMode.SERVING
        snap1 = await coordinator.status()
        calls_before_refresh = pg_calls
        snap_refreshed = await coordinator.status(refresh=True)
        assert pg_calls == calls_before_refresh + 1
        assert snap_refreshed.generation > snap1.generation
        assert not hasattr(snap_refreshed, "deepseek")
        # Advance lifecycle generation via gate maintenance transition
        lease = await harness.gate.begin_maintenance()
        await harness.gate.enter_exclusive_maintenance(lease)
        await harness.gate.complete_maintenance(lease, safe=True)
        await harness.gate.set_admission_ready(
            True, lifecycle_generation=harness.gate.lifecycle_generation
        )
        snap2 = await coordinator.status()
        assert snap2.lifecycle_generation == harness.gate.lifecycle_generation
        assert snap2.lifecycle_generation > snap_refreshed.lifecycle_generation
    finally:
        await coordinator.shutdown()


def test_cli_main_scaffold_is_import_safe_and_lightweight(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main([]) == 2
    out = capsys.readouterr()
    assert "atlasrag" in out.out.casefold()
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["serve"])
    assert exc_info.value.code == 2

    assert require_control_bearer is not None
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, atlasrag.cli.main; "
                "assert 'fastapi' not in sys.modules; "
                "assert 'uvicorn' not in sys.modules; "
                "assert 'atlasrag.runtime.composition' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
