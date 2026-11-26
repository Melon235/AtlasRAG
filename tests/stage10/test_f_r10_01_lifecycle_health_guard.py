"""Regressions for F-R10-01-01, R10-03-F02, F-R10-01-02, and F-R10-01-03."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
import redis.asyncio as redis
from runtime_harness import (
    Probe,
    RecordingOwnership,
    build_harness,
    pump_until,
    request,
)
from support import runtime_config

from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.enums import SourceType
from atlasrag.domain.errors import (
    DependencyUnavailableError,
    RuntimeBootstrapFailedError,
    RuntimeNotReadyError,
    RuntimeOwnershipConflictError,
    RuntimeShutdownFailedError,
    RuntimeShuttingDownError,
)
from atlasrag.domain.ingestion.source import SourceFile
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse
from atlasrag.runtime import composition as composition_module
from atlasrag.runtime import production as production_module
from atlasrag.runtime.coordinator import (
    ApplicationCoordinator,
    CoordinatorDependencies,
)
from atlasrag.runtime.gate import RuntimeGate
from atlasrag.runtime.health import HealthRegistry
from atlasrag.runtime.maintenance import MaintenanceCoordinator
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.runtime.resources import ResourceGuard
from atlasrag.runtime.tasks import InFlightTaskRegistry
from atlasrag.services.sync.models import (
    GlobalSafetyResult,
    ReconciliationOutcome,
    ReconciliationResult,
    ServingSafety,
)
from atlasrag.services.sync.source_safety import SourceSafetyResult


async def _healthy() -> None:
    return None


def _direct_deps(
    *,
    ownership: Any,
    open_resources: Any = None,
    reconcile: Any = None,
    close_resources: Any = None,
    ownership_acquired: bool = False,
) -> CoordinatorDependencies:
    gate = RuntimeGate()
    tasks = InFlightTaskRegistry()
    health = HealthRegistry(
        mode=lambda: gate.mode,
        postgres=_healthy,
        milvus=_healthy,
        redis=_healthy,
        embedding=_healthy,
        lifecycle_generation=lambda: gate.lifecycle_generation,
    )
    freshness = object()

    async def _default_open() -> None:
        return None

    async def _default_close() -> None:
        return None

    async def _default_reconcile() -> ReconciliationResult:
        return ReconciliationResult(
            outcome=ReconciliationOutcome.CLEAN, checked_documents=0
        )

    async def _default_source_safety() -> SourceSafetyResult:
        return SourceSafetyResult(safe=True)

    async def _default_verify() -> GlobalSafetyResult:
        return GlobalSafetyResult(serving_safety=ServingSafety.SAFE_TO_SERVE)

    async def _default_sync() -> Any:
        raise AssertionError("sync unused")

    async def _default_retrieval(_req: UserTurnRequest) -> FinalResponse:
        return FinalResponse(answer_text="ok", citations=())

    return CoordinatorDependencies(
        ownership=ownership,
        gate=gate,
        tasks=tasks,
        health=health,
        freshness=freshness,  # type: ignore[arg-type]
        maintenance=MaintenanceCoordinator(
            gate=gate,
            run_sync=_default_sync,
            freshness=freshness,  # type: ignore[arg-type]
            drain_timeout_seconds=1.0,
        ),
        retrieval=_default_retrieval,
        reconcile=reconcile or _default_reconcile,
        source_safety=_default_source_safety,
        verify=_default_verify,
        open_resources=open_resources or _default_open,
        close_resources=close_resources or _default_close,
        ownership_acquired=ownership_acquired,
    )


# ---------------------------------------------------------------------------
# F-R10-01-01 & R10-03-F02: Startup rollback, lock retention, cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f_r10_01_01_direct_coordinator_close_failure_retains_ownership_and_preserves_cause() -> (
    None
):
    log: list[str] = []
    ownership = RecordingOwnership(log)

    async def fail_reconcile() -> ReconciliationResult:
        raise DependencyUnavailableError()

    async def fail_close() -> None:
        log.append("close_failed")
        raise OSError("close broke: user=postgres password=super-secret")

    deps = _direct_deps(
        ownership=ownership,
        reconcile=fail_reconcile,
        close_resources=fail_close,
        ownership_acquired=False,
    )
    with pytest.raises(RuntimeBootstrapFailedError) as exc_info:
        await ApplicationCoordinator.start(deps)

    assert isinstance(exc_info.value.__cause__, DependencyUnavailableError)
    assert ownership.acquired == 1
    assert ownership.released == 0
    assert log == ["acquire", "close_failed"]
    notes = " ".join(
        list(getattr(exc_info.value, "__notes__", ()))
        + list(getattr(exc_info.value.__cause__, "__notes__", ()))
    )
    assert "OSError" in notes
    assert "super-secret" not in notes


@pytest.mark.asyncio
async def test_f_r10_01_01_direct_coordinator_clean_rollback_releases_lock_once() -> (
    None
):
    log: list[str] = []
    ownership = RecordingOwnership(log)

    async def fail_reconcile() -> ReconciliationResult:
        raise ValueError("bootstrap failure")

    async def ok_close() -> None:
        log.append("close_ok")

    deps = _direct_deps(
        ownership=ownership,
        reconcile=fail_reconcile,
        close_resources=ok_close,
        ownership_acquired=False,
    )
    with pytest.raises(RuntimeBootstrapFailedError) as exc_info:
        await ApplicationCoordinator.start(deps)
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert ownership.acquired == 1
    assert ownership.released == 1
    assert log == ["acquire", "close_ok", "release"]


@pytest.mark.asyncio
async def test_f_r10_01_01_direct_coordinator_ownership_conflict_skips_close_and_release() -> (
    None
):
    calls: list[str] = []

    class ConflictOwnership:
        def acquire(self) -> None:
            calls.append("acquire_conflict")
            raise RuntimeOwnershipConflictError

        def release(self) -> None:
            calls.append("release")
            raise AssertionError("release must not be called on failed acquire")

    async def open_resources() -> None:
        calls.append("open")

    async def close_resources() -> None:
        calls.append("close")

    deps = _direct_deps(
        ownership=ConflictOwnership(),
        open_resources=open_resources,
        close_resources=close_resources,
        ownership_acquired=False,
    )
    with pytest.raises(RuntimeBootstrapFailedError) as exc_info:
        await ApplicationCoordinator.start(deps)
    assert isinstance(exc_info.value.__cause__, RuntimeOwnershipConflictError)
    assert calls == ["acquire_conflict"]


@pytest.mark.asyncio
@pytest.mark.parametrize("close_fails", [False, True])
async def test_f_r10_01_01_cancellation_during_startup_preserves_cancel_and_lock_contract(
    close_fails: bool,
) -> None:
    log: list[str] = []
    ownership = RecordingOwnership(log)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_reconcile() -> ReconciliationResult:
        started.set()
        await release.wait()
        return ReconciliationResult(
            outcome=ReconciliationOutcome.CLEAN, checked_documents=0
        )

    async def close_cb() -> None:
        log.append("close")
        if close_fails:
            raise OSError("cleanup broke")

    deps = _direct_deps(
        ownership=ownership,
        reconcile=slow_reconcile,
        close_resources=close_cb,
        ownership_acquired=False,
    )
    task = asyncio.create_task(ApplicationCoordinator.start(deps))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)

    assert log[0:2] == ["acquire", "close"]
    if close_fails:
        assert ownership.released == 0
    else:
        assert ownership.released == 1


@pytest.mark.asyncio
async def test_f_r10_01_01_composition_double_failure_retains_lock_and_preserves_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool_closes = 0
    ownership_log: list[str] = []

    class Pool:
        def __init__(self, dsn: str, *, min_size: int, max_size: int) -> None:
            _ = (dsn, min_size, max_size)

        async def open(self) -> None:
            return None

        async def close(self) -> None:
            nonlocal pool_closes
            pool_closes += 1
            raise OSError("pool close leaked: pw=secret")

        async def health(self) -> None:
            return None

    class Milvus:
        def __init__(self, *a: Any, **k: Any) -> None:
            self.embedding_dimension = runtime_config().part_i.embedding_dimension
            self.analyzer = runtime_config().part_i.bm25_analyzer

        async def initialize(self) -> None:
            return None

        async def health(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def iter_all_projection(self, *a: Any, **k: Any) -> Any:
            if False:
                yield None

        async def iter_revision_projection(self, *a: Any, **k: Any) -> Any:
            if False:
                yield None

        async def delete_revision(self, *a: Any, **k: Any) -> int:
            return 0

        async def wait_revision_visible(self, *a: Any, **k: Any) -> None:
            return None

    class RerankStub:
        def __init__(self, device: str) -> None:
            from atlasrag.providers.reranker.base import RerankerModelIdentity

            cfg = runtime_config()
            self.identity = RerankerModelIdentity(
                model_id=cfg.models.reranker_model_id,
                model_revision=cfg.models.reranker_model_revision,
                tokenizer_id=cfg.models.reranker_model_id,
                tokenizer_revision=cfg.models.reranker_model_revision,
                device=device,
                dtype="float16",
                max_input_tokens=8192,
            )

        async def initialize(self, *, allow_download: bool = False) -> None:
            return None

        async def health(self) -> None:
            return None

    class Embedding:
        def __init__(self, device: str) -> None:
            from atlasrag.domain.indexing.models import EmbeddingModelIdentity

            cfg = runtime_config()
            self.identity = EmbeddingModelIdentity(
                model_id=cfg.models.embedding_model_id,
                model_revision=cfg.models.embedding_model_revision,
                tokenizer_id=cfg.models.embedding_tokenizer_id,
                tokenizer_revision=cfg.models.embedding_tokenizer_revision,
                dimension=cfg.part_i.embedding_dimension,
                device=cfg.models.model_device,
            )

        async def initialize(self, *, allow_download: bool = False) -> None:
            return None

        async def health(self) -> None:
            return None

    class RedisStub:
        async def ping(self) -> bool:
            return True

        async def get(self, key: str) -> bytes | None:
            return None

        async def set(self, key: str, value: Any, **_kw: Any) -> bool:
            return True

        async def delete(self, *keys: str) -> int:
            return 0

        async def unlink(self, *keys: str) -> int:
            return 0

        async def eval(self, *_a: Any) -> Any:
            return [b"fence", None]

        async def scan(self, **_kw: Any) -> tuple[int, list[bytes]]:
            return (0, [])

        async def aclose(self) -> None:
            return None

    class HttpStub:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def aclose(self) -> None:
            return None

    class UnitStub:
        def __init__(self, *_a: Any) -> None:
            self.documents = self
            self.runtime_metadata = self

        async def __aenter__(self) -> UnitStub:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def list(self) -> tuple[object, ...]:
            return ()

        async def get(self) -> Any:
            from atlasrag.repositories.postgres.records import RuntimeMetadata

            return RuntimeMetadata(query_cache_invalidation_required=False)

    monkeypatch.setattr(composition_module, "PostgresPool", Pool)
    monkeypatch.setattr(composition_module, "MilvusClient", lambda **k: object())
    monkeypatch.setattr(composition_module, "MilvusIndexProvider", Milvus)
    monkeypatch.setattr(composition_module, "QwenEmbeddingProvider", Embedding)
    monkeypatch.setattr(composition_module, "BgeRerankerProvider", RerankStub)
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: RedisStub())
    monkeypatch.setattr(httpx, "AsyncClient", HttpStub)
    ownership = RecordingOwnership(ownership_log)
    monkeypatch.setattr(composition_module, "RuntimeOwnershipLock", lambda: ownership)
    monkeypatch.setattr(
        production_module, "PostgresUnitOfWork", lambda pool: UnitStub()
    )
    monkeypatch.setattr(
        composition_module, "PostgresUnitOfWork", lambda pool: UnitStub()
    )

    async def fail_verify() -> GlobalSafetyResult:
        raise ValueError("verify exploded")

    async def builder(shared: Any) -> Any:
        bundle = await production_module.build_default_business(shared)
        return composition_module.BusinessBundle(
            retrieval=bundle.retrieval,
            sync=bundle.sync,
            reconcile=bundle.reconcile,
            source_safety=bundle.source_safety,
            verify=fail_verify,
            close=bundle.close,
        )

    Path("/tmp/atlasrag-production").mkdir(parents=True, exist_ok=True)
    with pytest.raises(RuntimeBootstrapFailedError) as exc_info:
        await composition_module.ApplicationComposition(runtime_config()).start(builder)

    assert isinstance(exc_info.value.__cause__, ValueError)
    assert pool_closes == 1
    assert ownership.acquired == 1
    assert ownership.released == 0
    notes = str(getattr(exc_info.value, "__notes__", ()))
    assert "super-secret" not in notes
    assert "pw=secret" not in notes


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["cancel_on_close", "error_on_close"])
async def test_r10_03_f02_open_pool_aborts_retries_on_close_cancel_or_error(
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    open_attempts = 0
    close_started = asyncio.Event()

    class Pool:
        def __init__(self, dsn: str, *, min_size: int, max_size: int) -> None:
            _ = (dsn, min_size, max_size)

        async def open(self) -> None:
            nonlocal open_attempts
            open_attempts += 1
            raise DependencyUnavailableError()

        async def close(self) -> None:
            close_started.set()
            if failure_mode == "cancel_on_close":
                await asyncio.Event().wait()
            raise OSError("close failed")

    monkeypatch.setattr(composition_module, "PostgresPool", Pool)
    monkeypatch.setattr(
        composition_module, "RuntimeOwnershipLock", lambda: RecordingOwnership([])
    )
    cfg = runtime_config()
    cfg = cfg.model_copy(
        update={
            "runtime": cfg.runtime.model_copy(
                update={"startup_attempts": 5, "startup_retry_delay_seconds": 0.01}
            )
        }
    )

    if failure_mode == "cancel_on_close":
        task = asyncio.create_task(
            composition_module.ApplicationComposition(cfg).start()
        )
        await asyncio.wait_for(close_started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
    else:
        with pytest.raises(RuntimeBootstrapFailedError) as exc_info:
            await asyncio.wait_for(
                composition_module.ApplicationComposition(cfg).start(),
                timeout=1.0,
            )
        assert isinstance(exc_info.value.__cause__, DependencyUnavailableError)

    assert open_attempts == 1


# ---------------------------------------------------------------------------
# F-R10-01-02: Cross-epoch health admission with nonzero refresh interval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f_r10_01_02_draining_hard_failure_cannot_admit_after_abort_within_refresh_interval() -> (
    None
):
    postgres = Probe()
    postgres.on_call(_healthy)  # start()
    query_entered = asyncio.Event()
    query_release = asyncio.Event()

    harness = build_harness(
        probes={"postgres": postgres},
        refresh_interval=60.0,
    )
    await harness.start()

    # Keep one query active so maintenance pauses in DRAINING.
    harness.retrieval.hold = True
    harness.retrieval.release = query_release
    harness.retrieval.started = query_entered
    q_active = asyncio.create_task(harness.coordinator.run_query(request("active")))
    await asyncio.wait_for(query_entered.wait(), timeout=1.0)

    drain_task = asyncio.create_task(harness.coordinator.run_maintenance())
    await pump_until(
        lambda: harness.gate.mode is RuntimeMode.DRAINING, label="DRAINING"
    )

    # Hard dependency failure observed during DRAINING -> health snapshot marks hard_healthy=False.
    postgres.fail_call(RuntimeError("postgres down during draining"))
    snap = await harness.coordinator.health()
    assert snap.runtime is RuntimeMode.DRAINING
    assert snap.hard_healthy is False

    # Abort maintenance via drain timeout -> gate restores SERVING & pre-drain _admission_ready=True.
    drain_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(drain_task, timeout=1.0)
    assert harness.gate.mode is RuntimeMode.SERVING
    assert harness.gate.lifecycle_generation > snap.lifecycle_generation

    # Before refresh_interval expires, Postgres is STILL failing -> query MUST BE REJECTED!
    harness.retrieval.hold = False
    postgres.fail_call(RuntimeError("postgres still down"))
    with pytest.raises(RuntimeNotReadyError):
        await asyncio.wait_for(
            harness.coordinator.run_query(request("blocked-during-outage")),
            timeout=1.0,
        )
    assert harness.retrieval.calls == 1

    # Recover Postgres -> explicit health check observes recovery and reopens admission.
    postgres.on_call(_healthy)
    recovered_snap = await harness.coordinator.health()
    assert recovered_snap.hard_healthy is True
    env2 = await harness.coordinator.run_query(request("recovered"))
    assert env2.response.answer_text == "answer"
    assert harness.retrieval.calls == 2

    query_release.set()
    await asyncio.wait_for(q_active, timeout=1.0)
    await harness.coordinator.shutdown()


@pytest.mark.asyncio
async def test_f_r10_01_02_in_flight_unhealthy_probe_across_abort_and_blocked_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr("atlasrag.runtime.health.monotonic", lambda: now[0])
    postgres = Probe()
    postgres.on_call(_healthy)  # start
    probe_started = asyncio.Event()
    probe_release = asyncio.Event()

    async def slow_failing_probe() -> None:
        probe_started.set()
        await probe_release.wait()
        raise RuntimeError("old-epoch hard failure")

    postgres.on_call(slow_failing_probe)
    harness = build_harness(probes={"postgres": postgres}, refresh_interval=60.0)
    await harness.start()

    # Expire refresh_interval so query probe runs in epoch 1; while probing, maintenance drains & aborts to epoch 3.
    now[0] += 61.0
    stale_q = asyncio.create_task(
        harness.coordinator.run_query(request("stale-unhealthy"))
    )
    await asyncio.wait_for(probe_started.wait(), timeout=1.0)

    lease = await harness.gate.begin_maintenance()
    await lease.abort()
    assert harness.gate.mode is RuntimeMode.SERVING

    # Postgres remains unhealthy during the bounded current-epoch retry -> query MUST BE REJECTED!
    postgres.default_error = RuntimeError("still down")
    probe_release.set()
    with pytest.raises(RuntimeNotReadyError):
        await asyncio.wait_for(stale_q, timeout=1.0)
    assert harness.retrieval.calls == 0

    # Recover Postgres -> after refresh interval, healthy query succeeds; healthy abort retains serving.
    postgres.default_error = None
    now[0] += 61.0
    assert (
        await harness.coordinator.run_query(request("healthy-again"))
    ).response.answer_text == "answer"
    lease_ok = await harness.gate.begin_maintenance()
    await lease_ok.abort()
    assert (
        await harness.coordinator.run_query(request("after-healthy-abort"))
    ).response.answer_text == "answer"

    # BLOCKED and SHUTTING_DOWN remain strictly closed.
    await harness.gate.mark_blocked()
    with pytest.raises(RuntimeNotReadyError):
        await harness.coordinator.run_query(request("while-blocked"))
    await harness.coordinator.shutdown()
    with pytest.raises(RuntimeShuttingDownError):
        await harness.coordinator.run_query(request("after-shutdown"))


# ---------------------------------------------------------------------------
# F-R10-01-03: Shared ResourceGuard shutdown wiring in composed runtime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f_r10_01_03_composed_runtime_wires_resource_guard_graceful_and_cancel_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class GuardPool:
        def __init__(self, dsn: str, *, min_size: int, max_size: int) -> None:
            _ = (dsn, min_size, max_size)

        async def open(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def health(self) -> None:
            return None

    class GuardMilvus:
        def __init__(self, *a: Any, **k: Any) -> None:
            self.embedding_dimension = runtime_config().part_i.embedding_dimension
            self.analyzer = runtime_config().part_i.bm25_analyzer

        async def initialize(self) -> None:
            return None

        async def health(self) -> None:
            return None

        async def close(self) -> None:
            return None

    class FakeEmbed:
        def __init__(self, device: str) -> None:
            from atlasrag.domain.indexing.models import EmbeddingModelIdentity

            cfg = runtime_config()
            self.identity = EmbeddingModelIdentity(
                model_id=cfg.models.embedding_model_id,
                model_revision=cfg.models.embedding_model_revision,
                tokenizer_id=cfg.models.embedding_tokenizer_id,
                tokenizer_revision=cfg.models.embedding_tokenizer_revision,
                dimension=cfg.part_i.embedding_dimension,
                device=cfg.models.model_device,
            )
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.block_query = False
            self.query_calls = 0

        async def initialize(self, *, allow_download: bool = False) -> None:
            return None

        async def health(self) -> None:
            return None

        async def embed(self, texts: Any) -> tuple[tuple[float, ...], ...]:
            return tuple((1.0,) * 1024 for _ in texts)

        async def embed_query(self, query: str) -> tuple[float, ...]:
            self.query_calls += 1
            if self.block_query:
                self.entered.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    raise
            return (1.0,) * 1024

    class FakeRerank:
        def __init__(self, device: str) -> None:
            from atlasrag.providers.reranker.base import RerankerModelIdentity

            cfg = runtime_config()
            self.identity = RerankerModelIdentity(
                model_id=cfg.models.reranker_model_id,
                model_revision=cfg.models.reranker_model_revision,
                tokenizer_id=cfg.models.reranker_model_id,
                tokenizer_revision=cfg.models.reranker_model_revision,
                device=cfg.models.model_device,
                dtype="float16",
                max_input_tokens=8192,
            )
            self.calls = 0

        async def initialize(self, *, allow_download: bool = False) -> None:
            return None

        async def health(self) -> None:
            return None

        async def score(self, query: str, passages: Any) -> tuple[float, ...]:
            self.calls += 1
            return tuple(0.9 for _ in passages)

    class FakeRedis:
        async def ping(self) -> bool:
            return True

        async def get(self, key: str) -> bytes | None:
            return None

        async def set(self, key: str, value: Any, **_kw: Any) -> bool:
            return True

        async def delete(self, *keys: str) -> int:
            return 0

        async def eval(self, *_a: Any) -> Any:
            return [b"fence", None]

        async def scan(self, **_kw: Any) -> tuple[int, list[bytes]]:
            return (0, [])

        async def aclose(self) -> None:
            return None

    class FakeHttp:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def aclose(self) -> None:
            return None

    class Unit:
        def __init__(self, *_a: Any) -> None:
            self.documents = self
            self.runtime_metadata = self

        async def __aenter__(self) -> Unit:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def list(self) -> tuple[object, ...]:
            return ()

        async def get(self) -> Any:
            from atlasrag.repositories.postgres.records import RuntimeMetadata

            return RuntimeMetadata(query_cache_invalidation_required=False)

    created_shared: list[composition_module.SharedResources] = []

    def patch_composition(
        monkey: pytest.MonkeyPatch, *, shutdown_timeout: float
    ) -> RuntimeConfig:
        monkey.setattr(composition_module, "PostgresPool", GuardPool)
        monkey.setattr(composition_module, "MilvusClient", lambda **k: object())
        monkey.setattr(composition_module, "MilvusIndexProvider", GuardMilvus)
        monkey.setattr(composition_module, "QwenEmbeddingProvider", FakeEmbed)
        monkey.setattr(composition_module, "BgeRerankerProvider", FakeRerank)
        monkey.setattr(redis, "from_url", lambda *a, **k: FakeRedis())
        monkey.setattr(httpx, "AsyncClient", FakeHttp)
        monkey.setattr(
            composition_module, "RuntimeOwnershipLock", lambda: RecordingOwnership([])
        )
        monkey.setattr(production_module, "PostgresUnitOfWork", lambda pool: Unit())
        monkey.setattr(composition_module, "PostgresUnitOfWork", lambda pool: Unit())
        cfg = runtime_config()
        return cfg.model_copy(
            update={
                "maintenance": cfg.maintenance.model_copy(
                    update={"shutdown_timeout_seconds": shutdown_timeout}
                )
            }
        )

    # Scenario A: Admitted multi-op query (embedding -> reranker) finishes inside grace window.
    created_shared.clear()
    cfg_grace = patch_composition(monkeypatch, shutdown_timeout=1.0)
    gate_first = asyncio.Event()
    release_first = asyncio.Event()

    async def builder_grace(
        shared: composition_module.SharedResources,
    ) -> composition_module.BusinessBundle:
        created_shared.append(shared)
        assert shared.reranker is not None
        assert shared.embedding._guard is shared.guard
        assert shared.reranker._guard is shared.guard
        assert shared.guarded_mineru._guard is shared.guard

        async def retrieval(req: UserTurnRequest) -> FinalResponse:
            gate_first.set()
            await release_first.wait()
            await shared.embedding.embed_query(req.query)
            assert shared.reranker is not None
            await shared.reranker.score(req.query, ["p1"])
            return FinalResponse(answer_text="graceful multi-op", citations=())

        async def sync_ok() -> Any:
            raise AssertionError

        async def rec() -> ReconciliationResult:
            return ReconciliationResult(
                outcome=ReconciliationOutcome.CLEAN, checked_documents=0
            )

        async def ss() -> SourceSafetyResult:
            return SourceSafetyResult(safe=True)

        async def ver() -> GlobalSafetyResult:
            return GlobalSafetyResult(serving_safety=ServingSafety.SAFE_TO_SERVE)

        async def close_noop() -> None:
            return None

        return composition_module.BusinessBundle(
            retrieval=retrieval,
            sync=sync_ok,
            reconcile=rec,
            source_safety=ss,
            verify=ver,
            close=close_noop,
        )

    Path("/tmp/atlasrag-production").mkdir(parents=True, exist_ok=True)
    comp_a = composition_module.ApplicationComposition(cfg_grace)
    coord_a = await comp_a.start(builder_grace)
    q_grace = asyncio.create_task(coord_a.run_query(request("multi-op")))
    await asyncio.wait_for(gate_first.wait(), timeout=1.0)
    shut_grace = asyncio.create_task(coord_a.shutdown())
    await pump_until(lambda: coord_a.tasks.closed, label="tasks closed")
    release_first.set()
    res_a = await asyncio.wait_for(q_grace, timeout=1.0)
    assert res_a.response.answer_text == "graceful multi-op"
    await asyncio.wait_for(shut_grace, timeout=1.0)

    # After shutdown, guard rejects new operations across embedding, reranker, AND guarded_mineru.
    shared_a = created_shared[0]
    with pytest.raises(RuntimeShuttingDownError):
        await shared_a.embedding.embed_query("late")
    assert shared_a.reranker is not None
    with pytest.raises(RuntimeShuttingDownError):
        await shared_a.reranker.score("late", ["p"])
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4\n")
    with pytest.raises(RuntimeShuttingDownError):
        await shared_a.guarded_mineru.parse(
            SourceFile(
                knowledge_directory=tmp_path,
                relative_source_path="a.pdf",
                file_name="a.pdf",
                source_type=SourceType.PDF,
                absolute_path=tmp_path / "a.pdf",
            ),
            object(),  # type: ignore[arg-type]
        )

    # Scenario B: Queued operation waiting on guard.slot() is rejected at shutdown cancellation boundary!
    created_shared.clear()
    cfg_timeout = patch_composition(monkeypatch, shutdown_timeout=0.05)
    holder_entered = asyncio.Event()
    holder_release = asyncio.Event()
    queued_ran: list[str] = []

    async def builder_timeout(
        shared: composition_module.SharedResources,
    ) -> composition_module.BusinessBundle:
        created_shared.append(shared)

        async def retrieval(req: UserTurnRequest) -> FinalResponse:
            async with shared.guard.slot():
                holder_entered.set()
                try:
                    await holder_release.wait()
                except asyncio.CancelledError:
                    await asyncio.sleep(0)
                    raise
            return FinalResponse(answer_text="holder", citations=())

        async def _unused() -> Any:
            raise AssertionError

        return composition_module.BusinessBundle(
            retrieval=retrieval,
            sync=_unused,
            reconcile=lambda: _ok_rec(),
            source_safety=lambda: _ok_ss(),
            verify=lambda: _ok_ver(),
            close=lambda: _noop_async(),
        )

    async def _ok_rec() -> ReconciliationResult:
        return ReconciliationResult(
            outcome=ReconciliationOutcome.CLEAN, checked_documents=0
        )

    async def _ok_ss() -> SourceSafetyResult:
        return SourceSafetyResult(safe=True)

    async def _ok_ver() -> GlobalSafetyResult:
        return GlobalSafetyResult(serving_safety=ServingSafety.SAFE_TO_SERVE)

    async def _noop_async() -> None:
        return None

    comp_b = composition_module.ApplicationComposition(cfg_timeout)
    coord_b = await comp_b.start(builder_timeout)
    holder_task = asyncio.create_task(coord_b.run_query(request("holder")))
    await asyncio.wait_for(holder_entered.wait(), timeout=1.0)

    async def queued_op() -> None:
        assert created_shared[0].reranker is not None
        await created_shared[0].reranker.score("queued", ["x"])
        queued_ran.append("ran")

    queued_task = asyncio.create_task(queued_op())
    await asyncio.sleep(0)
    await coord_b.shutdown()
    holder_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(holder_task, timeout=1.0)
    with pytest.raises(RuntimeShuttingDownError):
        await asyncio.wait_for(queued_task, timeout=1.0)
    assert queued_ran == []


@pytest.mark.asyncio
async def test_f_r10_01_03_unsettled_worker_or_close_failure_closes_guard_and_retains_ownership() -> (
    None
):
    guard = ResourceGuard(capacity=1)
    harness = build_harness(shutdown_timeout=0.01)
    # Attach real begin_guard_shutdown via CoordinatorDependencies replacement.
    deps = CoordinatorDependencies(
        ownership=harness.coordinator._deps.ownership,
        gate=harness.gate,
        tasks=harness.tasks,
        health=harness.health,
        freshness=harness.freshness,  # type: ignore[arg-type]
        maintenance=harness.coordinator._deps.maintenance,
        retrieval=harness.retrieval,
        reconcile=harness.coordinator._deps.reconcile,
        source_safety=harness.coordinator._deps.source_safety,
        verify=harness.coordinator._deps.verify,
        close_resources=harness.close,
        begin_guard_shutdown=guard.begin_shutdown,
        shutdown_timeout_seconds=0.01,
        ownership_acquired=True,
    )
    coord = ApplicationCoordinator(deps)
    await coord._start()

    # Unsettled worker: ignore cancellation cleanly only after release.
    unsettled_started = asyncio.Event()
    unsettled_release = asyncio.Event()
    orig_retrieval = harness.retrieval

    async def stubborn(_req: UserTurnRequest) -> FinalResponse:
        async with guard.slot():
            unsettled_started.set()
            while not unsettled_release.is_set():
                try:
                    await unsettled_release.wait()
                except asyncio.CancelledError:
                    pass
        return FinalResponse(answer_text="late", citations=())

    deps.retrieval = stubborn
    q_stubborn = asyncio.create_task(coord.run_query(request("stubborn")))
    await asyncio.wait_for(unsettled_started.wait(), timeout=1.0)

    with pytest.raises(RuntimeShutdownFailedError):
        await coord.shutdown()
    assert harness.close.calls == 0
    assert harness.ownership.released == 0
    with pytest.raises(RuntimeShuttingDownError):
        async with guard.slot():
            pass

    # Let worker finish, then repeated shutdown runs close_resources; test close failure retains ownership.
    unsettled_release.set()
    await asyncio.wait_for(q_stubborn, timeout=1.0)
    harness.close.error = OSError("close failed")
    with pytest.raises(OSError):
        await coord.shutdown()
    assert harness.ownership.released == 0
    _ = orig_retrieval
