"""Real PG/Milvus/Redis Stage 10-B lifecycle, retrieval, concurrency and health."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from pymilvus import MilvusClient  # type: ignore[import-untyped]
from redis.asyncio import Redis

from atlasrag.config.fingerprint import runtime_fingerprint
from atlasrag.domain.errors import (
    DependencyUnavailableError,
    MaintenanceDrainTimeoutError,
    RuntimeNotReadyError,
    RuntimeOwnershipConflictError,
    RuntimeShuttingDownError,
)
from atlasrag.domain.ingestion.chunks import ChunkType
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalLocalCitation
from atlasrag.providers.index.schema import COLLECTION_NAME
from atlasrag.repositories.postgres.pool import PostgresPool
from atlasrag.repositories.postgres.records import IngestionStatus
from atlasrag.repositories.postgres.uow import PostgresUnitOfWork
from atlasrag.runtime import health as health_module
from atlasrag.runtime.composition import ApplicationComposition
from atlasrag.runtime.coordinator import ApplicationCoordinator
from atlasrag.runtime.health import DependencyHealth, HealthSnapshot
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.services.sync.models import ServingSafety, SyncOutcome
from integration.stage10_helpers import (
    FakeBgeReranker,
    FakeQwenEmbedding,
    HttpMockState,
    TcpFaultProxy,
    build_stage10_runtime_config,
    parse_stage10_endpoints,
    patch_runtime_boundaries,
    stage10_isolation_guard,
)

pytestmark = pytest.mark.integration

GUIDE_V1 = (
    "# AtlasRAG Runtime Guide\n\n"
    "## Deterministic Architecture\n\n"
    "AtlasRAG separates zephyrite-token-alpha orchestration from obsidian-lattice "
    "storage invariants so query caching remains strictly disposable.\n\n"
    "| Component | Policy |\n"
    "| --- | --- |\n"
    "| query_cache | disposable |\n"
)

GUIDE_V2 = (
    "# AtlasRAG Runtime Guide\n\n"
    "## Deterministic Architecture\n\n"
    "Revision two introduces solstice-marker-v2 verification across the runtime "
    "after maintenance commits.\n\n"
    "| Component | Policy |\n"
    "| --- | --- |\n"
    "| query_cache | disposable |\n"
)

NOTES_DOC = (
    "# Companion Notes\n\n"
    "Secondary operational notes cover lock hygiene and shutdown ordering.\n"
)


class _BlockingEmbedding(FakeQwenEmbedding):
    def __init__(self, *, entered: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__()
        self._entered = entered
        self._release = release
        self.arm_block = False

    async def embed_query(self, query: str) -> tuple[float, ...]:
        if self.arm_block:
            self._entered.set()
            await self._release.wait()
        return await super().embed_query(query)


async def _wait_until_draining(coordinator: Any) -> None:
    for _ in range(500):
        if coordinator.gate.mode is RuntimeMode.DRAINING:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Expected DRAINING mode, observed {coordinator.gate.mode}")


@pytest.mark.asyncio
async def test_b10_03_empty_kb_startup_ownership_and_startup_retry(
    postgres_dsn: str,
    milvus_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, (redis_host, redis_port), (milvus_host, milvus_port) = parse_stage10_endpoints()
    knowledge_dir = tmp_path / "kb"
    knowledge_dir.mkdir()
    lock_file = tmp_path / "run" / "atlasrag.lock"
    http_state = HttpMockState()

    async with stage10_isolation_guard(
        redis_host=redis_host, redis_port=redis_port, milvus_uri=milvus_uri
    ):
        fail_always = FakeQwenEmbedding(fail_init=lambda: DependencyUnavailableError())
        patch_runtime_boundaries(
            monkeypatch,
            lock_file=lock_file,
            embedding_factory=lambda **_: fail_always,
            reranker_factory=lambda **_: FakeBgeReranker(),
            http_state=http_state,
        )
        cfg_fail = build_stage10_runtime_config(
            postgres_dsn=postgres_dsn,
            knowledge_directory=knowledge_dir,
            redis_host=redis_host,
            redis_port=redis_port,
            milvus_host=milvus_host,
            milvus_port=milvus_port,
            runtime_overrides={
                "startup_attempts": 2,
                "startup_retry_delay_seconds": 0.02,
            },
        )
        with pytest.raises(DependencyUnavailableError):
            await ApplicationComposition(cfg_fail).start()
        assert fail_always.init_calls == 2

        attempts = 0

        def _transient_failure() -> BaseException | None:
            nonlocal attempts
            attempts += 1
            return DependencyUnavailableError() if attempts == 1 else None

        retrying_embedding = FakeQwenEmbedding(fail_init=_transient_failure)
        reranker = FakeBgeReranker()
        patch_runtime_boundaries(
            monkeypatch,
            lock_file=lock_file,
            embedding_factory=lambda **_: retrying_embedding,
            reranker_factory=lambda **_: reranker,
            http_state=http_state,
        )
        config = build_stage10_runtime_config(
            postgres_dsn=postgres_dsn,
            knowledge_directory=knowledge_dir,
            redis_host=redis_host,
            redis_port=redis_port,
            milvus_host=milvus_host,
            milvus_port=milvus_port,
            runtime_overrides={
                "startup_attempts": 2,
                "startup_retry_delay_seconds": 0.02,
            },
        )
        composition = ApplicationComposition(config)
        coordinator = await composition.start()
        try:
            assert retrying_embedding.init_calls == 2
            assert reranker.init_calls == 1
            assert coordinator.gate.mode is RuntimeMode.SERVING
            assert await coordinator.readiness() is True

            snap = await coordinator.health()
            assert snap.runtime is RuntimeMode.SERVING
            assert snap.hard_healthy is True
            assert snap.knowledge_verified is True
            assert snap.postgres is DependencyHealth.HEALTHY
            assert snap.milvus is DependencyHealth.HEALTHY
            assert snap.redis is DependencyHealth.HEALTHY
            assert snap.searxng is DependencyHealth.HEALTHY
            assert snap.embedding is DependencyHealth.HEALTHY
            assert snap.reranker is DependencyHealth.HEALTHY

            with pytest.raises(RuntimeOwnershipConflictError):
                await ApplicationComposition(config).start()

            # Empty startup must not fabricate or ingest documents automatically.
            assert composition._pool is not None
            async with PostgresUnitOfWork(composition._pool) as unit:
                assert await unit.documents.list() == ()

            milvus_client = MilvusClient(uri=milvus_uri, timeout=10.0)
            try:
                assert milvus_client.has_collection(COLLECTION_NAME) is True
                stats = milvus_client.get_collection_stats(COLLECTION_NAME)
                assert int(stats["row_count"]) == 0
            finally:
                milvus_client.close()

            empty_env = await coordinator.run_query(
                UserTurnRequest(session_id="s-empty", query="anything")
            )
            assert len(empty_env.trace_id) == 32
            assert coordinator.gate.active_queries == 0
        finally:
            await coordinator.shutdown()

        # Lock release verified by starting a second composition cleanly on same path.
        patch_runtime_boundaries(
            monkeypatch,
            lock_file=lock_file,
            embedding_factory=lambda **_: FakeQwenEmbedding(),
            reranker_factory=lambda **_: FakeBgeReranker(),
            http_state=http_state,
        )
        restarted = await ApplicationComposition(config).start()
        try:
            assert await restarted.readiness() is True
        finally:
            await restarted.shutdown()


@pytest.mark.asyncio
async def test_b10_04_b10_05_real_maintenance_publish_hybrid_retrieval_and_query_cache(
    postgres_dsn: str,
    milvus_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, (redis_host, redis_port), (milvus_host, milvus_port) = parse_stage10_endpoints()
    knowledge_dir = tmp_path / "kb"
    knowledge_dir.mkdir()
    lock_file = tmp_path / "run" / "atlasrag.lock"
    embedding = FakeQwenEmbedding()
    reranker = FakeBgeReranker()
    http_state = HttpMockState(answer_text="Grounded zephyrite summary [L1].")
    patch_runtime_boundaries(
        monkeypatch,
        lock_file=lock_file,
        embedding_factory=lambda **_: embedding,
        reranker_factory=lambda **_: reranker,
        http_state=http_state,
    )

    config_r1 = build_stage10_runtime_config(
        postgres_dsn=postgres_dsn,
        knowledge_directory=knowledge_dir,
        redis_host=redis_host,
        redis_port=redis_port,
        milvus_host=milvus_host,
        milvus_port=milvus_port,
    )
    fp_r1 = runtime_fingerprint(config_r1)

    async with stage10_isolation_guard(
        redis_host=redis_host, redis_port=redis_port, milvus_uri=milvus_uri
    ):
        coordinator = await ApplicationComposition(config_r1).start()
        guide_v1_rev = ""
        try:
            (knowledge_dir / "guide.md").write_text(GUIDE_V1, encoding="utf-8")
            (knowledge_dir / "notes.md").write_text(NOTES_DOC, encoding="utf-8")
            sync_v1 = await coordinator.run_maintenance()
            assert sync_v1.outcome is SyncOutcome.SUCCESS
            assert sync_v1.serving_safety is ServingSafety.SAFE_TO_SERVE
            assert sync_v1.ready_builds == 2
            assert await coordinator.readiness() is True

            pool = PostgresPool(postgres_dsn, min_size=1, max_size=4)
            await pool.open()
            try:
                async with PostgresUnitOfWork(pool) as unit:
                    docs = await unit.documents.list()
                    assert len(docs) == 2
                    by_path = {doc.relative_source_path: doc for doc in docs}
                    assert set(by_path) == {"guide.md", "notes.md"}
                    guide_doc = by_path["guide.md"]
                    assert guide_doc.ingestion_status is IngestionStatus.READY
                    assert guide_doc.current_revision_id is not None
                    guide_v1_rev = guide_doc.current_revision_id
                    chunks = await unit.chunks.list_for_revision(
                        guide_doc.document_id, guide_v1_rev
                    )
                    types = {c.chunk_type for c in chunks}
                    assert ChunkType.TEXT_PARENT.value in {t.value for t in types}
                    assert ChunkType.TEXT_CHILD.value in {t.value for t in types}
                    assert ChunkType.TABLE.value in {t.value for t in types}

                milvus_client = MilvusClient(uri=milvus_uri, timeout=10.0)
                try:
                    rows = milvus_client.query(
                        collection_name=COLLECTION_NAME,
                        filter=f'document_id == "{guide_doc.document_id}"',
                        output_fields=["chunk_type", "revision_id"],
                    )
                    projected_types = {str(row["chunk_type"]) for row in rows}
                    assert ChunkType.TEXT_PARENT.value not in projected_types
                    assert {
                        ChunkType.TEXT_CHILD.value,
                        ChunkType.TABLE.value,
                    } <= projected_types
                finally:
                    milvus_client.close()
            finally:
                await pool.close()

            q_text = "Explain zephyrite-token-alpha architecture"
            env1 = await coordinator.run_query(
                UserTurnRequest(session_id="s1", query=q_text)
            )
            local_cits_1 = [
                c for c in env1.response.citations if isinstance(c, FinalLocalCitation)
            ]
            assert local_cits_1
            assert local_cits_1[0].relative_source_path == "guide.md"
            assert any("zephyrite-token-alpha" in p for p in http_state.prompts)
            assert embedding.query_calls == 1
            assert reranker.score_calls == 1
            assert http_state.deepseek_rewrite_calls == 1
            assert http_state.deepseek_generation_calls == 1

            # Cache HIT on identical session + query + runtime_fingerprint
            env2 = await coordinator.run_query(
                UserTurnRequest(session_id="s1", query=q_text)
            )
            assert env2.response == env1.response
            assert env2.trace_id != env1.trace_id
            assert coordinator.gate.active_queries == 0
            assert embedding.query_calls == 1
            assert reranker.score_calls == 1
            assert http_state.deepseek_rewrite_calls == 1
            assert http_state.deepseek_generation_calls == 1

            # Session isolation: different session misses and creates its own Redis key
            env_s2 = await coordinator.run_query(
                UserTurnRequest(session_id="s2", query=q_text)
            )
            assert env_s2.trace_id != env1.trace_id
            assert embedding.query_calls == 2
            assert reranker.score_calls == 2
            assert http_state.deepseek_generation_calls == 2

            redis_raw = Redis.from_url(
                f"redis://{redis_host}:{redis_port}/0", decode_responses=False
            )
            try:
                raw_keys = [
                    k.decode("utf-8")
                    async for k in redis_raw.scan_iter(match="atlasrag:query:*")
                ]
                assert any(
                    k.startswith(f"atlasrag:query:s1:{fp_r1}:") for k in raw_keys
                )
                assert any(
                    k.startswith(f"atlasrag:query:s2:{fp_r1}:") for k in raw_keys
                )
            finally:
                await redis_raw.aclose()

            # Document update -> v2 rebuilds, updates PG/Milvus, invalidates v1 query cache
            http_state.answer_text = "Updated solstice-marker-v2 answer [L1]."
            (knowledge_dir / "guide.md").write_text(GUIDE_V2, encoding="utf-8")
            sync_v2 = await coordinator.run_maintenance()
            assert sync_v2.outcome is SyncOutcome.SUCCESS
            assert sync_v2.ready_builds == 1

            env_v2 = await coordinator.run_query(
                UserTurnRequest(
                    session_id="s1",
                    query="Explain solstice-marker-v2 verification",
                )
            )
            assert "solstice-marker-v2" in env_v2.response.answer_text
            local_cits_2 = [
                c
                for c in env_v2.response.citations
                if isinstance(c, FinalLocalCitation)
            ]
            assert local_cits_2
            assert local_cits_2[0].relative_source_path == "guide.md"
            pool2 = PostgresPool(postgres_dsn, min_size=1, max_size=4)
            await pool2.open()
            try:
                async with PostgresUnitOfWork(pool2) as unit:
                    docs2 = {
                        d.relative_source_path: d for d in await unit.documents.list()
                    }
                    assert docs2["guide.md"].current_revision_id != guide_v1_rev
            finally:
                await pool2.close()
        finally:
            await coordinator.shutdown()

        # Different runtime_fingerprint (same Part I pipeline_fingerprint) must miss Redis cache.
        config_r2 = build_stage10_runtime_config(
            postgres_dsn=postgres_dsn,
            knowledge_directory=knowledge_dir,
            redis_host=redis_host,
            redis_port=redis_port,
            milvus_host=milvus_host,
            milvus_port=milvus_port,
        ).model_copy(
            update={
                "models": config_r1.models.model_copy(
                    update={"query_model_revision": "query-r2"}
                )
            }
        )
        fp_r2 = runtime_fingerprint(config_r2)
        assert fp_r2 != fp_r1

        coord_r2 = await ApplicationComposition(config_r2).start()
        try:
            before_gen = http_state.deepseek_generation_calls
            await coord_r2.run_query(
                UserTurnRequest(
                    session_id="s1",
                    query="Explain solstice-marker-v2 verification",
                )
            )
            assert http_state.deepseek_generation_calls == before_gen + 1
        finally:
            await coord_r2.shutdown()


@pytest.mark.asyncio
async def test_b10_06_concurrency_drain_and_maintenance_exclusion(
    postgres_dsn: str,
    milvus_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, (redis_host, redis_port), (milvus_host, milvus_port) = parse_stage10_endpoints()
    knowledge_dir = tmp_path / "kb"
    knowledge_dir.mkdir()
    (knowledge_dir / "guide.md").write_text(GUIDE_V1, encoding="utf-8")
    lock_file = tmp_path / "run" / "atlasrag.lock"

    entered = asyncio.Event()
    release = asyncio.Event()
    embedding = _BlockingEmbedding(entered=entered, release=release)
    http_state = HttpMockState()
    patch_runtime_boundaries(
        monkeypatch,
        lock_file=lock_file,
        embedding_factory=lambda **_: embedding,
        reranker_factory=lambda **_: FakeBgeReranker(),
        http_state=http_state,
    )

    config = build_stage10_runtime_config(
        postgres_dsn=postgres_dsn,
        knowledge_directory=knowledge_dir,
        redis_host=redis_host,
        redis_port=redis_port,
        milvus_host=milvus_host,
        milvus_port=milvus_port,
        runtime_overrides={"postgres_pool_max_size": 4},
        maintenance_overrides={
            "max_document_concurrency": 1,
            "drain_timeout_seconds": 0.15,
        },
    )

    async with stage10_isolation_guard(
        redis_host=redis_host, redis_port=redis_port, milvus_uri=milvus_uri
    ):
        comp = ApplicationComposition(config)
        coordinator = await comp.start()
        try:
            sync_res = await coordinator.run_maintenance()
            assert sync_res.ready_builds == 1

            # Concurrent queries exceed postgres_pool_max_size=2 to detect any pool leak.
            results = await asyncio.gather(
                *(
                    coordinator.run_query(
                        UserTurnRequest(
                            session_id=f"conc-{i}",
                            query=f"Explain zephyrite-token-alpha {i}",
                        )
                    )
                    for i in range(6)
                )
            )
            assert len({item.trace_id for item in results}) == 6
            assert coordinator.gate.active_queries == 0
            assert comp._pool is not None
            stats = comp._pool._pool.get_stats()
            assert stats.get("requests_waiting", 0) == 0

            # Active query held while maintenance enters DRAINING.
            embedding.arm_block = True
            entered.clear()
            release.clear()
            q_task = asyncio.create_task(
                coordinator.run_query(
                    UserTurnRequest(session_id="drain-a", query="zephyrite query A")
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=5.0)
            assert coordinator.gate.active_queries == 1

            maint_task = asyncio.create_task(coordinator.run_maintenance())
            await _wait_until_draining(coordinator)
            with pytest.raises(RuntimeNotReadyError):
                await coordinator.run_query(
                    UserTurnRequest(session_id="rejected", query="during draining")
                )
            with pytest.raises(RuntimeNotReadyError):
                await coordinator.run_maintenance()

            # Let timeout path trigger on one attempt, preserving valid active query.
            with pytest.raises(MaintenanceDrainTimeoutError):
                await maint_task
            assert coordinator.gate.mode is RuntimeMode.SERVING

            release.set()
            env_a = await q_task
            assert env_a.trace_id
            assert coordinator.gate.active_queries == 0

            # Now drain completes cleanly once active query is allowed to settle.
            entered.clear()
            release.clear()
            q_task2 = asyncio.create_task(
                coordinator.run_query(
                    UserTurnRequest(session_id="drain-b", query="zephyrite query B")
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=5.0)
            maint_task2 = asyncio.create_task(coordinator.run_maintenance())
            await _wait_until_draining(coordinator)
            release.set()
            env_b, sync_after = await asyncio.gather(q_task2, maint_task2)
            assert env_b.trace_id
            assert sync_after.outcome is SyncOutcome.SUCCESS
            assert await coordinator.readiness() is True
            assert coordinator.gate.active_queries == 0
        finally:
            embedding.arm_block = False
            release.set()
            await coordinator.shutdown()


async def _wait_for_dependency_health(
    coordinator: ApplicationCoordinator,
    field_name: str,
    *,
    expected: DependencyHealth = DependencyHealth.HEALTHY,
    timeout_seconds: float = 15.0,
    interval_seconds: float = 0.25,
) -> HealthSnapshot:
    """Poll coordinator health until a dependency recovers past transport backoff."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        snapshot = await coordinator.health()
        if getattr(snapshot, field_name) is expected:
            return snapshot
        if loop.time() >= deadline:
            return snapshot
        await asyncio.sleep(interval_seconds)


