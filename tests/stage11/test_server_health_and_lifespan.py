"""Tests for FastAPI lifespan, health/status endpoints, and minimal CLI scaffold."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from atlasrag.cli.main import main as cli_main
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import (
    RuntimeBootstrapFailedError,
    RuntimeOwnershipConflictError,
)
from atlasrag.runtime.cache_freshness import CacheFreshnessState
from atlasrag.runtime.coordinator import ApplicationCoordinator
from atlasrag.runtime.health import DependencyHealth, HealthRegistry, HealthSnapshot
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.server.app import create_app, require_control_bearer
from atlasrag.server.config import ServerConfig
from atlasrag.server.service import ApplicationServerService
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


@pytest.mark.asyncio
async def test_live_freshness_projection_tracks_bypassed_and_trusted_without_reprobe() -> (
    None
):
    state = {"freshness": CacheFreshnessState.TRUSTED, "enabled": True}
    probe_calls = 0

    async def count_pg() -> None:
        nonlocal probe_calls
        probe_calls += 1

    def resolve_cache(redis_health: DependencyHealth) -> str:
        if not state["enabled"]:
            return "DISABLED"
        if (
            state["freshness"] is CacheFreshnessState.TRUSTED
            and redis_health is DependencyHealth.HEALTHY
        ):
            return "ACTIVE"
        return "BYPASSED"

    coordinator, _ = await start_test_coordinator(
        probes={"postgres": count_pg},
        query_cache_status=resolve_cache,
        refresh_interval=60.0,
    )
    try:
        app = create_app(
            runtime_config=sample_runtime_config(),
            server_config=ServerConfig(),
            coordinator=coordinator,
        )
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            calls_after_start = probe_calls
            r1 = client.get("/api/v1/status")
            assert r1.status_code == 200
            assert r1.json()["query_cache"] == "ACTIVE"
            assert probe_calls == calls_after_start

            # Freshness flips to BYPASSED without snapshot TTL expiry
            state["freshness"] = CacheFreshnessState.BYPASSED
            r2 = client.get("/api/v1/status")
            assert r2.status_code == 200
            assert r2.json()["query_cache"] == "BYPASSED"
            assert probe_calls == calls_after_start

            # Recovery back to TRUSTED with healthy Redis projects ACTIVE again
            state["freshness"] = CacheFreshnessState.TRUSTED
            r3 = client.get("/api/v1/status")
            assert r3.status_code == 200
            assert r3.json()["query_cache"] == "ACTIVE"
            assert probe_calls == calls_after_start

            # Disabled policy never reports ACTIVE
            state["enabled"] = False
            r4 = client.get("/api/v1/status")
            assert r4.status_code == 200
            assert r4.json()["query_cache"] == "DISABLED"

            # Recovering freshness to TRUSTED while snapshot Redis is UNAVAILABLE still
            # must NOT publish ACTIVE
            state["enabled"] = True
            deps_health = coordinator._deps.health  # noqa: SLF001
            assert deps_health.snapshot is not None
            deps_health._snapshot = replace(  # noqa: SLF001
                deps_health.snapshot,
                redis=DependencyHealth.UNAVAILABLE,
                query_cache="ACTIVE",
            )
            r5 = client.get("/api/v1/status")
            assert r5.status_code == 200
            assert r5.json()["query_cache"] == "BYPASSED"
            assert probe_calls == calls_after_start
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_singleflight_default_refresh_and_server_config_cooldown_wiring() -> None:
    probe_started = asyncio.Event()
    probe_release = asyncio.Event()
    wait_on_probe = False
    probe_calls = 0

    async def gated_pg() -> None:
        nonlocal probe_calls
        probe_calls += 1
        if wait_on_probe:
            probe_started.set()
            await probe_release.wait()

    coordinator, _ = await start_test_coordinator(
        probes={"postgres": gated_pg},
        refresh_interval=0.05,
        manual_cooldown=60.0,
    )
    try:
        calls_after_start = probe_calls
        # Expire snapshot once by advancing beyond refresh_interval? Use 0.06s deterministic
        # gate or replace snapshot monotonic_at so zero timing flakiness exists.
        deps_health: HealthRegistry = coordinator._deps.health  # noqa: SLF001
        assert deps_health.snapshot is not None
        deps_health._snapshot = replace(  # noqa: SLF001
            deps_health.snapshot, monotonic_at=0.0
        )

        wait_on_probe = True
        tasks = [
            asyncio.create_task(coordinator.status()),
            asyncio.create_task(coordinator.status()),
            asyncio.create_task(coordinator.status()),
            asyncio.create_task(coordinator.status()),
        ]
        await probe_started.wait()
        await asyncio.sleep(0)
        assert probe_calls == calls_after_start + 1
        probe_release.set()
        results = await asyncio.gather(*tasks)
        assert probe_calls == calls_after_start + 1
        generations = {snap.generation for snap in results}
        assert len(generations) == 1
        assert results[0].generation > 1

        # Concurrent manual refreshes respect cooldown
        wait_on_probe = False
        probe_calls_before_manual = probe_calls
        service_long = ApplicationServerService(
            coordinator=coordinator,
            runtime_config=sample_runtime_config(),
            server_config=ServerConfig(min_manual_refresh_interval_seconds=60.0),
        )
        m1 = await service_long.status(refresh=True)
        assert probe_calls == probe_calls_before_manual
        assert m1.ready is True

        # Non-default ServerConfig cooldown=0.0 overrides coordinator default 60.0
        service_zero = ApplicationServerService(
            coordinator=coordinator,
            runtime_config=sample_runtime_config(),
            server_config=ServerConfig(min_manual_refresh_interval_seconds=0.0),
        )
        await service_zero.status(refresh=True)
        assert probe_calls == probe_calls_before_manual + 1

        # Concurrent manual_refresh with expired snapshot & cooldown=5.0 coalesces to 1 batch
        deps_health._snapshot = replace(  # noqa: SLF001
            deps_health.snapshot, monotonic_at=0.0
        )
        probe_started = asyncio.Event()
        probe_release = asyncio.Event()
        wait_on_probe = True
        calls_before_concurrent_manual = probe_calls
        service_five = ApplicationServerService(
            coordinator=coordinator,
            runtime_config=sample_runtime_config(),
            server_config=ServerConfig(min_manual_refresh_interval_seconds=5.0),
        )
        m_tasks = [
            asyncio.create_task(service_five.status(refresh=True)),
            asyncio.create_task(service_five.status(refresh=True)),
        ]
        await probe_started.wait()
        await asyncio.sleep(0)
        probe_release.set()
        await asyncio.gather(*m_tasks)
        assert probe_calls == calls_before_concurrent_manual + 1
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_lifecycle_transition_during_default_and_manual_probes_and_fail_closed() -> (
    None
):
    probe_started = asyncio.Event()
    probe_release = asyncio.Event()
    gate_next_probe = False
    probe_calls = 0

    async def controllable_pg() -> None:
        nonlocal probe_calls, gate_next_probe
        probe_calls += 1
        if gate_next_probe:
            gate_next_probe = False
            probe_started.set()
            await probe_release.wait()

    coordinator, harness = await start_test_coordinator(
        probes={"postgres": controllable_pg},
        query_cache_status=lambda redis_health: (
            "ACTIVE" if redis_health is DependencyHealth.HEALTHY else "BYPASSED"
        ),
        refresh_interval=60.0,
        manual_cooldown=0.0,
    )
    try:
        service = ApplicationServerService(
            coordinator=coordinator,
            runtime_config=sample_runtime_config(),
            server_config=ServerConfig(min_manual_refresh_interval_seconds=0.0),
        )
        deps_health = coordinator._deps.health  # noqa: SLF001

        # 1. Lifecycle changes while default refresh probe awaits completion
        assert deps_health.snapshot is not None
        deps_health._snapshot = replace(  # noqa: SLF001
            deps_health.snapshot, monotonic_at=0.0
        )
        gate_next_probe = True
        default_task = asyncio.create_task(service.status(refresh=False))
        await probe_started.wait()
        lease = await harness.gate.begin_maintenance()
        probe_release.set()
        st_default = await default_task
        assert st_default.mode is RuntimeMode.DRAINING
        assert st_default.ready is False
        await harness.gate.enter_exclusive_maintenance(lease)
        await harness.gate.complete_maintenance(lease, safe=True)
        await harness.gate.set_admission_ready(
            True, lifecycle_generation=harness.gate.lifecycle_generation
        )

        # 2. Lifecycle changes while manual refresh probe awaits completion
        probe_started = asyncio.Event()
        probe_release = asyncio.Event()
        gate_next_probe = True
        manual_task = asyncio.create_task(service.status(refresh=True))
        await probe_started.wait()
        lease2 = await harness.gate.begin_maintenance()
        probe_release.set()
        st_manual = await manual_task
        assert st_manual.mode is RuntimeMode.DRAINING
        assert st_manual.ready is False
        await harness.gate.enter_exclusive_maintenance(lease2)
        await harness.gate.complete_maintenance(lease2, safe=False)

        # 3. Persistent mismatched SERVING snapshot must NEVER produce ready=True
        stale_serving = HealthSnapshot(
            runtime=RuntimeMode.SERVING,
            hard_healthy=True,
            knowledge_verified=True,
            postgres=DependencyHealth.HEALTHY,
            milvus=DependencyHealth.HEALTHY,
            redis=DependencyHealth.HEALTHY,
            searxng=DependencyHealth.HEALTHY,
            embedding=DependencyHealth.HEALTHY,
            reranker=DependencyHealth.HEALTHY,
            query_cache="ACTIVE",
            generation=999,
            lifecycle_generation=harness.gate.lifecycle_generation + 99,
            checked_at=datetime.now(UTC),
            monotonic_at=0.0,
        )

        async def _stale_status(
            *, refresh: bool = False, min_refresh_interval_seconds: float | None = None
        ) -> HealthSnapshot:
            return stale_serving

        coordinator.status = _stale_status  # type: ignore[method-assign]
        fail_closed = await service.status()
        assert fail_closed.mode is harness.gate.mode
        assert fail_closed.ready is False
        assert fail_closed.knowledge_verified is False
        assert fail_closed.query_cache == "BYPASSED"
    finally:
        await coordinator.shutdown()
