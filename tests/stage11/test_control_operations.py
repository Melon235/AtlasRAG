"""WP-C tests for MaintenanceOperationRegistry, /api/v1/control/*, and shutdown boundary."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient

from atlasrag.domain.errors import (
    DependencyUnavailableError,
    RuntimeShutdownFailedError,
    RuntimeShuttingDownError,
)
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse
from atlasrag.providers.cache.keys import query_cache_key
from atlasrag.providers.cache.models import CachedQueryResult
from atlasrag.runtime.cache_freshness import CacheFreshnessBarrier, CacheFreshnessState
from atlasrag.runtime.coordinator import (
    ApplicationCoordinator,
    CoordinatorDependencies,
)
from atlasrag.runtime.gate import RuntimeGate
from atlasrag.runtime.health import HealthRegistry
from atlasrag.runtime.maintenance import MaintenanceCoordinator
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.runtime.ownership import RuntimeOwnershipLock
from atlasrag.runtime.tasks import InFlightTaskRegistry, TaskKind
from atlasrag.server.app import create_app
from atlasrag.server.config import ServerConfig
from atlasrag.server.errors import ServerHttpError
from atlasrag.server.operations import MaintenanceOperationRegistry
from atlasrag.server.schemas import OperationKind, OperationStatus
from atlasrag.server.service import ApplicationServerService, ShutdownOwner
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.cache.session import SessionQueryCache
from atlasrag.services.sync.models import (
    GlobalSafetyResult,
    ReconciliationOutcome,
    ReconciliationResult,
    ServingSafety,
    SyncOutcome,
    SyncResult,
)
from atlasrag.services.sync.source_safety import SourceSafetyResult
from stage11.conftest import (
    FakeRuntimeMetadataRepo,
    FakeSyncUow,
    _isolated_ownership_lock,
    healthy_probe,
    sample_response,
    sample_runtime_config,
    start_test_coordinator,
)

_CONTROL_TOKEN = "secret-control-token-wp-c"


def _control_headers(*, token: str | None = _CONTROL_TOKEN) -> dict[str, str]:
    headers = {
        "host": "127.0.0.1:8000",
        "origin": "http://127.0.0.1:8000",
    }
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return headers


def _web_headers() -> dict[str, str]:
    return {
        "host": "127.0.0.1:8000",
        "origin": "http://127.0.0.1:8000",
        "x-atlasrag-client": "web",
    }


def test_control_endpoints_enforce_bearer_host_origin_and_csrf_separation() -> None:
    """Missing/invalid Bearer -> 401; bad Host/Origin -> 403; X-AtlasRAG-Client cannot auth."""

    async def _run() -> None:
        coordinator, _ = await start_test_coordinator()
        try:
            app = create_app(
                runtime_config=sample_runtime_config(),
                server_config=ServerConfig(),
                coordinator=coordinator,
                control_token_provider=lambda: _CONTROL_TOKEN,
            )
            with TestClient(app, base_url="http://127.0.0.1:8000") as client:
                endpoints = [
                    ("POST", "/api/v1/control/sync"),
                    ("POST", "/api/v1/control/recover"),
                    ("GET", "/api/v1/control/operations/op_000000000000000000000000"),
                    ("POST", "/api/v1/control/shutdown"),
                ]
                for method, path in endpoints:
                    # Missing token
                    r_no = client.request(
                        method,
                        path,
                        headers={
                            "host": "127.0.0.1:8000",
                            "origin": "http://127.0.0.1:8000",
                        },
                    )
                    assert r_no.status_code == 401
                    assert r_no.json()["error_code"] == "UNAUTHORIZED"

                    # Invalid token
                    r_bad = client.request(
                        method, path, headers=_control_headers(token="wrong-token")
                    )
                    assert r_bad.status_code == 401

                    # X-AtlasRAG-Client header cannot substitute for Bearer
                    r_csrf = client.request(method, path, headers=_web_headers())
                    assert r_csrf.status_code == 401

                    # Bad Host -> 403
                    r_host = client.request(
                        method,
                        path,
                        headers={
                            "host": "evil.example:8000",
                            "authorization": f"Bearer {_CONTROL_TOKEN}",
                        },
                    )
                    assert r_host.status_code == 403

                # Cross-origin POST -> 403
                r_origin = client.post(
                    "/api/v1/control/sync",
                    headers={
                        "host": "127.0.0.1:8000",
                        "origin": "http://evil.example:8000",
                        "authorization": f"Bearer {_CONTROL_TOKEN}",
                    },
                )
                assert r_origin.status_code == 403

                # Extra field rejected (extra="forbid")
                r_extra = client.post(
                    "/api/v1/control/sync",
                    json={"force": True},
                    headers=_control_headers(),
                )
                assert r_extra.status_code == 422
        finally:
            await coordinator.shutdown()

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_sync_from_serving_and_maintenance_exclusion_and_recover_lifecycle() -> (
    None
):
    """Test sync from SERVING, exclusion during DRAINING/MAINTENANCE, duplicate 409, and BLOCKED recovery."""

    sync_entered = asyncio.Event()
    sync_release = asyncio.Event()
    phase = {"step": "sync"}

    async def controllable_sync() -> SyncResult:
        if phase["step"] == "sync":
            sync_entered.set()
            await sync_release.wait()
            return SyncResult(
                outcome=SyncOutcome.SUCCESS,
                serving_safety=ServingSafety.SAFE_TO_SERVE,
                checked_documents=3,
                ready_builds=1,
            )
        return SyncResult(
            outcome=SyncOutcome.SUCCESS,
            serving_safety=ServingSafety.SAFE_TO_SERVE,
            checked_documents=4,
            verified_documents=4,
        )

    coordinator, harness = await start_test_coordinator(sync_fn=controllable_sync)
    app = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        coordinator=coordinator,
        control_token_provider=lambda: _CONTROL_TOKEN,
    )
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8000",
            ) as client,
        ):
            sid = (
                await client.post(
                    "/api/v1/sessions",
                    json={"client_kind": "web"},
                    headers=_web_headers(),
                )
            ).json()["session_id"]

            # Recover in SERVING rejected 503
            rec_bad = await client.post(
                "/api/v1/control/recover", headers=_control_headers()
            )
            assert rec_bad.status_code == 503
            assert rec_bad.json()["error_code"] == "RUNTIME_NOT_READY"

            # Sync in SERVING -> 202 after DRAINING handoff
            acc = await client.post("/api/v1/control/sync", headers=_control_headers())
            assert acc.status_code == 202
            acc_body = acc.json()
            op_id = acc_body["operation_id"]
            assert op_id.startswith("op_") and len(op_id) == 27
            assert acc_body["kind"] == "SYNC"
            assert acc_body["status"] == "RUNNING"

            await sync_entered.wait()
            # Exactly one coordinator-owned MAINTENANCE task (no duplicate runtime ownership)
            assert len(harness.tasks.snapshot(TaskKind.MAINTENANCE)) == 1

            # Concurrent sync -> 409 MAINTENANCE_BUSY
            dup = await client.post("/api/v1/control/sync", headers=_control_headers())
            assert dup.status_code == 409
            assert dup.json()["error_code"] == "MAINTENANCE_BUSY"

            # Overlapping query / stream during MAINTENANCE -> 503 RUNTIME_NOT_READY
            q_blocked = await client.post(
                "/api/v1/query",
                json={"session_id": sid, "query": "during maintenance"},
                headers=_web_headers(),
            )
            assert q_blocked.status_code == 503
            assert q_blocked.json()["error_code"] == "RUNTIME_NOT_READY"

            q_stream_blocked = await client.post(
                "/api/v1/query/stream",
                json={"session_id": sid, "query": "during maintenance"},
                headers=_web_headers(),
            )
            assert q_stream_blocked.status_code == 503
            assert q_stream_blocked.json()["error_code"] == "RUNTIME_NOT_READY"

            sync_release.set()
            svc: ApplicationServerService = app.state.server_service
            await svc.operations.wait_active()

            st = await client.get(
                f"/api/v1/control/operations/{op_id}", headers=_control_headers()
            )
            assert st.status_code == 200
            st_body = st.json()
            assert st_body["status"] == "SUCCEEDED"
            assert st_body["sync_outcome"] == "SUCCESS"
            assert st_body["serving_safety"] == "SAFE_TO_SERVE"
            assert st_body["runtime_mode"] == "SERVING"
            assert st_body["summary"]["checked_documents"] == 3
            assert svc.operations.active_task is None
    finally:
        await coordinator.shutdown()

    # Start in BLOCKED -> sync rejected 503, recover succeeds 202 -> SUCCEEDED -> SERVING
    async def blocked_reconcile() -> ReconciliationResult:
        return ReconciliationResult(
            outcome=ReconciliationOutcome.BLOCKED,
            blocked_reasons=("BROKEN_INDEX",),
        )

    coord_blk, _ = await start_test_coordinator(reconcile_fn=blocked_reconcile)
    app_blk = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        coordinator=coord_blk,
        control_token_provider=lambda: _CONTROL_TOKEN,
    )
    try:
        async with (
            app_blk.router.lifespan_context(app_blk),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_blk),
                base_url="http://127.0.0.1:8000",
            ) as client,
        ):
            assert (await client.get("/api/v1/health/ready")).status_code == 503
            sync_blk = await client.post(
                "/api/v1/control/sync", headers=_control_headers()
            )
            assert sync_blk.status_code == 503

            rec_acc = await client.post(
                "/api/v1/control/recover", headers=_control_headers()
            )
            assert rec_acc.status_code == 202
            rec_op = rec_acc.json()["operation_id"]
            await app_blk.state.server_service.operations.wait_active()
            rec_st = (
                await client.get(
                    f"/api/v1/control/operations/{rec_op}", headers=_control_headers()
                )
            ).json()
            assert rec_st["kind"] == "RECOVER"
            assert rec_st["status"] == "SUCCEEDED"
            assert rec_st["serving_safety"] == "SAFE_TO_SERVE"
            assert rec_st["runtime_mode"] == "SERVING"
            assert (await client.get("/api/v1/health/ready")).status_code == 200
    finally:
        await coord_blk.shutdown()


@pytest.mark.asyncio
async def test_maintenance_admission_races_on_draining_abort_and_client_cancel() -> (
    None
):
    """Verify rollback before entry, on_draining failure aborting MaintenanceLease, client cancel, and shutdown races."""

    coordinator, harness = await start_test_coordinator()
    registry = MaintenanceOperationRegistry(
        coordinator=coordinator,
        server_config=ServerConfig(),
    )
    try:
        # 1. Exception in on_draining callback MUST abort MaintenanceLease (never strand DRAINING!)
        def failing_on_draining() -> None:
            raise RuntimeError("injected on_draining boom")

        with pytest.raises(ServerHttpError):
            await registry.start_operation(
                OperationKind.SYNC, on_draining_hook=failing_on_draining
            )
        assert harness.gate.mode is RuntimeMode.SERVING
        assert harness.tasks.snapshot() == ()
        assert registry.active_operation_id is None
        assert registry.active_task is None
        assert registry.retained_count() == 0

        # 2. Failure inside coordinator before begin_maintenance (e.g. begin_maintenance fails) rolls back
        real_begin = harness.gate.begin_maintenance

        async def fail_before_lease() -> Any:
            raise DependencyUnavailableError

        harness.gate.begin_maintenance = fail_before_lease  # type: ignore[method-assign]
        with pytest.raises(DependencyUnavailableError):
            await registry.start_operation(OperationKind.SYNC)
        harness.gate.begin_maintenance = real_begin  # type: ignore[method-assign]
        assert harness.gate.mode is RuntimeMode.SERVING
        assert registry.active_operation_id is None
        assert registry.retained_count() == 0

        # 3. Client cancellation before DRAINING handoff cancels safely without stranding lease
        enter_begin = asyncio.Event()
        release_begin = asyncio.Event()

        async def paused_begin() -> Any:
            enter_begin.set()
            await release_begin.wait()
            return await real_begin()

        harness.gate.begin_maintenance = paused_begin  # type: ignore[method-assign]
        req_task = asyncio.create_task(registry.start_operation(OperationKind.SYNC))
        await enter_begin.wait()
        req_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await req_task
        harness.gate.begin_maintenance = real_begin  # type: ignore[method-assign]
        assert harness.gate.mode is RuntimeMode.SERVING
        assert registry.active_operation_id is None
        assert registry.active_task is None
        assert registry.retained_count() == 0
        assert harness.tasks.snapshot() == ()

        # 4. Shutdown racing between DRAINING admission and HTTP 202 return fails safely without false 202
        async def slow_sync() -> SyncResult:
            await asyncio.sleep(0.02)
            return SyncResult(
                outcome=SyncOutcome.SUCCESS, serving_safety=ServingSafety.SAFE_TO_SERVE
            )

        coordinator._deps.maintenance._run_sync = slow_sync  # noqa: SLF001

        def shutdown_during_draining() -> None:
            # Simulate gate entering SHUTTING_DOWN right after DRAINING handoff
            harness.gate._mode = RuntimeMode.SHUTTING_DOWN  # noqa: SLF001
            harness.gate._shutdown = True  # noqa: SLF001

        with pytest.raises(RuntimeShuttingDownError):
            await registry.start_operation(
                OperationKind.SYNC,
                on_draining_hook=shutdown_during_draining,
            )
        await registry.wait_active()
        assert registry.active_task is None
    finally:
        await coordinator.shutdown()

    # 5. Shutdown between operation reservation and coordinator entry rolls back cleanly
    coord_pre, harness_pre = await start_test_coordinator()
    reg_pre = MaintenanceOperationRegistry(
        coordinator=coord_pre, server_config=ServerConfig()
    )
    try:
        real_run_maint = coord_pre.run_maintenance

        async def racing_run_maint(**kwargs: Any) -> SyncResult:
            await harness_pre.gate.begin_shutdown()
            return await real_run_maint(**kwargs)

        coord_pre.run_maintenance = racing_run_maint  # type: ignore[method-assign]
        with pytest.raises(RuntimeShuttingDownError):
            await reg_pre.start_operation(OperationKind.SYNC)
        assert reg_pre.active_operation_id is None
        assert reg_pre.active_task is None
        assert reg_pre.retained_count() == 0
    finally:
        await coord_pre.shutdown()

    # 6. Terminal SyncOutcome=FAILED + UNSAFE_TO_SERVE and SUCCESS + UNSAFE_TO_SERVE keep BLOCKED
    for outcome_val in (SyncOutcome.SUCCESS, SyncOutcome.FAILED):

        async def unsafe_sync(val: SyncOutcome = outcome_val) -> SyncResult:
            return SyncResult(
                outcome=val,
                serving_safety=ServingSafety.UNSAFE_TO_SERVE,
                unsafe_reasons=("VERIFY_MISMATCH",),
                blocked_code="SAFETY_FAILED" if val is SyncOutcome.FAILED else None,
            )

        coord_unsafe, harness_unsafe = await start_test_coordinator(sync_fn=unsafe_sync)
        reg_unsafe = MaintenanceOperationRegistry(
            coordinator=coord_unsafe, server_config=ServerConfig()
        )
        try:
            acc_u = await reg_unsafe.start_operation(OperationKind.SYNC)
            await reg_unsafe.wait_active()
            st_u = await reg_unsafe.get_operation(acc_u.operation_id)
            assert st_u.sync_outcome is outcome_val
            assert st_u.serving_safety is ServingSafety.UNSAFE_TO_SERVE
            assert st_u.runtime_mode is RuntimeMode.BLOCKED
            assert harness_unsafe.gate.mode is RuntimeMode.BLOCKED
        finally:
            await coord_unsafe.shutdown()


@pytest.mark.asyncio
async def test_maintenance_disconnect_resilience_drain_timeout_and_unsafe_verdicts() -> (
    None
):
    """Test disconnect survival, drain timeout, PARTIAL_SUCCESS+UNSAFE_TO_SERVE, and safe error projection."""

    sync_started = asyncio.Event()
    sync_finish = asyncio.Event()

    async def gated_sync() -> SyncResult:
        sync_started.set()
        await sync_finish.wait()
        return SyncResult(
            outcome=SyncOutcome.PARTIAL_SUCCESS,
            serving_safety=ServingSafety.UNSAFE_TO_SERVE,
            checked_documents=2,
            failed_builds=1,
            unsafe_reasons=("MISMATCHED_RECORDS", "MISSING_CHUNKS"),
            blocked_code="GLOBAL_VERIFY_FAILED",
        )

    coord, harness = await start_test_coordinator(sync_fn=gated_sync)
    app = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        coordinator=coord,
        control_token_provider=lambda: _CONTROL_TOKEN,
    )
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8000",
            ) as client:
                acc = await client.post(
                    "/api/v1/control/sync", headers=_control_headers()
                )
                assert acc.status_code == 202
                op_id = acc.json()["operation_id"]
                await sync_started.wait()
                # Client disconnects (closes client connection/context) immediately after 202

            sync_finish.set()
            svc: ApplicationServerService = app.state.server_service
            await svc.operations.wait_active()

            # Reconnect fresh client and verify operation survived & PARTIAL_SUCCESS + UNSAFE_TO_SERVE stays BLOCKED
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8000",
            ) as client2:
                st = (
                    await client2.get(
                        f"/api/v1/control/operations/{op_id}",
                        headers=_control_headers(),
                    )
                ).json()
                assert st["status"] == "SUCCEEDED"
                assert st["sync_outcome"] == "PARTIAL_SUCCESS"
                assert st["serving_safety"] == "UNSAFE_TO_SERVE"
                assert st["runtime_mode"] == "BLOCKED"
                assert st["unsafe_reasons"] == ["MISMATCHED_RECORDS", "MISSING_CHUNKS"]
                assert st["blocked_code"] == "GLOBAL_VERIFY_FAILED"
                assert (await client2.get("/api/v1/health/ready")).status_code == 503
    finally:
        await coord.shutdown()

    # Drain timeout while a query stays busy -> operation FAILED + MAINTENANCE_DRAIN_TIMEOUT
    q_started = asyncio.Event()
    q_release = asyncio.Event()

    async def slow_query(_req: UserTurnRequest) -> FinalResponse:
        q_started.set()
        await q_release.wait()
        return sample_response()

    coord2, harness2 = await start_test_coordinator(retrieval=slow_query)
    coord2._deps.maintenance._drain_timeout = 0.02  # noqa: SLF001
    svc2 = ApplicationServerService(
        coordinator=coord2,
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
    )
    try:
        q_task = asyncio.create_task(
            coord2.run_query(UserTurnRequest(session_id="s1", query="hold drain"))
        )
        await q_started.wait()
        acc2 = await svc2.start_sync()
        await svc2.operations.wait_active()
        st2 = await svc2.get_operation(acc2.operation_id)
        assert st2.status is OperationStatus.FAILED
        assert st2.error_code == "MAINTENANCE_DRAIN_TIMEOUT"
        assert st2.sync_outcome is None
        assert st2.serving_safety is None
        q_release.set()
        await q_task
        assert harness2.gate.mode is RuntimeMode.SERVING
    finally:
        await coord2.shutdown()

    # Unhandled maintenance exception with sensitive DSN/path projects safe error only
    async def secret_crash_sync() -> SyncResult:
        raise RuntimeError("secret path /home/private/key.pem")

    coord3, _ = await start_test_coordinator(sync_fn=secret_crash_sync)
    svc3 = ApplicationServerService(
        coordinator=coord3,
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
    )
    try:
        acc3 = await svc3.start_sync()
        await svc3.operations.wait_active()
        st3 = await svc3.get_operation(acc3.operation_id)
        assert st3.status is OperationStatus.FAILED
        assert st3.error_code == "INTERNAL_ERROR"
        assert st3.message is not None and "/home/private" not in st3.message
    finally:
        await coord3.shutdown()


@pytest.mark.asyncio
async def test_maintenance_freshness_invalidation_and_bounded_retention() -> None:
    """Verify CacheFreshnessBarrier invalidation across /control/sync and FIFO operation retention."""

    class _RedisCacheStub:
        def __init__(self) -> None:
            self.entries: dict[str, CachedQueryResult] = {}
            self.write_fence = "fence-1"
            self.fence_counter = 1
            self.invalidations = 0

        async def invalidate_queries(self) -> int:
            self.invalidations += 1
            n = len(self.entries)
            self.entries.clear()
            return n

        async def establish_query_write_fence(self) -> str:
            self.fence_counter += 1
            self.write_fence = f"fence-{self.fence_counter}"
            return self.write_fence

        async def ensure_query_write_fence(self) -> str:
            return self.write_fence

        async def revoke_query_write_fence(self) -> None:
            return None

        async def get_query_write_fence(self) -> str | None:
            return self.write_fence

        async def get_fenced_query(
            self, session_id: str, fp: str, h: str
        ) -> tuple[str | None, CachedQueryResult | None]:
            return (
                self.write_fence,
                self.entries.get(query_cache_key(session_id, fp, h)),
            )

        async def put_fenced_query(
            self,
            session_id: str,
            fp: str,
            h: str,
            value: CachedQueryResult,
            *,
            ttl_seconds: int,
            write_fence: str,
        ) -> bool:
            del ttl_seconds
            if write_fence != self.write_fence:
                return False
            self.entries[query_cache_key(session_id, fp, h)] = value
            return True

    redis = _RedisCacheStub()
    repo = FakeRuntimeMetadataRepo(invalidation_required=False)
    freshness = CacheFreshnessBarrier(
        redis=cast(Any, redis),
        uow_factory=cast(Any, lambda: FakeSyncUow(repo)),
    )
    answer_version = {"v": 1}

    async def cached_retrieval(req: UserTurnRequest) -> FinalResponse:
        cache = SessionQueryCache(
            redis=cast(Any, redis),
            uow_factory=cast(Any, lambda: FakeSyncUow(repo)),
            runtime_fingerprint="a" * 64,
            ttl_seconds=600,
            policy=QueryCachePolicy(enabled=True),
            clock=lambda: datetime.now(UTC),
            require_write_fence=True,
            expected_write_fence=lambda: freshness.active_write_fence,
        )
        hit, _ = await cache.lookup_with_status(req)
        if hit is not None:
            return hit
        resp = sample_response(f"knowledge-v{answer_version['v']}")
        from atlasrag.domain.enums import RagCompletionStatus
        from atlasrag.domain.results import RagCoreResult

        await cache.store(
            req,
            RagCoreResult(
                final_response=resp, completion_status=RagCompletionStatus.SUCCESS
            ),
            web_invoked=False,
        )
        return resp

    async def mutating_sync() -> SyncResult:
        await repo.set_query_cache_invalidation_required(True)
        answer_version["v"] = 2
        return SyncResult(
            outcome=SyncOutcome.SUCCESS,
            serving_safety=ServingSafety.SAFE_TO_SERVE,
            ready_builds=1,
        )

    gate = RuntimeGate()
    tasks = InFlightTaskRegistry()
    health = HealthRegistry(
        mode=lambda: gate.mode,
        lifecycle_generation=lambda: gate.lifecycle_generation,
        postgres=healthy_probe,
        milvus=healthy_probe,
        redis=healthy_probe,
        embedding=healthy_probe,
    )
    ownership: RuntimeOwnershipLock = _isolated_ownership_lock()
    coord = await ApplicationCoordinator.start(
        CoordinatorDependencies(
            ownership=ownership,
            gate=gate,
            tasks=tasks,
            health=health,
            freshness=freshness,
            maintenance=MaintenanceCoordinator(
                gate=gate,
                run_sync=mutating_sync,
                freshness=freshness,
                drain_timeout_seconds=1.0,
            ),
            retrieval=cached_retrieval,
            reconcile=lambda: asyncio.sleep(
                0, result=ReconciliationResult(outcome=ReconciliationOutcome.CLEAN)
            ),
            source_safety=lambda: asyncio.sleep(
                0, result=SourceSafetyResult(safe=True)
            ),
            verify=lambda: asyncio.sleep(
                0,
                result=GlobalSafetyResult(serving_safety=ServingSafety.SAFE_TO_SERVE),
            ),
        )
    )
    app = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(max_retained_operations=3),
        coordinator=coord,
        control_token_provider=lambda: _CONTROL_TOKEN,
    )
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8000",
            ) as client,
        ):
            sid = (
                await client.post(
                    "/api/v1/sessions",
                    json={"client_kind": "web"},
                    headers=_web_headers(),
                )
            ).json()["session_id"]

            r1 = await client.post(
                "/api/v1/query",
                json={"session_id": sid, "query": "versioned"},
                headers=_web_headers(),
            )
            assert r1.json()["response"]["answer_text"] == "knowledge-v1"
            assert freshness.state is CacheFreshnessState.TRUSTED

            # Run sync -> CacheFreshnessBarrier enforces invalidation and rotates fence
            acc = await client.post("/api/v1/control/sync", headers=_control_headers())
            assert acc.status_code == 202
            svc: ApplicationServerService = app.state.server_service
            await svc.operations.wait_active()
            assert redis.invalidations >= 1
            assert repo.invalidation_required is False

            r2 = await client.post(
                "/api/v1/query",
                json={"session_id": sid, "query": "versioned"},
                headers=_web_headers(),
            )
            assert r2.json()["response"]["answer_text"] == "knowledge-v2"

            # Bounded retention (max_retained_operations=3): run 4 more syncs -> first op evicted -> 404
            op_ids = [acc.json()["operation_id"]]
            for _ in range(4):
                a = await client.post(
                    "/api/v1/control/sync", headers=_control_headers()
                )
                assert a.status_code == 202
                op_ids.append(a.json()["operation_id"])
                await svc.operations.wait_active()

            assert svc.operations.retained_count() == 3
            missing = await client.get(
                f"/api/v1/control/operations/{op_ids[0]}",
                headers=_control_headers(),
            )
            assert missing.status_code == 404
            assert missing.json()["error_code"] == "OPERATION_NOT_FOUND"
            assert (
                await client.get(
                    f"/api/v1/control/operations/{op_ids[-1]}",
                    headers=_control_headers(),
                )
            ).status_code == 200
    finally:
        await coord.shutdown()

    # Active operation must NOT be evicted even if terminal count reaches max_retained_operations
    hold_started = asyncio.Event()
    hold_release = asyncio.Event()
    call_idx = {"n": 0}

    async def retention_sync() -> SyncResult:
        call_idx["n"] += 1
        if call_idx["n"] == 3:
            hold_started.set()
            await hold_release.wait()
        return SyncResult(
            outcome=SyncOutcome.SUCCESS, serving_safety=ServingSafety.SAFE_TO_SERVE
        )

    coord_act, _ = await start_test_coordinator(sync_fn=retention_sync)
    reg = MaintenanceOperationRegistry(
        coordinator=coord_act, server_config=ServerConfig(max_retained_operations=2)
    )
    try:
        op1 = await reg.start_operation(OperationKind.SYNC)
        await reg.wait_active()
        op2 = await reg.start_operation(OperationKind.SYNC)
        await reg.wait_active()
        op3 = await reg.start_operation(OperationKind.SYNC)
        await hold_started.wait()
        assert reg.retained_count() == 2
        assert (
            await reg.get_operation(op3.operation_id)
        ).status is OperationStatus.RUNNING
        assert (
            await reg.get_operation(op2.operation_id)
        ).status is OperationStatus.SUCCEEDED
        with pytest.raises(ServerHttpError):
            await reg.get_operation(op1.operation_id)
        hold_release.set()
        await reg.wait_active()
        assert (
            await reg.get_operation(op3.operation_id)
        ).status is OperationStatus.SUCCEEDED
    finally:
        await coord_act.shutdown()


@pytest.mark.asyncio
async def test_control_shutdown_boundary_injection_and_admission_closure() -> None:
    """POST /control/shutdown closes admission promptly without inline coordinator.shutdown(), or fails safely."""

    # 1. Missing shutdown_owner fails safely without closing gate or fabricating 202
    coord_no_owner, harness_no = await start_test_coordinator()
    app_no = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        coordinator=coord_no_owner,
        control_token_provider=lambda: _CONTROL_TOKEN,
    )
    try:
        async with (
            app_no.router.lifespan_context(app_no),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_no),
                base_url="http://127.0.0.1:8000",
            ) as client,
        ):
            resp = await client.post(
                "/api/v1/control/shutdown", headers=_control_headers()
            )
            assert resp.status_code == 503
            assert harness_no.gate.mode is RuntimeMode.SERVING
    finally:
        await coord_no_owner.shutdown()

    # 2. Configured ShutdownOwner closes admission promptly, rejects all new work, does NOT run shutdown inline
    owner_called = {"count": 0}
    coord, harness = await start_test_coordinator()

    def _on_shutdown_request() -> None:
        owner_called["count"] += 1

    owner = ShutdownOwner(
        pid=424242,
        server_instance_id="srv_111122223333444455556666",
        request_shutdown=_on_shutdown_request,
    )
    app = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        coordinator=coord,
        control_token_provider=lambda: _CONTROL_TOKEN,
        shutdown_owner=owner,
    )
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8000",
            ) as client,
        ):
            sid = (
                await client.post(
                    "/api/v1/sessions",
                    json={"client_kind": "web"},
                    headers=_web_headers(),
                )
            ).json()["session_id"]

            sh = await client.post(
                "/api/v1/control/shutdown", headers=_control_headers()
            )
            assert sh.status_code == 202
            assert sh.json() == {
                "status": "SHUTTING_DOWN",
                "pid": 424242,
                "server_instance_id": "srv_111122223333444455556666",
            }
            assert owner_called["count"] == 1
            assert harness.gate.mode is RuntimeMode.SHUTTING_DOWN
            # 202 does NOT falsely claim runtime cleanup finished (ownership still held!)
            assert harness.ownership.acquired

            # Prompt rejection across knowledge, status, and control endpoints
            assert (
                await client.post(
                    "/api/v1/sessions",
                    json={"client_kind": "web"},
                    headers=_web_headers(),
                )
            ).status_code == 503
            assert (
                await client.post(
                    "/api/v1/query",
                    json={"session_id": sid, "query": "post shutdown"},
                    headers=_web_headers(),
                )
            ).status_code == 503
            assert (
                await client.post(
                    "/api/v1/query/stream",
                    json={"session_id": sid, "query": "post shutdown"},
                    headers=_web_headers(),
                )
            ).status_code == 503
            assert (
                await client.post("/api/v1/control/sync", headers=_control_headers())
            ).status_code == 503
            assert (
                await client.post("/api/v1/control/recover", headers=_control_headers())
            ).status_code == 503
            assert (await client.get("/api/v1/status")).status_code == 503
    finally:
        await coord.shutdown()


@pytest.mark.asyncio
async def test_wpc_f04_maintenance_idempotent_cancellation_and_bounded_wait_active() -> (
    None
):
    """WPC-F04: No duplicate cancel across HTTP+shutdown races, rollback intact, and wait_active timeout safe."""

    # Scenario 1: Pre-admission HTTP cancellation first, coordinator shutdown second
    worker_started = asyncio.Event()
    settling_started = asyncio.Event()
    worker_release = asyncio.Event()
    cancel_deliveries = 0

    async def shielded_pre_admission() -> None:
        nonlocal cancel_deliveries
        inner = asyncio.create_task(worker_release.wait())
        worker_started.set()
        cancelled = False
        while not inner.done():
            try:
                await asyncio.shield(inner)
            except asyncio.CancelledError:
                cancel_deliveries += 1
                settling_started.set()
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    coord1, harness1 = await start_test_coordinator(shutdown_timeout_seconds=0.02)
    reg1 = MaintenanceOperationRegistry(
        coordinator=coord1, server_config=ServerConfig()
    )
    real_begin1 = harness1.gate.begin_maintenance

    async def shielded_begin1() -> Any:
        await shielded_pre_admission()
        return await real_begin1()

    harness1.gate.begin_maintenance = shielded_begin1  # type: ignore[method-assign]
    shut1_phase = asyncio.Event()
    real_cancel_all1 = harness1.tasks.cancel_all

    def tracked_cancel1() -> None:
        shut1_phase.set()
        real_cancel_all1()

    harness1.tasks.cancel_all = tracked_cancel1  # type: ignore[method-assign]
    try:
        req1 = asyncio.create_task(reg1.start_operation(OperationKind.SYNC))
        await worker_started.wait()
        assert reg1.active_operation_id is not None

        # Cancel HTTP pre-admission first, hold unsettled while coordinator.shutdown() runs
        req1.cancel()
        await settling_started.wait()
        assert cancel_deliveries == 1

        shut_task1 = asyncio.create_task(coord1.shutdown())
        await shut1_phase.wait()
        with pytest.raises(RuntimeShutdownFailedError):
            await shut_task1
        assert cancel_deliveries == 1
        assert harness1.ownership.acquired

        # Release worker -> clean settlement, rollback of STARTING reservation, zero stranded lease
        worker_release.set()
        with pytest.raises(asyncio.CancelledError):
            await req1
        await coord1.shutdown()
        assert reg1.active_operation_id is None
        assert reg1.active_task is None
        assert reg1.retained_count() == 0
        assert harness1.tasks.snapshot() == ()
        assert not harness1.ownership.acquired
    finally:
        await coord1.shutdown()

    # Scenario 2: Coordinator shutdown cancellation first, repeated HTTP cancellation second
    worker_started.clear()
    settling_started.clear()
    worker_release.clear()
    cancel_deliveries = 0

    coord2, harness2 = await start_test_coordinator(shutdown_timeout_seconds=0.02)
    reg2 = MaintenanceOperationRegistry(
        coordinator=coord2, server_config=ServerConfig()
    )
    real_begin2 = harness2.gate.begin_maintenance

    async def shielded_begin2() -> Any:
        await shielded_pre_admission()
        return await real_begin2()

    harness2.gate.begin_maintenance = shielded_begin2
    shut2_phase = asyncio.Event()
    real_cancel_all2 = harness2.tasks.cancel_all

    def tracked_cancel2() -> None:
        shut2_phase.set()
        real_cancel_all2()

    harness2.tasks.cancel_all = tracked_cancel2
    try:
        req2 = asyncio.create_task(reg2.start_operation(OperationKind.SYNC))
        await worker_started.wait()

        # Trigger coordinator shutdown first
        shut_task2 = asyncio.create_task(coord2.shutdown())
        await shut2_phase.wait()
        await settling_started.wait()
        assert cancel_deliveries == 1

        # Repeated HTTP outer cancellations while worker remains unsettled
        req2.cancel()
        await asyncio.sleep(0)
        req2.cancel()
        with pytest.raises(RuntimeShutdownFailedError):
            await shut_task2
        assert cancel_deliveries == 1
        assert harness2.ownership.acquired

        worker_release.set()
        with pytest.raises(asyncio.CancelledError):
            await req2
        await coord2.shutdown()
        assert reg2.active_operation_id is None
        assert reg2.active_task is None
        assert reg2.retained_count() == 0
        assert harness2.tasks.snapshot() == ()
        assert not harness2.ownership.acquired
    finally:
        await coord2.shutdown()

    # Scenario 3: Bounded wait_active(timeout=...) returns False without cancelling worker or faking cleanup
    op_entered = asyncio.Event()
    op_release = asyncio.Event()

    async def long_sync() -> SyncResult:
        inner = asyncio.create_task(op_release.wait())
        op_entered.set()
        cancelled = False
        while not inner.done():
            try:
                await asyncio.shield(inner)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return SyncResult(
            outcome=SyncOutcome.SUCCESS,
            serving_safety=ServingSafety.SAFE_TO_SERVE,
            checked_documents=2,
        )

    coord3, harness3 = await start_test_coordinator(
        sync_fn=long_sync,
        shutdown_timeout_seconds=0.02,
    )
    reg3 = MaintenanceOperationRegistry(
        coordinator=coord3, server_config=ServerConfig()
    )
    try:
        accepted = await reg3.start_operation(OperationKind.SYNC)
        await op_entered.wait()

        t0 = monotonic()
        settled = await reg3.wait_active(timeout=0.03)
        elapsed = monotonic() - t0
        assert settled is False
        assert elapsed < 0.5
        # Must NOT cancel op_task, mark operation terminal, or release ownership!
        assert reg3.active_task is not None
        assert not reg3.active_task.done()
        assert not reg3.active_task.cancelled()
        status_mid = await reg3.get_operation(accepted.operation_id)
        assert status_mid.status is OperationStatus.RUNNING
        assert harness3.ownership.acquired
        assert len(harness3.tasks.snapshot(TaskKind.MAINTENANCE)) == 1

        # Shutdown fails closed while unresolved maintenance stays owned
        with pytest.raises(RuntimeShutdownFailedError):
            await coord3.shutdown()
        assert harness3.ownership.acquired

        # Settling worker permits normal final cleanup
        op_release.set()
        assert await reg3.wait_active(timeout=1.0) is True
        status_end = await reg3.get_operation(accepted.operation_id)
        assert status_end.status is OperationStatus.FAILED
        assert reg3.active_task is None
        await coord3.shutdown()
        assert not harness3.ownership.acquired
    finally:
        await coord3.shutdown()