@pytest.mark.asyncio
async def test_b10_12_live_dependency_health_degradation_and_shutdown(
    postgres_dsn: str,
    milvus_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        (pg_host, pg_port, _pg_db, pg_user, pg_pw),
        (redis_host, redis_port),
        (milvus_host, milvus_port),
    ) = parse_stage10_endpoints()

    pg_proxy = TcpFaultProxy(pg_host, pg_port)
    redis_proxy = TcpFaultProxy(redis_host, redis_port)
    milvus_proxy = TcpFaultProxy(milvus_host, milvus_port)
    await asyncio.gather(pg_proxy.start(), redis_proxy.start(), milvus_proxy.start())

    knowledge_dir = tmp_path / "kb"
    knowledge_dir.mkdir()
    (knowledge_dir / "guide.md").write_text(GUIDE_V1, encoding="utf-8")
    lock_file = tmp_path / "run" / "atlasrag.lock"
    http_state = HttpMockState()

    test_pg_db = urlsplit(postgres_dsn).path.lstrip("/")
    proxied_pg_dsn = (
        f"postgresql://{pg_user}:{pg_pw}@127.0.0.1:{pg_proxy.port}/{test_pg_db}"
    )

    async with stage10_isolation_guard(
        redis_host=redis_host, redis_port=redis_port, milvus_uri=milvus_uri
    ):
        try:
            # 1) Reranker unavailability -> soft degrade (still healthy + SERVING).
            patch_runtime_boundaries(
                monkeypatch,
                lock_file=lock_file,
                embedding_factory=lambda **_: FakeQwenEmbedding(),
                reranker_factory=lambda **_: FakeBgeReranker(
                    fail_init=DependencyUnavailableError()
                ),
                http_state=http_state,
            )
            now_health = 100.0
            monkeypatch.setattr(health_module, "monotonic", lambda: now_health)
            cfg_no_rerank = build_stage10_runtime_config(
                postgres_dsn=proxied_pg_dsn,
                knowledge_directory=knowledge_dir,
                redis_host="127.0.0.1",
                redis_port=redis_proxy.port,
                milvus_host="127.0.0.1",
                milvus_port=milvus_proxy.port,
                runtime_overrides={
                    "health_probe_timeout_seconds": 2.0,
                    "health_refresh_interval_seconds": 1.0,
                },
            )
            coord = await ApplicationComposition(cfg_no_rerank).start()
            try:
                assert (await coord.run_maintenance()).ready_builds == 1
                snap = await coord.health()
                assert snap.reranker is DependencyHealth.UNAVAILABLE
                assert snap.hard_healthy is True
                assert await coord.readiness() is True
                assert (
                    await coord.run_query(
                        UserTurnRequest(
                            session_id="degrade", query="zephyrite-token-alpha"
                        )
                    )
                ).trace_id

                # 2) Redis outage -> soft dependency failure only.
                redis_proxy.mode = "refuse"
                await redis_proxy.drop_connections()
                snap = await coord.health()
                assert snap.redis is DependencyHealth.UNAVAILABLE
                assert snap.postgres is DependencyHealth.HEALTHY
                assert snap.milvus is DependencyHealth.HEALTHY
                assert snap.hard_healthy is True
                assert await coord.readiness() is True
                assert (
                    await coord.run_query(
                        UserTurnRequest(
                            session_id="redis-down", query="zephyrite-token-alpha"
                        )
                    )
                ).trace_id
                redis_proxy.mode = "passthrough"
                recovered_redis = await _wait_for_dependency_health(coord, "redis")
                assert recovered_redis.redis is DependencyHealth.HEALTHY

                # 3) SearXNG outage -> soft dependency failure only.
                http_state.searxng_status = 503
                snap = await coord.health()
                assert snap.searxng is DependencyHealth.UNAVAILABLE
                assert snap.hard_healthy is True
                http_state.searxng_status = 200
                assert (await coord.health()).searxng is DependencyHealth.HEALTHY

                # 4) Milvus outage -> hard_healthy False + readiness False + query rejected.
                milvus_proxy.mode = "refuse"
                await milvus_proxy.drop_connections()
                snap = await coord.health()
                assert snap.milvus is DependencyHealth.UNAVAILABLE
                assert snap.hard_healthy is False
                assert await coord.readiness() is False
                now_health += 2.0
                with pytest.raises(RuntimeNotReadyError):
                    await coord.run_query(
                        UserTurnRequest(session_id="milvus-down", query="anything")
                    )
                milvus_proxy.mode = "passthrough"
                recovered_snap = await _wait_for_dependency_health(coord, "milvus")
                assert recovered_snap.milvus is DependencyHealth.HEALTHY
                now_health += 2.0
                assert await coord.readiness() is True

                # 5) PG outage -> hard_healthy False + readiness False + query rejected.
                pg_proxy.mode = "refuse"
                await pg_proxy.drop_connections()
                snap = await coord.health()
                assert snap.postgres is DependencyHealth.UNAVAILABLE
                assert snap.hard_healthy is False
                assert await coord.readiness() is False
                now_health += 2.0
                with pytest.raises(RuntimeNotReadyError):
                    await coord.run_query(
                        UserTurnRequest(session_id="pg-down", query="anything")
                    )
                pg_proxy.mode = "passthrough"
                recovered_pg_snap = await _wait_for_dependency_health(coord, "postgres")
                assert recovered_pg_snap.postgres is DependencyHealth.HEALTHY
                now_health += 2.0
                assert await coord.readiness() is True
                assert (
                    await coord.run_query(
                        UserTurnRequest(
                            session_id="post-recovery", query="zephyrite-token-alpha"
                        )
                    )
                ).trace_id
            finally:
                pg_proxy.mode = "passthrough"
                redis_proxy.mode = "passthrough"
                milvus_proxy.mode = "passthrough"
                await coord.shutdown()

            with pytest.raises(RuntimeShuttingDownError):
                await coord.readiness()
            with pytest.raises(RuntimeShuttingDownError):
                await coord.run_query(
                    UserTurnRequest(session_id="after-stop", query="anything")
                )
        finally:
            await asyncio.gather(
                pg_proxy.close(), redis_proxy.close(), milvus_proxy.close()
            )
