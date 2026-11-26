"""Real PG/Milvus/Redis Stage 10-B Parent Context Cache, freshness and recovery."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from redis.asyncio import Redis

from atlasrag.domain.enums import EvidenceType
from atlasrag.domain.errors import (
    CanonicalDataError,
    DependencyUnavailableError,
    RuntimeNotReadyError,
)
from atlasrag.domain.evidence import EvidenceRef
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalLocalCitation, FinalResponse
from atlasrag.providers.cache.keys import parent_cache_key, query_cache_key
from atlasrag.providers.cache.models import CachedParentContext, CachedQueryResult
from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.providers.index.milvus import MilvusIndexProvider
from atlasrag.repositories.postgres.pool import PostgresPool
from atlasrag.repositories.postgres.records import RuntimeMetadata, StoredChunkType
from atlasrag.repositories.postgres.uow import PostgresUnitOfWork
from atlasrag.runtime import cache_freshness as cache_freshness_module
from atlasrag.runtime.cache_freshness import (
    CacheFreshnessBarrier,
    CacheFreshnessState,
)
from atlasrag.runtime.composition import ApplicationComposition
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.services.local_evidence.parent_context import ParentContextService
from atlasrag.services.sync import deletion as deletion_module
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

PARENT_AND_TABLE_DOC = (
    "# Telemetry Handbook\n\n"
    "## Caching Rules\n\n"
    "Parent expansion serves canonical context alongside redis parent cache hits.\n\n"
    "| CacheName | Role |\n"
    "| --- | --- |\n"
    "| QueryCache | Disposable response cache |\n"
)

PARENT_AND_TABLE_DOC_V2 = (
    "# Telemetry Handbook\n\n"
    "## Caching Rules\n\n"
    "Version two updates parent context with solstice-v2-fresh-fact content.\n\n"
    "| CacheName | Role |\n"
    "| --- | --- |\n"
    "| QueryCache | Disposable response cache |\n"
)

SECONDARY_DOC = (
    "# Secondary Manual\n\n"
    "Remaining corpus content stays queryable after another document is deleted.\n"
)


class _FakeUnitWrapper:
    def __init__(self, inner: PostgresUnitOfWork, spy: dict[str, list[Any]]) -> None:
        self._inner = inner
        self._spy = spy

    async def __aenter__(self) -> PostgresUnitOfWork:
        entered = await self._inner.__aenter__()
        orig_list_by_ids = entered.chunks.list_by_ids
        orig_list_headers = entered.chunks.list_canonical_context_headers

        async def tracked_list_by_ids(chunk_ids: Any) -> Any:
            self._spy["list_by_ids"].append(tuple(chunk_ids))
            return await orig_list_by_ids(chunk_ids)

        async def tracked_list_headers(chunk_ids: Any) -> Any:
            self._spy["list_headers"].append(tuple(chunk_ids))
            return await orig_list_headers(chunk_ids)

        entered.chunks.list_by_ids = tracked_list_by_ids  # type: ignore[method-assign]
        entered.chunks.list_canonical_context_headers = tracked_list_headers  # type: ignore[method-assign]
        return entered

    async def __aexit__(self, *args: object) -> None:
        await self._inner.__aexit__(*args)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_b10_07_real_parent_context_cache_and_header_projection(
    postgres_dsn: str,
    milvus_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, (redis_host, redis_port), (milvus_host, milvus_port) = parse_stage10_endpoints()
    redis_proxy = TcpFaultProxy(redis_host, redis_port)
    await redis_proxy.start()
    knowledge_dir = tmp_path / "kb"
    knowledge_dir.mkdir()
    (knowledge_dir / "handbook.md").write_text(PARENT_AND_TABLE_DOC, encoding="utf-8")
    lock_file = tmp_path / "run" / "atlasrag.lock"
    http_state = HttpMockState()
    patch_runtime_boundaries(
        monkeypatch,
        lock_file=lock_file,
        embedding_factory=lambda **_: FakeQwenEmbedding(),
        reranker_factory=lambda **_: FakeBgeReranker(),
        http_state=http_state,
    )

    config = build_stage10_runtime_config(
        postgres_dsn=postgres_dsn,
        knowledge_directory=knowledge_dir,
        redis_host="127.0.0.1",
        redis_port=redis_proxy.port,
        milvus_host=milvus_host,
        milvus_port=milvus_port,
        runtime_overrides={"redis_socket_timeout_seconds": 0.5},
    )

    async with stage10_isolation_guard(
        redis_host=redis_host, redis_port=redis_port, milvus_uri=milvus_uri
    ):
        comp = ApplicationComposition(config)
        coord = await comp.start()
        try:
            sync_v1 = await coord.run_maintenance()
            assert sync_v1.ready_builds == 1
            assert comp._pool is not None
            assert comp._redis is not None

            spy: dict[str, list[Any]] = {"list_by_ids": [], "list_headers": []}
            uow_factory = lambda: _FakeUnitWrapper(PostgresUnitOfWork(comp._pool), spy)  # noqa: E731

            async with PostgresUnitOfWork(comp._pool) as unit:
                doc = (await unit.documents.list())[0]
                rev1 = doc.current_revision_id
                assert rev1 is not None
                chunks = await unit.chunks.list_for_revision(doc.document_id, rev1)
                parent_chunk = next(
                    c for c in chunks if c.chunk_type is StoredChunkType.TEXT_PARENT
                )
                table_chunk = next(
                    c for c in chunks if c.chunk_type is StoredChunkType.TABLE
                )

            refs = (
                EvidenceRef(
                    evidence_type=EvidenceType.TEXT_PARENT,
                    context_id=parent_chunk.chunk_id,
                    document_id=doc.document_id,
                    revision_id=rev1,
                ),
                EvidenceRef(
                    evidence_type=EvidenceType.TABLE,
                    context_id=table_chunk.chunk_id,
                    document_id=doc.document_id,
                    revision_id=rev1,
                ),
            )

            uncached_service = ParentContextService(
                lambda: PostgresUnitOfWork(comp._pool),  # type: ignore[arg-type]
                cache=comp._redis,
                cache_enabled=False,
            )
            cached_service = ParentContextService(
                uow_factory,  # type: ignore[arg-type]
                cache=comp._redis,
                cache_enabled=True,
                cache_ttl_seconds=600,
            )

            expected = await uncached_service.expand(refs)
            # 1) Cache MISS: full PG read + Redis fill for TEXT_PARENT only.
            first = await cached_service.expand(refs)
            assert first == expected
            assert spy["list_by_ids"] == [(parent_chunk.chunk_id, table_chunk.chunk_id)]
            assert spy["list_headers"] == []
            assert await comp._redis.get_parent(rev1, parent_chunk.chunk_id) is not None
            assert await comp._redis.get_parent(rev1, table_chunk.chunk_id) is None

            # 2) Tamper provenance fields in Redis payload -> header SQL projection supplies authoritative provenance.
            raw_redis = Redis.from_url(
                f"redis://{redis_host}:{redis_port}/0", decode_responses=False
            )
            try:
                p_key = parent_cache_key(rev1, parent_chunk.chunk_id)
                stored_payload = await raw_redis.get(p_key)
                assert stored_payload is not None
                cached_obj = CachedParentContext.model_validate_json(stored_payload)
                tampered = cached_obj.model_copy(
                    update={
                        "provenance": cached_obj.provenance.model_copy(
                            update={
                                "file_name": "tampered.md",
                                "relative_source_path": "tampered.md",
                                "section_path": ("Tampered Section",),
                            }
                        )
                    }
                )
                await raw_redis.set(p_key, tampered.model_dump_json(), ex=600)
            finally:
                await raw_redis.aclose()

            # 3) Cache HIT: TEXT_PARENT skipped in list_by_ids (header SQL projection used), order preserved.
            spy["list_by_ids"].clear()
            spy["list_headers"].clear()
            second = await cached_service.expand(refs)
            assert second == expected
            assert spy["list_by_ids"] == [(table_chunk.chunk_id,)]
            assert spy["list_headers"] == [(parent_chunk.chunk_id,)]

            # 4) Redis outage fallback -> reads from real PG transparently.
            redis_proxy.mode = "refuse"
            await redis_proxy.drop_connections()
            fallback = await cached_service.expand(refs)
            assert fallback == expected
            redis_proxy.mode = "passthrough"

            # 5) Parent Context Cache survives query invalidation cleanly.
            await comp._redis.put_query(
                "s",
                "fp",
                "h1",
                CachedQueryResult(
                    created_at=datetime(2026, 11, 26, 0, 0, tzinfo=UTC),
                    final_response=FinalResponse(answer_text="a", citations=()),
                ),
                ttl_seconds=60,
            )
            assert await comp._redis.invalidate_queries() >= 1
            assert await comp._redis.get_parent(rev1, parent_chunk.chunk_id) is not None

            # 6) Revision change v1 -> v2 isolates parent keys and rejects stale v1 ref.
            (knowledge_dir / "handbook.md").write_text(
                PARENT_AND_TABLE_DOC_V2, encoding="utf-8"
            )
            assert (await coord.run_maintenance()).ready_builds == 1
            with pytest.raises(CanonicalDataError):
                await cached_service.expand(refs[:1])

            async with PostgresUnitOfWork(comp._pool) as unit:
                doc2 = (await unit.documents.list())[0]
                rev2 = doc2.current_revision_id
                assert rev2 is not None and rev2 != rev1
                p2 = next(
                    c
                    for c in await unit.chunks.list_for_revision(doc2.document_id, rev2)
                    if c.chunk_type is StoredChunkType.TEXT_PARENT
                )
            refs2 = (
                EvidenceRef(
                    evidence_type=EvidenceType.TEXT_PARENT,
                    context_id=p2.chunk_id,
                    document_id=doc2.document_id,
                    revision_id=rev2,
                ),
            )
            await cached_service.expand(refs2)
            assert await comp._redis.get_parent(rev2, p2.chunk_id) is not None

            # 7) Deleting document from PG prevents Redis resurrection even while cached key remains.
            async with PostgresUnitOfWork(comp._pool) as unit:
                await unit.chunks.delete_document(doc2.document_id)
                await unit.elements.delete_document(doc2.document_id)
                await unit.documents.delete(doc2.document_id)
                await unit.commit()
            assert await comp._redis.get_parent(rev2, p2.chunk_id) is not None
            with pytest.raises(CanonicalDataError):
                await cached_service.expand(refs2)
        finally:
            redis_proxy.mode = "passthrough"
            await coord.shutdown()
            await redis_proxy.close()


@pytest.mark.asyncio
async def test_b10_08_freshness_marker_durability_multibatch_scan_and_confirmation(
    postgres_dsn: str,
    milvus_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, (redis_host, redis_port), (milvus_host, milvus_port) = parse_stage10_endpoints()
    knowledge_dir = tmp_path / "kb"
    knowledge_dir.mkdir()
    lock_file = tmp_path / "run" / "atlasrag.lock"
    patch_runtime_boundaries(
        monkeypatch,
        lock_file=lock_file,
        embedding_factory=lambda **_: FakeQwenEmbedding(),
        reranker_factory=lambda **_: FakeBgeReranker(),
        http_state=HttpMockState(),
    )
    cfg = build_stage10_runtime_config(
        postgres_dsn=postgres_dsn,
        knowledge_directory=knowledge_dir,
        redis_host=redis_host,
        redis_port=redis_port,
        milvus_host=milvus_host,
        milvus_port=milvus_port,
    )

    async with stage10_isolation_guard(
        redis_host=redis_host, redis_port=redis_port, milvus_uri=milvus_uri
    ):
        comp = await ApplicationComposition(cfg).start()
        try:
            raw_redis = Redis.from_url(
                f"redis://{redis_host}:{redis_port}/0", decode_responses=False
            )
            pool = PostgresPool(postgres_dsn, min_size=1, max_size=4)
            await pool.open()
            try:
                provider = RedisCacheProvider(raw_redis, scan_batch_size=3)
                # Populate 15 query keys across >1 SCAN page + parent + unrelated keys.
                for i in range(15):
                    await raw_redis.set(
                        query_cache_key("sess", "fp", f"req-{i}"), b"{}"
                    )
                await raw_redis.set(parent_cache_key("rev-1", "par-1"), b"{}")
                await raw_redis.set("stage10b:unrelated:keep", b"1")

                # Set marker = True durably in PG, and prove uncommitted clear rolls back.
                async with PostgresUnitOfWork(pool) as unit:
                    await unit.runtime_metadata.set_query_cache_invalidation_required(
                        True
                    )
                    await unit.commit()
                async with PostgresUnitOfWork(pool) as unit:
                    await unit.runtime_metadata.set_query_cache_invalidation_required(
                        False
                    )
                    # Exit without commit -> must roll back!
                async with PostgresUnitOfWork(pool) as unit:
                    assert (
                        await unit.runtime_metadata.get()
                    ).query_cache_invalidation_required is True

                # Failed PG confirmation leaves barrier BYPASSED after Redis sweep.
                call_count = 0

                class _FailingMetadataProxy:
                    def __init__(self, inner: Any) -> None:
                        self._inner = inner

                    async def get(self) -> RuntimeMetadata:
                        return RuntimeMetadata.model_validate(await self._inner.get())

                    async def set_query_cache_invalidation_required(
                        self, required: bool
                    ) -> RuntimeMetadata:
                        raise DependencyUnavailableError

                class _FailClearUnitProxy:
                    def __init__(self, inner: PostgresUnitOfWork) -> None:
                        self._inner = inner
                        self.runtime_metadata = _FailingMetadataProxy(
                            inner.runtime_metadata
                        )

                    async def commit(self) -> None:
                        await self._inner.commit()

                class _FailClearUow:
                    def __init__(self) -> None:
                        self._inner = PostgresUnitOfWork(pool)

                    async def __aenter__(self) -> Any:
                        nonlocal call_count
                        call_count += 1
                        entered = await self._inner.__aenter__()
                        if call_count == 2:
                            return _FailClearUnitProxy(entered)
                        return entered

                    async def __aexit__(self, *args: object) -> None:
                        await self._inner.__aexit__(*args)  # type: ignore[arg-type]

                broken_barrier = CacheFreshnessBarrier(
                    redis=provider,
                    uow_factory=_FailClearUow,  # type: ignore[arg-type]
                )
                assert await broken_barrier.enforce() is CacheFreshnessState.BYPASSED
                assert broken_barrier.active_write_fence is None

                # Repopulate and enforce cleanly via multi-batch SCAN + UNLINK + PG confirmation.
                for i in range(10):
                    await raw_redis.set(query_cache_key("sess", "fp", f"r2-{i}"), b"{}")
                barrier = CacheFreshnessBarrier(
                    redis=provider,
                    uow_factory=lambda: PostgresUnitOfWork(pool),
                )
                assert await barrier.enforce() is CacheFreshnessState.TRUSTED
                assert barrier.active_write_fence is not None
                assert (
                    barrier.active_write_fence == await provider.get_query_write_fence()
                )
                assert [
                    k async for k in raw_redis.scan_iter(match="atlasrag:query:*")
                ] == []
                assert await raw_redis.get(parent_cache_key("rev-1", "par-1")) == b"{}"
                assert await raw_redis.get("stage10b:unrelated:keep") == b"1"
                async with PostgresUnitOfWork(pool) as unit:
                    assert (
                        await unit.runtime_metadata.get()
                    ).query_cache_invalidation_required is False
            finally:
                await pool.close()
                await raw_redis.aclose()
        finally:
            await comp.shutdown()


@pytest.mark.asyncio
async def test_b10_10_redis_outage_during_maintenance_and_throttled_recovery(
    postgres_dsn: str,
    milvus_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, (redis_host, redis_port), (milvus_host, milvus_port) = parse_stage10_endpoints()
    redis_proxy = TcpFaultProxy(redis_host, redis_port)
    await redis_proxy.start()
    knowledge_dir = tmp_path / "kb"
    knowledge_dir.mkdir()
    (knowledge_dir / "handbook.md").write_text(PARENT_AND_TABLE_DOC, encoding="utf-8")
    lock_file = tmp_path / "run" / "atlasrag.lock"
    http_state = HttpMockState(answer_text="Answer v1 stale [L1].")
    patch_runtime_boundaries(
        monkeypatch,
        lock_file=lock_file,
        embedding_factory=lambda **_: FakeQwenEmbedding(),
        reranker_factory=lambda **_: FakeBgeReranker(),
        http_state=http_state,
    )

    now_time = 1000.0
    monkeypatch.setattr(cache_freshness_module, "monotonic", lambda: now_time)

    config = build_stage10_runtime_config(
        postgres_dsn=postgres_dsn,
        knowledge_directory=knowledge_dir,
        redis_host="127.0.0.1",
        redis_port=redis_proxy.port,
        milvus_host=milvus_host,
        milvus_port=milvus_port,
        runtime_overrides={
            "redis_socket_timeout_seconds": 0.4,
            "cache_recovery_interval_seconds": 50.0,
        },
    )

    async with stage10_isolation_guard(
        redis_host=redis_host, redis_port=redis_port, milvus_uri=milvus_uri
    ):
        comp = ApplicationComposition(config)
        coord = await comp.start()
        try:
            assert (await coord.run_maintenance()).ready_builds == 1
            q = UserTurnRequest(session_id="s-outage", query="Caching Rules role")
            v1_env = await coord.run_query(q)
            assert "Answer v1 stale" in v1_env.response.answer_text
            # Second call hits Redis query cache for v1, plus parent cache key exists.
            assert (await coord.run_query(q)).response == v1_env.response
            raw_redis = Redis.from_url(
                f"redis://{redis_host}:{redis_port}/0", decode_responses=False
            )
            try:
                assert (
                    len(
                        [k async for k in raw_redis.scan_iter(match="atlasrag:query:*")]
                    )
                    == 1
                )
                parent_keys_before = [
                    k async for k in raw_redis.scan_iter(match="atlasrag:parent:*")
                ]
                assert len(parent_keys_before) >= 1
            finally:
                await raw_redis.aclose()

            # Redis goes down BEFORE maintenance updating doc -> v2.
            redis_proxy.mode = "refuse"
            await redis_proxy.drop_connections()
            http_state.answer_text = "Answer v2 solstice-v2-fresh-fact [L1]."
            (knowledge_dir / "handbook.md").write_text(
                PARENT_AND_TABLE_DOC_V2, encoding="utf-8"
            )
            maint_v2 = await coord.run_maintenance()
            assert maint_v2.outcome is SyncOutcome.SUCCESS
            assert maint_v2.serving_safety is ServingSafety.SAFE_TO_SERVE
            assert coord.gate.mode is RuntimeMode.SERVING
            assert comp._pool is not None
            async with PostgresUnitOfWork(comp._pool) as unit:
                assert (
                    await unit.runtime_metadata.get()
                ).query_cache_invalidation_required is True

            # Serving continues in degraded cache-bypass mode returning v2, never stale v1.
            outage_env = await coord.run_query(q)
            assert "solstice-v2-fresh-fact" in outage_env.response.answer_text

            # Bring Redis back while throttle window NOT yet elapsed: still must NOT serve stale v1.
            redis_proxy.mode = "passthrough"
            now_time += 10.0
            throttled_env = await coord.run_query(q)
            assert "solstice-v2-fresh-fact" in throttled_env.response.answer_text

            # Advance past cache_recovery_interval_seconds -> recovers, purges stale v1, keeps parent cache.
            now_time += 100.0
            recovered_env = await coord.run_query(q)
            assert "solstice-v2-fresh-fact" in recovered_env.response.answer_text
            async with PostgresUnitOfWork(comp._pool) as unit:
                assert (
                    await unit.runtime_metadata.get()
                ).query_cache_invalidation_required is False

            before_hits = http_state.deepseek_generation_calls
            cached_v2 = await coord.run_query(q)
            assert "solstice-v2-fresh-fact" in cached_v2.response.answer_text
            assert http_state.deepseek_generation_calls == before_hits

            raw_redis = Redis.from_url(
                f"redis://{redis_host}:{redis_port}/0", decode_responses=False
            )
            try:
                p_after = [
                    k async for k in raw_redis.scan_iter(match="atlasrag:parent:*")
                ]
                assert set(parent_keys_before) <= set(p_after)
            finally:
                await raw_redis.aclose()
        finally:
            redis_proxy.mode = "passthrough"
            await coord.shutdown()
            await redis_proxy.close()


@pytest.mark.asyncio
async def test_b10_11_interrupted_deletion_block_restart_and_recover(
    postgres_dsn: str,
    milvus_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, (redis_host, redis_port), (milvus_host, milvus_port) = parse_stage10_endpoints()
    knowledge_dir = tmp_path / "kb"
    knowledge_dir.mkdir()
    target_path = knowledge_dir / "removable.md"
    target_path.write_text(PARENT_AND_TABLE_DOC, encoding="utf-8")
    (knowledge_dir / "keep.md").write_text(SECONDARY_DOC, encoding="utf-8")
    lock_file = tmp_path / "run" / "atlasrag.lock"
    http_state = HttpMockState(answer_text="Corpus intact [L1].")
    patch_runtime_boundaries(
        monkeypatch,
        lock_file=lock_file,
        embedding_factory=lambda **_: FakeQwenEmbedding(),
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
    )

    async with stage10_isolation_guard(
        redis_host=redis_host, redis_port=redis_port, milvus_uri=milvus_uri
    ):
        # 1) Ingest two docs, then delete removable.md from disk + fail BEFORE Milvus delete.
        coord1 = await ApplicationComposition(config).start()
        removable_id = ""
        try:
            assert (await coord1.run_maintenance()).ready_builds == 2
            pool = PostgresPool(postgres_dsn, min_size=1, max_size=4)
            await pool.open()
            try:
                async with PostgresUnitOfWork(pool) as unit:
                    docs = {
                        d.relative_source_path: d for d in await unit.documents.list()
                    }
                    removable_id = docs["removable.md"].document_id
            finally:
                await pool.close()

            target_path.unlink()
            orig_delete_doc = MilvusIndexProvider.delete_document

            async def fail_milvus_delete(self: MilvusIndexProvider, doc_id: str) -> int:
                raise DependencyUnavailableError

            monkeypatch.setattr(
                MilvusIndexProvider, "delete_document", fail_milvus_delete
            )
            failed_sync = await coord1.run_maintenance()
            assert failed_sync.serving_safety is ServingSafety.UNSAFE_TO_SERVE
            assert coord1.gate.mode is RuntimeMode.BLOCKED
            assert await coord1.readiness() is False
            with pytest.raises(RuntimeNotReadyError):
                await coord1.run_query(
                    UserTurnRequest(session_id="s", query="anything")
                )
            monkeypatch.setattr(MilvusIndexProvider, "delete_document", orig_delete_doc)
        finally:
            await coord1.shutdown()

        # 2) Restart after failed Milvus delete: remains BLOCKED without auto-deleting.
        coord2 = await ApplicationComposition(config).start()
        try:
            assert coord2.gate.mode is RuntimeMode.BLOCKED
            assert await coord2.readiness() is False
            # Next, interrupt BETWEEN Milvus delete and PG commit.
            orig_delete_pg = (
                deletion_module.MissingSourceDeletionService._delete_postgres
            )

            async def fail_pg_delete(self: Any, doc_id: str) -> bool:
                raise DependencyUnavailableError

            monkeypatch.setattr(
                deletion_module.MissingSourceDeletionService,
                "_delete_postgres",
                fail_pg_delete,
            )
            half_failed = await coord2.recover()
            assert half_failed.serving_safety is ServingSafety.UNSAFE_TO_SERVE
            assert coord2.gate.mode is RuntimeMode.BLOCKED
            monkeypatch.setattr(
                deletion_module.MissingSourceDeletionService,
                "_delete_postgres",
                orig_delete_pg,
            )
        finally:
            await coord2.shutdown()

        # 3) Restart with Milvus deleted but PG row still present: must start BLOCKED.
        coord3 = await ApplicationComposition(config).start()
        try:
            assert coord3.gate.mode is RuntimeMode.BLOCKED
            assert await coord3.readiness() is False
            with pytest.raises(RuntimeNotReadyError):
                await coord3.run_query(
                    UserTurnRequest(session_id="s", query="anything")
                )

            pool = PostgresPool(postgres_dsn, min_size=1, max_size=4)
            await pool.open()
            try:
                async with PostgresUnitOfWork(pool) as unit:
                    assert await unit.documents.get_by_id(removable_id) is not None
            finally:
                await pool.close()

            # Clean recover() converges: PG removable row deleted, keep.md remains SERVING.
            recovered = await coord3.recover()
            assert recovered.outcome is SyncOutcome.SUCCESS
            assert recovered.serving_safety is ServingSafety.SAFE_TO_SERVE
            recovered_mode: RuntimeMode = coord3.gate.mode
            assert recovered_mode == RuntimeMode.SERVING
            assert await coord3.readiness() is True

            pool = PostgresPool(postgres_dsn, min_size=1, max_size=4)
            await pool.open()
            try:
                async with PostgresUnitOfWork(pool) as unit:
                    assert await unit.documents.get_by_id(removable_id) is None
                    remaining = await unit.documents.list()
                    assert [d.relative_source_path for d in remaining] == ["keep.md"]
            finally:
                await pool.close()

            env = await coord3.run_query(
                UserTurnRequest(session_id="s", query="Remaining corpus queryable")
            )
            cits = [
                c for c in env.response.citations if isinstance(c, FinalLocalCitation)
            ]
            assert cits
            assert cits[0].relative_source_path == "keep.md"
        finally:
            await coord3.shutdown()
