"""A-R6 Redis transport bounds, server-side write fencing and delayed-write matrix."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import redis.asyncio as redis
from cache_support import (
    DelayedRemoteRawRedis,
    FakeRawRedis,
    Knowledge,
    build_cache_rig,
    build_runtime,
    prepare,
    rebuild_rig,
    request,
)
from redis.backoff import NoBackoff
from redis.retry import Retry
from support import runtime_config

from atlasrag.application.hashing import request_hash
from atlasrag.config.models import RuntimeOperationalConfig
from atlasrag.domain.errors import (
    DependencyTimeoutError,
    DependencyUnavailableError,
    InvariantViolationError,
    ProviderResponseError,
)
from atlasrag.domain.indexing.models import EmbeddingModelIdentity
from atlasrag.providers.cache.keys import (
    QUERY_WRITE_FENCE_KEY,
    parent_cache_key,
    query_cache_key,
)
from atlasrag.providers.cache.redis import (
    _PUT_FENCED_QUERY_SCRIPT,
    _PUT_UNFENCED_IF_UNPROTECTED_SCRIPT,
    RedisCacheProvider,
)
from atlasrag.providers.embedding.qwen import (
    QWEN_DIMENSION,
    QWEN_MODEL_ID,
    QWEN_REVISION,
)
from atlasrag.providers.reranker.base import RerankerModelIdentity
from atlasrag.providers.reranker.bge import (
    BGE_RERANKER_MODEL_ID,
    BGE_RERANKER_REVISION,
)
from atlasrag.runtime import composition as composition_module
from atlasrag.runtime import production as production_module
from atlasrag.runtime.cache_freshness import CacheFreshnessState
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.services.cache.session import CacheCommitOutcome


def _key(session: str, query: str = "question") -> str:
    return query_cache_key(session, "runtime", request_hash(request(session, query)))


class _Metadata:
    query_cache_invalidation_required = False

    async def get(self) -> _Metadata:
        return self

    async def set_query_cache_invalidation_required(self, required: bool) -> _Metadata:
        return self


class _Documents:
    async def list(self) -> tuple[object, ...]:
        return ()


class _Unit:
    documents = _Documents()
    runtime_metadata = _Metadata()

    async def __aenter__(self) -> _Unit:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _Pool:
    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def health(self) -> None:
        return None


class _Milvus:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.embedding_dimension = int(kwargs.get("embedding_dimension", 1024))  # type: ignore[call-overload]
        self.analyzer = str(kwargs.get("analyzer", "standard"))

    async def initialize(self) -> None:
        return None

    async def health(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def iter_all_projection(self, **kwargs: object) -> Any:
        if False:
            yield None

    async def iter_revision_projection(self, **kwargs: object) -> Any:
        if False:
            yield None

    async def delete_revision(self, *args: object, **kwargs: object) -> int:
        return 0

    async def wait_revision_visible(self, *args: object, **kwargs: object) -> None:
        return None


class _Embedding:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self._device = str(kwargs.get("device", "cuda"))

    @property
    def identity(self) -> EmbeddingModelIdentity:
        return EmbeddingModelIdentity(
            model_id=QWEN_MODEL_ID,
            model_revision=QWEN_REVISION,
            tokenizer_id=QWEN_MODEL_ID,
            tokenizer_revision=QWEN_REVISION,
            dimension=QWEN_DIMENSION,
            device=self._device,
        )

    async def initialize(self, *, allow_download: bool = False) -> None:
        return None


class _Reranker:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self._device = str(kwargs.get("device", "cuda"))

    @property
    def identity(self) -> RerankerModelIdentity:
        return RerankerModelIdentity(
            model_id=BGE_RERANKER_MODEL_ID,
            model_revision=BGE_RERANKER_REVISION,
            tokenizer_id=BGE_RERANKER_MODEL_ID,
            tokenizer_revision=BGE_RERANKER_REVISION,
            device=self._device,
            dtype="float16",
            max_input_tokens=8192,
        )

    async def initialize(self, *, allow_download: bool = False) -> None:
        return None


class _Ownership:
    def acquire(self) -> None:
        return None

    def release(self) -> None:
        return None


# AR6-01 — Production Redis transport bounds & boundary error translation
@pytest.mark.asyncio
async def test_ar6_01_production_redis_client_uses_bounded_no_retry_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[tuple[tuple[object, ...], dict[str, Any]]] = []

    class _Redis(FakeRawRedis):
        async def aclose(self) -> None:
            return None

    def _from_url(*args: object, **kwargs: object) -> _Redis:
        captured.append((args, dict(kwargs)))
        return _Redis()

    orig_async_client = httpx.AsyncClient
    monkeypatch.setattr(composition_module, "PostgresPool", lambda *a, **k: _Pool())
    monkeypatch.setattr(composition_module, "MilvusClient", lambda **k: object())
    monkeypatch.setattr(composition_module, "MilvusIndexProvider", _Milvus)
    monkeypatch.setattr(composition_module, "QwenEmbeddingProvider", _Embedding)
    monkeypatch.setattr(composition_module, "BgeRerankerProvider", _Reranker)
    monkeypatch.setattr(redis, "from_url", _from_url)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: orig_async_client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200))
        ),
    )
    monkeypatch.setattr(composition_module, "RuntimeOwnershipLock", _Ownership)
    monkeypatch.setattr(production_module, "PostgresUnitOfWork", lambda pool: _Unit())
    monkeypatch.setattr(composition_module, "PostgresUnitOfWork", lambda pool: _Unit())

    base = runtime_config()
    custom_dir = tmp_path / "knowledge"
    custom_dir.mkdir(parents=True, exist_ok=True)
    config = base.model_copy(
        update={
            "infrastructure": base.infrastructure.model_copy(
                update={"knowledge_directory": str(custom_dir)}
            ),
            "runtime": RuntimeOperationalConfig(
                postgres_pool_max_size=16,
                redis_connect_timeout_seconds=4.5,
                redis_socket_timeout_seconds=6.5,
                redis_health_check_interval_seconds=25.0,
            ),
        }
    )

    coordinator = await composition_module.ApplicationComposition(config).start()
    try:
        assert len(captured) == 1
        args, kwargs = captured[0]
        assert args == (
            f"redis://{config.infrastructure.redis_host}:{config.infrastructure.redis_port}/0",
        )
        assert kwargs["decode_responses"] is False
        assert kwargs["socket_connect_timeout"] == 4.5
        assert kwargs["socket_timeout"] == 6.5
        assert kwargs["socket_keepalive"] is True
        assert kwargs["health_check_interval"] == 25.0
        assert kwargs["retry_on_timeout"] is False
        assert kwargs["retry_on_error"] == []
        retry_policy = kwargs["retry"]
        assert isinstance(retry_policy, Retry)
        assert retry_policy._retries == 0
        assert isinstance(retry_policy._backoff, NoBackoff)
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_ar6_01_redis_fence_primitives_enforce_strict_error_and_ack_contracts() -> (
    None
):
    raw = FakeRawRedis()
    provider = RedisCacheProvider(cast(Any, raw))

    with pytest.raises(InvariantViolationError):
        await provider.put_fenced_query(
            "s",
            "f",
            "h",
            core_result_payload := build_cache_rig().cache._policy and None,  # type: ignore[arg-type]
            ttl_seconds=60,
            write_fence="token",
        )
    del core_result_payload

    rig = build_cache_rig(require_write_fence=True)
    await rig.barrier.enforce()
    good_token = await rig.provider.get_query_write_fence()
    assert good_token is not None

    # Corrupt fence token in Redis must not pass validation silently.
    rig.raw.values[QUERY_WRITE_FENCE_KEY] = "bad token with space"
    with pytest.raises(ProviderResponseError):
        await rig.provider.get_query_write_fence()
    with pytest.raises(ProviderResponseError):
        await rig.provider.get_fenced_query("s", "f", "h")

    # Malformed or bool EVAL response is rejected at the provider boundary.
    rig.raw.values[QUERY_WRITE_FENCE_KEY] = good_token
    rig.raw.eval_mode = "malformed"
    with pytest.raises(ProviderResponseError):
        await rig.provider.get_fenced_query("s", "f", "h")
    with pytest.raises(ProviderResponseError):
        await rig.provider.ensure_query_write_fence()

    # Invalid caller token fails before Redis is touched.
    rig.raw.eval_mode = "ok"
    valid_prep = await prepare(build_cache_rig(), request("s"))
    assert valid_prep is not None
    before = rig.raw.eval_calls
    for bad_token in ("", "with space", "with\nnewline", "x" * 129, object()):
        with pytest.raises(InvariantViolationError):
            await rig.provider.put_fenced_query(
                "s",
                "runtime",
                "h",
                valid_prep.payload,
                ttl_seconds=60,
                write_fence=bad_token,  # type: ignore[arg-type]
            )
    assert rig.raw.eval_calls == before

    # Transport timeout / unavailable translate deterministically.
    rig.raw.eval_mode = "timeout"
    with pytest.raises(DependencyTimeoutError):
        await rig.provider.get_fenced_query("s", "runtime", "h")
    rig.raw.eval_mode = "unavailable"
    with pytest.raises(DependencyUnavailableError):
        await rig.provider.ensure_query_write_fence()


# AR6-02 — Server-side write fencing semantics
@pytest.mark.asyncio
async def test_ar6_02_fenced_cache_requires_fence_and_rejects_stale_or_revoked_tokens() -> (
    None
):
    rig = build_cache_rig(require_write_fence=True)
    turn = request("session-a")
    stale_key = _key("session-a")
    prepared_unfenced = await prepare(rig, turn)
    assert prepared_unfenced is None

    # Even if a stale entry is manually present in Redis, lookup without an
    # active fence fails closed as a miss.
    unprotected_rig = build_cache_rig()
    prep = await prepare(unprotected_rig, turn)
    assert prep is not None
    rig.raw.values[stale_key] = prep.payload.model_dump_json()
    assert await rig.cache.lookup(turn) is None

    # Establish active fence via freshness barrier -> read & write work cleanly.
    rig.raw.values.pop(stale_key)
    assert await rig.barrier.enforce() is CacheFreshnessState.TRUSTED
    fence_v1 = await rig.provider.get_query_write_fence()
    assert fence_v1 is not None

    prepared_v1 = await prepare(rig, turn)
    assert prepared_v1 is not None
    assert prepared_v1.write_fence == fence_v1
    assert await rig.cache.commit(prepared_v1) is CacheCommitOutcome.STORED
    assert (await rig.cache.lookup(turn)) is not None

    # If the fence is revoked or rotated before commit reaches Redis, the Lua
    # guard rejects the write atomically and SessionQueryCache reports BYPASSED.
    prepared_second = await prepare(rig, request("session-b"))
    assert prepared_second is not None
    await rig.provider.revoke_query_write_fence()
    assert await rig.cache.commit(prepared_second) is CacheCommitOutcome.BYPASSED
    assert _key("session-b") not in rig.raw.values

    fence_v2 = await rig.provider.establish_query_write_fence()
    assert fence_v2 != fence_v1
    assert await rig.cache.commit(prepared_second) is CacheCommitOutcome.BYPASSED
    assert _key("session-b") not in rig.raw.values

    # Unfenced callers are also rejected server-side once a fence key exists.
    assert (
        await rig.provider.put_fenced_query(
            "session-c",
            "runtime",
            request_hash(request("session-c")),
            prepared_second.payload,
            ttl_seconds=60,
            write_fence=None,
        )
        is False
    )
    assert _key("session-c") not in rig.raw.values


@pytest.mark.asyncio
async def test_ar6_02_put_fenced_query_direct_error_boundaries() -> None:
    prep_rig = build_cache_rig(require_write_fence=True)
    await prep_rig.barrier.enforce()
    raw = prep_rig.raw
    provider = prep_rig.provider
    token = await provider.get_query_write_fence()
    assert token is not None
    prepared = await prepare(prep_rig, request("session-a"))
    assert prepared is not None

    raw.set_mode = "timeout"
    with pytest.raises(DependencyTimeoutError):
        await provider.put_fenced_query(
            "session-a",
            "runtime",
            request_hash(request("session-a")),
            prepared.payload,
            ttl_seconds=60,
            write_fence=token,
        )
    raw.set_mode = "reject"
    with pytest.raises(DependencyUnavailableError):
        await provider.put_fenced_query(
            "session-a",
            "runtime",
            request_hash(request("session-a")),
            prepared.payload,
            ttl_seconds=60,
            write_fence=token,
        )


# AR6-03 — Delayed remote execution after timeout/disconnect/lease release/restart
@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["timeout", "connection", "bad_ack"])
async def test_ar6_03_delayed_remote_write_after_unknown_and_lease_release_rejected_after_maintenance(
    failure_mode: str,
) -> None:
    raw = DelayedRemoteRawRedis()
    raw.remote_failure_mode = failure_mode
    rig = build_cache_rig(require_write_fence=True, raw=raw)
    knowledge = Knowledge()
    runtime = build_runtime(rig=rig, knowledge=knowledge)
    await runtime.start()

    # Client query experiences transport failure; commit classifies UNKNOWN and
    # returns without holding QueryLease, while the write sits delayed in transit.
    first = await runtime.query(request("session-a"))
    assert first.response.answer_text == "Answer v1 [L1]."
    assert runtime.gate.active_queries == 0
    assert len(raw.pending_remote_writes) == 1
    assert raw.pending_remote_writes[0][0] == _PUT_FENCED_QUERY_SCRIPT
    assert _key("session-a") not in raw.values

    # Maintenance executes while Q1 is already finished and clears/rotates fence.
    raw.delay_remote_writes = False
    result = await runtime.maintenance()
    assert result.serving_safety.value == "SAFE_TO_SERVE"
    assert rig.marker.required is False
    assert runtime.gate.mode is RuntimeMode.SERVING

    # Delayed remote SET finally reaches Redis AFTER cache is TRUSTED again:
    # server Lua rejects it deterministically because the write fence rotated.
    flushed = await raw.flush_pending_remote_writes()
    assert flushed == [0]
    assert _key("session-a") not in raw.values

    second = await runtime.query(request("session-a"))
    assert second.response.answer_text == "Answer v2 [L1]."
    hit = await runtime.query(request("session-a"))
    assert hit.response.answer_text == "Answer v2 [L1]."


@pytest.mark.asyncio
async def test_ar6_03_delayed_remote_write_rejected_before_marker_clear_when_fence_revoked() -> (
    None
):
    raw = DelayedRemoteRawRedis()
    rig = build_cache_rig(require_write_fence=True, raw=raw)
    knowledge = Knowledge()
    runtime = build_runtime(rig=rig, knowledge=knowledge)
    await runtime.start()

    await runtime.query(request("session-a"))
    assert len(raw.pending_remote_writes) == 1

    # Simulate maintenance committing marker=True and Barrier revoking fence
    # BEFORE invalidate_queries and BEFORE PG marker=False clears.
    async with rig.marker() as unit:
        await unit.runtime_metadata.set_query_cache_invalidation_required(True)
        await unit.commit()
    await rig.provider.revoke_query_write_fence()
    assert rig.marker.required is True
    assert QUERY_WRITE_FENCE_KEY not in raw.values

    # Delayed remote write arrives in the window while fence is revoked: rejected!
    flushed = await raw.flush_pending_remote_writes()
    assert flushed == [0]
    assert _key("session-a") not in raw.values


@pytest.mark.asyncio
async def test_ar6_03_cancellation_after_submitted_write_rejected_when_remote_flushes_post_maintenance() -> (
    None
):
    raw = FakeRawRedis()
    raw.set_mode = "delay"
    rig = build_cache_rig(require_write_fence=True, raw=raw)
    knowledge = Knowledge()
    runtime = build_runtime(rig=rig, knowledge=knowledge)
    await runtime.start()

    query_task = asyncio.create_task(runtime.query(request("session-a")))
    await asyncio.wait_for(raw.set_started.wait(), timeout=2.0)
    query_task.cancel()
    await asyncio.sleep(0)
    raw.set_release.set()
    raw.set_mode = "store"
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(query_task, timeout=2.0)

    # Maintenance runs, invalidates query namespace, and rotates write fence.
    await runtime.maintenance()
    assert _key("session-a") not in raw.values

    # Simulate a delayed transport-in-flight duplicate / late-arriving command
    # carrying the pre-maintenance fence arriving afterward via DelayedRemoteRawRedis.
    delayed = DelayedRemoteRawRedis()
    rig2 = build_cache_rig(require_write_fence=True, raw=delayed)
    runtime2 = build_runtime(rig=rig2, knowledge=Knowledge())
    await runtime2.start()
    delayed.delay_remote_writes = True
    await runtime2.query(request("session-cancel"))
    delayed.delay_remote_writes = False
    await runtime2.maintenance()
    assert await delayed.flush_pending_remote_writes() == [0]
    assert (
        query_cache_key(
            "session-cancel", "runtime", request_hash(request("session-cancel"))
        )
        not in delayed.values
    )


@pytest.mark.asyncio
async def test_ar6_03_pre_restart_unknown_write_rejected_after_restart_with_marker_false() -> (
    None
):
    raw = DelayedRemoteRawRedis()
    rig = build_cache_rig(require_write_fence=True, raw=raw)
    knowledge = Knowledge()
    runtime = build_runtime(rig=rig, knowledge=knowledge)
    await runtime.start()

    # Pre-existing trusted cache entry from an earlier completed query.
    raw.delay_remote_writes = False
    trusted = await runtime.query(request("session-trusted"))
    assert trusted.response.answer_text == "Answer v1 [L1]."
    trusted_key = _key("session-trusted")
    assert trusted_key in raw.values

    # New query hits timeout -> UNKNOWN -> queued remote write before shutdown.
    raw.delay_remote_writes = True
    unknown = await runtime.query(request("session-delayed"))
    assert unknown.response.answer_text == "Answer v1 [L1]."
    delayed_key = _key("session-delayed")
    assert delayed_key not in raw.values
    assert len(raw.pending_remote_writes) == 1
    await runtime.shutdown()

    # Restart runtime with marker=False: startup enforce() rotates fence
    # without scanning/unlinking existing valid keys, killing pre-restart delayed writes.
    assert rig.marker.required is False
    raw.delay_remote_writes = False
    scan_before = raw.scan_calls
    unlink_before = raw.unlink_calls
    fresh = rebuild_rig(rig)
    restarted = build_runtime(rig=fresh, knowledge=knowledge)
    await restarted.start()

    assert raw.scan_calls == scan_before
    assert raw.unlink_calls == unlink_before
    assert trusted_key in raw.values
    assert await raw.flush_pending_remote_writes() == [0]
    assert delayed_key not in raw.values

    # Trusted pre-restart key still serves hits, and delayed key misses & computes fresh.
    assert (await restarted.cache.lookup(request("session-trusted"))) is not None
    assert (await restarted.cache.lookup(request("session-delayed"))) is None


@pytest.mark.asyncio
async def test_ar6_03_parent_cache_survives_delayed_writes_and_fence_lifecycle() -> (
    None
):
    raw = DelayedRemoteRawRedis()
    rig = build_cache_rig(require_write_fence=True, raw=raw)
    parent_key = parent_cache_key("rev_" + "1" * 64, "parent_" + "a" * 64)
    parent_payload = '{"canonical":"parent"}'
    raw.values[parent_key] = parent_payload

    knowledge = Knowledge()
    runtime = build_runtime(rig=rig, knowledge=knowledge)
    await runtime.start()

    await runtime.query(request("session-a"))
    assert len(raw.pending_remote_writes) == 1
    raw.delay_remote_writes = False
    await runtime.maintenance()
    assert await raw.flush_pending_remote_writes() == [0]
    assert raw.values.get(parent_key) == parent_payload
    assert _key("session-a") not in raw.values


@pytest.mark.asyncio
async def test_ar6_03_standalone_unfenced_write_rejected_if_fence_established_before_flush() -> (
    None
):
    raw = DelayedRemoteRawRedis()
    standalone = build_cache_rig(require_write_fence=False, raw=raw)
    raw.delay_remote_writes = True
    prep = await prepare(standalone, request("session-u"))
    assert prep is not None
    outcome = await standalone.cache.commit(prep)
    assert outcome is CacheCommitOutcome.UNKNOWN
    assert raw.pending_remote_writes[0][0] == _PUT_UNFENCED_IF_UNPROTECTED_SCRIPT

    # Barrier subsequently establishes a write fence; delayed unfenced write is rejected.
    raw.delay_remote_writes = False
    await standalone.barrier.enforce()
    assert await raw.flush_pending_remote_writes() == [0]
    assert _key("session-u") not in raw.values


@pytest.mark.asyncio
async def test_ar6_03_unsafe_maintenance_blocks_then_recover_rotates_fence_and_rejects_delayed_write() -> (
    None
):
    raw = DelayedRemoteRawRedis()
    rig = build_cache_rig(require_write_fence=True, raw=raw)
    knowledge = Knowledge()
    runtime = build_runtime(rig=rig, knowledge=knowledge)
    await runtime.start()

    # Timed-out write before maintenance leaves command queued on network.
    await runtime.query(request("session-a"))
    assert len(raw.pending_remote_writes) == 1

    # First maintenance produces UNSAFE_TO_SERVE -> runtime enters BLOCKED.
    raw.delay_remote_writes = False
    runtime.sync.mode = "unsafe"
    unsafe_result = await runtime.maintenance()
    assert unsafe_result.serving_safety.value == "UNSAFE_TO_SERVE"
    assert runtime.gate.mode is RuntimeMode.BLOCKED
    assert rig.marker.required is True

    # Operator recovery succeeds -> SAFE_TO_SERVE -> barrier revokes/sweeps/rotates.
    runtime.sync.mode = "safe"
    recovered = await runtime.coordinator.recover()
    assert recovered.serving_safety.value == "SAFE_TO_SERVE"
    mode_after_recovery: RuntimeMode = runtime.gate.mode
    assert mode_after_recovery is RuntimeMode.SERVING
    assert rig.marker.required is False

    # Delayed remote write carrying pre-recovery fence is rejected by Lua.
    assert await raw.flush_pending_remote_writes() == [0]
    assert _key("session-a") not in raw.values
    fresh = await runtime.query(request("session-a"))
    assert fresh.response.answer_text == "Answer v2 [L1]."
