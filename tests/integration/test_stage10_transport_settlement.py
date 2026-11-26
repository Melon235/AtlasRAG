"""Real Redis 8.2.10 AR4-T01 transport settlement, Lua fences and startup fail-closed proof."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from redis.asyncio import Redis
from redis.retry import Retry

from atlasrag.application.hashing import request_hash
from atlasrag.config.fingerprint import runtime_fingerprint
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse
from atlasrag.providers.cache.keys import QUERY_WRITE_FENCE_KEY, query_cache_key
from atlasrag.providers.cache.models import CachedQueryResult
from atlasrag.runtime import cache_freshness as cache_freshness_module
from atlasrag.runtime.cache_freshness import CacheFreshnessState
from atlasrag.runtime.composition import ApplicationComposition
from atlasrag.services.sync.models import ServingSafety, SyncOutcome
from integration.stage10_helpers import (
    FakeBgeReranker,
    FakeQwenEmbedding,
    HttpMockState,
    TcpFaultProxy,
    build_stage10_runtime_config,
    parse_stage10_endpoints,
    patch_runtime_boundaries,
    send_raw_resp_to_redis,
    stage10_isolation_guard,
)

pytestmark = pytest.mark.integration

DOC_V1 = (
    "# Transport Settlement Guide\n\n"
    "Delayed Redis EVAL writes carrying stale fences are rejected atomically.\n"
)

DOC_V2 = (
    "# Transport Settlement Guide\n\n"
    "Maintenance rotation settles revision two transport state safely.\n"
)


def _cached_result(answer_text: str) -> CachedQueryResult:
    return CachedQueryResult(
        created_at=datetime(2026, 11, 26, 0, 0, tzinfo=UTC),
        final_response=FinalResponse(answer_text=answer_text, citations=()),
    )


@pytest.mark.asyncio
async def test_b10_09_real_redis_lua_fence_and_composition_client_config(
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
    config = build_stage10_runtime_config(
        postgres_dsn=postgres_dsn,
        knowledge_directory=knowledge_dir,
        redis_host=redis_host,
        redis_port=redis_port,
        milvus_host=milvus_host,
        milvus_port=milvus_port,
        runtime_overrides={
            "redis_connect_timeout_seconds": 1.25,
            "redis_socket_timeout_seconds": 1.5,
            "redis_health_check_interval_seconds": 0.0,
        },
    )

    async with stage10_isolation_guard(
        redis_host=redis_host, redis_port=redis_port, milvus_uri=milvus_uri
    ):
        comp = ApplicationComposition(config)
        coord = await comp.start()
        try:
            assert comp._redis_client is not None
            kwargs = comp._redis_client.connection_pool.connection_kwargs
            assert kwargs["socket_connect_timeout"] == 1.25
            assert kwargs["socket_timeout"] == 1.5
            assert kwargs["socket_keepalive"] is True
            assert kwargs["retry_on_timeout"] is False
            assert kwargs["retry_on_error"] == []
            retry_cfg = kwargs["retry"]
            assert isinstance(retry_cfg, Retry)
            assert retry_cfg._retries == 0

            assert comp._redis is not None
            fence_t1 = await comp._redis.get_query_write_fence()
            assert fence_t1 is not None

            # Real Lua EVAL enforces fence token matching and positive TTL.
            val1 = _cached_result("v1")
            assert (
                await comp._redis.put_fenced_query(
                    "s", "fp", "h1", val1, ttl_seconds=120, write_fence=fence_t1
                )
                is True
            )
            ttl = await comp._redis_client.ttl(query_cache_key("s", "fp", "h1"))
            assert 0 < ttl <= 120

            # Unfenced compatibility mode is refused whenever QUERY_WRITE_FENCE_KEY exists.
            assert (
                await comp._redis.put_fenced_query(
                    "s",
                    "fp",
                    "h_unfenced",
                    _cached_result("bad"),
                    ttl_seconds=60,
                    write_fence=None,
                )
                is False
            )

            fence_t2 = await comp._redis.establish_query_write_fence()
            assert fence_t2 != fence_t1
            assert await comp._redis.ensure_query_write_fence() == fence_t2

            # Stale T1 write rejected; existing key not overwritten.
            assert (
                await comp._redis.put_fenced_query(
                    "s",
                    "fp",
                    "h1",
                    _cached_result("stale"),
                    ttl_seconds=120,
                    write_fence=fence_t1,
                )
                is False
            )
            loaded_fence, loaded = await comp._redis.get_fenced_query("s", "fp", "h1")
            assert loaded_fence == fence_t2
            assert loaded is not None and loaded.final_response.answer_text == "v1"
        finally:
            await coord.shutdown()


@pytest.mark.asyncio
async def test_b10_09_ar4_t01_proxy_stalled_eval_rejected_by_real_redis_after_maintenance(
    postgres_dsn: str,
    milvus_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, (redis_host, redis_port), (milvus_host, milvus_port) = parse_stage10_endpoints()
    proxy = TcpFaultProxy(redis_host, redis_port)
    await proxy.start()

    knowledge_dir = tmp_path / "kb"
    knowledge_dir.mkdir()
    (knowledge_dir / "settlement.md").write_text(DOC_V1, encoding="utf-8")
    lock_file = tmp_path / "run" / "atlasrag.lock"
    marker = "__AR4_T01_DELAYED_RESP__"
    http_state = HttpMockState(answer_text=f"V1 answer {marker} [L1].")
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
        redis_port=proxy.port,
        milvus_host=milvus_host,
        milvus_port=milvus_port,
        runtime_overrides={
            "redis_socket_timeout_seconds": 0.25,
            "cache_recovery_interval_seconds": 300.0,
        },
    )

    async with stage10_isolation_guard(
        redis_host=redis_host, redis_port=redis_port, milvus_uri=milvus_uri
    ):
        comp = ApplicationComposition(config)
        coord = await comp.start()
        try:
            assert (await coord.run_maintenance()).ready_builds == 1

            raw_redis = Redis.from_url(
                f"redis://{redis_host}:{redis_port}/0", decode_responses=False
            )
            try:
                fence_t1 = (await raw_redis.get(QUERY_WRITE_FENCE_KEY)).decode("utf-8")  # type: ignore[union-attr]

                # Intercept ONLY the fenced EVAL write containing the unique v1 marker.
                proxy.mode = "hold_match_and_stall"
                proxy.drop_next_match = marker.encode("utf-8")

                req = UserTurnRequest(
                    session_id="ar4-t01", query="delayed RESP write test"
                )
                env_v1 = await coord.run_query(req)
                assert marker in env_v1.response.answer_text
                assert coord.gate.active_queries == 0
                await proxy.wait_captured(timeout=5.0)
                assert len(proxy.captured_payloads) == 1
                captured_eval_resp = proxy.captured_payloads[0]
                assert marker.encode("utf-8") in captured_eval_resp
                assert fence_t1.encode("utf-8") in captured_eval_resp

                # Redis must not have received or persisted the delayed write yet.
                assert [
                    k async for k in raw_redis.scan_iter(match="atlasrag:query:*")
                ] == []

                # Restore proxy & run maintenance v2, rotating fence T1 -> T2.
                proxy.mode = "passthrough"
                proxy.drop_next_match = None
                await proxy.drop_connections()
                http_state.answer_text = "V2 clean settled answer [L1]."
                (knowledge_dir / "settlement.md").write_text(DOC_V2, encoding="utf-8")
                sync_v2 = await coord.run_maintenance()
                assert sync_v2.outcome is SyncOutcome.SUCCESS
                assert sync_v2.serving_safety is ServingSafety.SAFE_TO_SERVE
                fence_t2 = (await raw_redis.get(QUERY_WRITE_FENCE_KEY)).decode("utf-8")  # type: ignore[union-attr]
                assert fence_t2 != fence_t1

                # Replay exact captured delayed RESP EVAL packet directly to real Redis 8.2.10.
                redis_reply = await send_raw_resp_to_redis(
                    redis_host, redis_port, captured_eval_resp
                )
                assert redis_reply == b":0\r\n"
                assert [
                    k async for k in raw_redis.scan_iter(match="atlasrag:query:*")
                ] == []

                # Subsequent query under v2 returns clean v2, never stale v1.
                env_v2 = await coord.run_query(req)
                assert env_v2.response.answer_text == "V2 clean settled answer [L1]."
                assert [k async for k in raw_redis.scan_iter(match="atlasrag:query:*")]
            finally:
                await raw_redis.aclose()
        finally:
            proxy.mode = "passthrough"
            await coord.shutdown()
            await proxy.close()


@pytest.mark.asyncio
async def test_b10_09_startup_fence_reset_failure_leaves_barrier_bypassed_and_blocks_old_fence(
    postgres_dsn: str,
    milvus_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, (redis_host, redis_port), (milvus_host, milvus_port) = parse_stage10_endpoints()
    proxy = TcpFaultProxy(redis_host, redis_port)
    await proxy.start()

    knowledge_dir = tmp_path / "kb"
    knowledge_dir.mkdir()
    (knowledge_dir / "settlement.md").write_text(DOC_V2, encoding="utf-8")
    lock_file = tmp_path / "run" / "atlasrag.lock"
    http_state = HttpMockState(answer_text="Fresh uncached answer [L1].")
    patch_runtime_boundaries(
        monkeypatch,
        lock_file=lock_file,
        embedding_factory=lambda **_: FakeQwenEmbedding(),
        reranker_factory=lambda **_: FakeBgeReranker(),
        http_state=http_state,
    )

    now_time = 2000.0
    monkeypatch.setattr(cache_freshness_module, "monotonic", lambda: now_time)

    config = build_stage10_runtime_config(
        postgres_dsn=postgres_dsn,
        knowledge_directory=knowledge_dir,
        redis_host="127.0.0.1",
        redis_port=proxy.port,
        milvus_host=milvus_host,
        milvus_port=milvus_port,
        runtime_overrides={
            "redis_socket_timeout_seconds": 0.25,
            "cache_recovery_interval_seconds": 120.0,
        },
    )
    fp = runtime_fingerprint(config)

    req_trusted = UserTurnRequest(
        session_id="startup-bypass", query="Startup trusted bypass check"
    )
    req_delayed = UserTurnRequest(
        session_id="startup-delayed", query="Startup pre-restart delayed check"
    )
    trusted_key = query_cache_key(req_trusted.session_id, fp, request_hash(req_trusted))
    delayed_key = query_cache_key(req_delayed.session_id, fp, request_hash(req_delayed))

    async with stage10_isolation_guard(
        redis_host=redis_host, redis_port=redis_port, milvus_uri=milvus_uri
    ):
        raw_redis = Redis.from_url(
            f"redis://{redis_host}:{redis_port}/0", decode_responses=False
        )
        try:
            # 1) Populate doc, cache req_trusted under T_old, and capture stalled delayed EVAL packet.
            comp1 = ApplicationComposition(config)
            coord1 = await comp1.start()
            try:
                assert (await coord1.run_maintenance()).ready_builds == 1
                http_state.answer_text = "Trusted pre-restart answer [L1]."
                await coord1.run_query(req_trusted)
                assert await raw_redis.get(trusted_key) is not None
                raw_t_old = await raw_redis.get(QUERY_WRITE_FENCE_KEY)
                assert isinstance(raw_t_old, bytes)
                t_old = raw_t_old.decode("utf-8")

                delayed_marker = "__PRE_RESTART_DELAYED__"
                http_state.answer_text = f"Delayed pre-restart {delayed_marker} [L1]."
                proxy.mode = "hold_match_and_stall"
                proxy.drop_next_match = delayed_marker.encode("utf-8")
                await coord1.run_query(req_delayed)
                await proxy.wait_captured(timeout=5.0)
                pre_restart_packet = proxy.captured_payloads[0]
                assert await raw_redis.get(delayed_key) is None
            finally:
                proxy.mode = "passthrough"
                proxy.drop_next_match = None
                await coord1.shutdown()

            # 2) Restart with lost ACK / timeout on establish_query_write_fence SET.
            proxy.mode = "hold_match_and_stall"
            proxy.drop_next_match = (
                b"$3\r\nSET\r\n"
                + f"${len(QUERY_WRITE_FENCE_KEY)}\r\n{QUERY_WRITE_FENCE_KEY}\r\n".encode(
                    "ascii"
                )
            )

            http_state.answer_text = "Fresh uncached answer [L1]."
            comp2 = ApplicationComposition(config)
            coord2 = await comp2.start()
            try:
                assert coord2._deps.freshness.state is CacheFreshnessState.BYPASSED
                assert coord2._deps.freshness.active_write_fence is None

                # Restore proxy connectivity while throttle interval has NOT elapsed:
                # T_old still sits in Redis, so runtime MUST fail closed and bypass trusted_key.
                proxy.mode = "passthrough"
                proxy.drop_next_match = None
                await proxy.drop_connections()

                now_time += 10.0
                calls_before_bypass = http_state.deepseek_generation_calls
                env_bypassed = await coord2.run_query(req_trusted)
                assert (
                    env_bypassed.response.answer_text == "Fresh uncached answer [L1]."
                )
                assert http_state.deepseek_generation_calls == calls_before_bypass + 1

                # New uncached query must not persist anything while BYPASSED.
                req_no_write = UserTurnRequest(
                    session_id="startup-nowrite", query="Do not write under old fence"
                )
                await coord2.run_query(req_no_write)
                assert (
                    await raw_redis.get(
                        query_cache_key(
                            req_no_write.session_id, fp, request_hash(req_no_write)
                        )
                    )
                    is None
                )

                # Once recovery interval elapses, fence rotates T_old -> T_new, rejecting delayed T_old write.
                now_time += 200.0
                calls_before_rec = http_state.deepseek_generation_calls
                env_recovered = await coord2.run_query(req_trusted)
                assert (
                    env_recovered.response.answer_text
                    == "Trusted pre-restart answer [L1]."
                )
                assert http_state.deepseek_generation_calls == calls_before_rec
                raw_fence_after = await raw_redis.get(QUERY_WRITE_FENCE_KEY)
                assert isinstance(raw_fence_after, bytes)
                fence_after = raw_fence_after.decode("utf-8")
                assert fence_after != t_old
                state_after: CacheFreshnessState = coord2._deps.freshness.state
                assert state_after == CacheFreshnessState.TRUSTED
                assert coord2._deps.freshness.active_write_fence == fence_after

                assert (
                    await send_raw_resp_to_redis(
                        redis_host, redis_port, pre_restart_packet
                    )
                    == b":0\r\n"
                )
                assert await raw_redis.get(delayed_key) is None

                # Fresh uncached query now persists under T_new and hits on replay.
                req_post = UserTurnRequest(
                    session_id="startup-post-recovery", query="Startup post-recovery"
                )
                calls_post = http_state.deepseek_generation_calls
                await coord2.run_query(req_post)
                assert http_state.deepseek_generation_calls == calls_post + 1
                await coord2.run_query(req_post)
                assert http_state.deepseek_generation_calls == calls_post + 1
            finally:
                await coord2.shutdown()
        finally:
            await raw_redis.aclose()
            proxy.mode = "passthrough"
            await proxy.close()
