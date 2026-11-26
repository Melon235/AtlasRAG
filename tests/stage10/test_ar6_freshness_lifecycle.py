"""A-R6 freshness barrier ordering, failure preservation and concurrent lease tests."""

from __future__ import annotations

import asyncio

import pytest
from cache_support import (
    FakeClock,
    Knowledge,
    build_cache_rig,
    build_runtime,
    core_result,
    prepare,
    request,
)

from atlasrag.application.hashing import request_hash
from atlasrag.domain.errors import CanonicalDataError
from atlasrag.providers.cache.keys import (
    QUERY_WRITE_FENCE_KEY,
    parent_cache_key,
    query_cache_key,
)
from atlasrag.runtime import cache_freshness as freshness_module
from atlasrag.runtime.cache_freshness import CacheFreshnessState
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.services.cache.session import CacheCommitOutcome


def _query_key(session: str) -> str:
    return query_cache_key(session, "runtime", request_hash(request(session)))


# AR6-04 — Exact ordering and failure safety across marker=True and marker=False
@pytest.mark.asyncio
async def test_ar6_04_marker_true_enforces_exact_fence_revoke_sweep_establish_pg_clear_order() -> (
    None
):
    rig = build_cache_rig(marker_required=True, require_write_fence=True)
    stale_key = _query_key("session-a")
    old_fence = "fence-old"
    rig.raw.values[QUERY_WRITE_FENCE_KEY] = old_fence
    rig.raw.values[stale_key] = '{"stale":true}'

    state = await rig.barrier.enforce()
    assert state is CacheFreshnessState.TRUSTED
    assert rig.marker.required is False
    assert stale_key not in rig.raw.values
    new_fence = rig.raw.values.get(QUERY_WRITE_FENCE_KEY)
    assert isinstance(new_fence, str) and new_fence != old_fence

    redis_ops = [
        event
        for event in rig.raw.events
        if event in {"fence_delete", "scan", "unlink", "fence_set"}
    ]
    assert redis_ops == ["fence_delete", "scan", "unlink", "fence_set"]
    assert rig.marker.events == [
        "unit_enter",
        "marker_read",
        "unit_enter",
        ("marker_set", False),
        "commit",
        "unit_enter",
        "marker_read",
    ]


@pytest.mark.asyncio
async def test_ar6_04_failure_during_fence_revoke_sweep_or_establish_keeps_marker_true_and_blocks_stale_writes() -> (
    None
):
    # 1. Revoke failure
    rig1 = build_cache_rig(marker_required=True, require_write_fence=True)
    rig1.raw.values[QUERY_WRITE_FENCE_KEY] = "old"
    rig1.raw.delete_mode = "unavailable"
    assert await rig1.barrier.enforce() is CacheFreshnessState.BYPASSED
    assert rig1.marker.required is True

    # 2. Failure during SCAN after fence revoked: fence stays absent, marker True
    rig2 = build_cache_rig(marker_required=True, require_write_fence=True)
    rig2.raw.values[QUERY_WRITE_FENCE_KEY] = "old"
    rig2.raw.values[_query_key("s1")] = '{"stale":true}'
    rig2.raw.scan_mode = "unavailable"
    assert await rig2.barrier.enforce() is CacheFreshnessState.BYPASSED
    assert QUERY_WRITE_FENCE_KEY not in rig2.raw.values
    assert rig2.marker.required is True
    assert await rig2.cache.lookup(request("s1")) is None
    assert (
        await rig2.provider.put_fenced_query(
            "s1",
            "runtime",
            request_hash(request("s1")),
            (await prepare(build_cache_rig(), request("s1"))).payload,  # type: ignore[union-attr]
            ttl_seconds=60,
            write_fence="old",
        )
        is False
    )

    # 3. Failure during establish_query_write_fence after sweep: fence stays absent
    rig3 = build_cache_rig(marker_required=True, require_write_fence=True)
    rig3.raw.values[QUERY_WRITE_FENCE_KEY] = "old"
    rig3.raw.values[_query_key("s2")] = '{"stale":true}'
    rig3.raw.set_mode = "unavailable_before"
    assert await rig3.barrier.enforce() is CacheFreshnessState.BYPASSED
    assert QUERY_WRITE_FENCE_KEY not in rig3.raw.values
    assert _query_key("s2") not in rig3.raw.values
    assert rig3.marker.required is True

    # Recover cleanly on retry
    rig3.raw.set_mode = "store"
    assert await rig3.barrier.enforce() is CacheFreshnessState.TRUSTED
    assert QUERY_WRITE_FENCE_KEY in rig3.raw.values
    assert rig3.marker.required is False


@pytest.mark.asyncio
async def test_ar6_04_canonical_pg_error_always_propagates_and_outage_startup_rotates_on_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig_err = build_cache_rig(marker_required=True, require_write_fence=True)
    rig_err.marker.commit_mode = "canonical"
    with pytest.raises(CanonicalDataError):
        await rig_err.barrier.enforce()

    clock = FakeClock()
    monkeypatch.setattr(freshness_module, "monotonic", clock)
    rig = build_cache_rig(
        marker_required=False, retry_interval=10.0, require_write_fence=True
    )
    rig.raw.values[QUERY_WRITE_FENCE_KEY] = "pre-restart-fence"
    rig.raw.set_mode = "unavailable_before"

    # Startup enforce() fails to rotate due to Redis outage -> BYPASSED
    assert await rig.barrier.enforce() is CacheFreshnessState.BYPASSED
    assert rig.raw.values[QUERY_WRITE_FENCE_KEY] == "pre-restart-fence"

    # Once Redis recovers and retry interval elapses, recover_if_due() rotates
    # away from the pre-startup fence once.
    rig.raw.set_mode = "store"
    clock.advance(11.0)
    assert await rig.barrier.recover_if_due() is CacheFreshnessState.TRUSTED
    assert rig.raw.values[QUERY_WRITE_FENCE_KEY] != "pre-restart-fence"


# AR6-05 — Concurrent QueryLease preservation & periodic recovery without false invalidation
@pytest.mark.asyncio
async def test_ar6_05_periodic_recover_if_due_preserves_active_fence_for_inflight_concurrent_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(freshness_module, "monotonic", clock)
    rig = build_cache_rig(
        marker_required=False, retry_interval=10.0, require_write_fence=True
    )
    assert await rig.barrier.enforce() is CacheFreshnessState.TRUSTED
    active_fence = rig.raw.values[QUERY_WRITE_FENCE_KEY]

    # Query A enters QueryLease and completes preparation under active_fence.
    prepared_a = await prepare(rig, request("session-a"))
    assert prepared_a is not None
    assert prepared_a.write_fence == active_fence

    # Another query advances past retry_interval and runs recover_if_due() while
    # Query A is still live inside its QueryLease: marker=False preserves active_fence!
    clock.advance(11.0)
    assert await rig.barrier.recover_if_due() is CacheFreshnessState.TRUSTED
    assert rig.raw.values[QUERY_WRITE_FENCE_KEY] == active_fence

    # Query A commits cleanly without false BYPASSED.
    assert await rig.cache.commit(prepared_a) is CacheCommitOutcome.STORED
    assert await rig.cache.lookup(request("session-a")) is not None


@pytest.mark.asyncio
async def test_ar6_05_periodic_recover_if_due_reestablishes_missing_fence_after_redis_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(freshness_module, "monotonic", clock)
    rig = build_cache_rig(
        marker_required=False, retry_interval=10.0, require_write_fence=True
    )
    assert await rig.barrier.enforce() is CacheFreshnessState.TRUSTED
    initial_fence = rig.raw.values[QUERY_WRITE_FENCE_KEY]

    # Simulate volatile Redis restart emptying keys while runtime stayed up.
    rig.raw.values.clear()
    clock.advance(11.0)
    assert await rig.barrier.recover_if_due() is CacheFreshnessState.TRUSTED
    reestablished = rig.raw.values[QUERY_WRITE_FENCE_KEY]
    assert reestablished != initial_fence

    assert (
        await rig.cache.store(request("session-a"), core_result(), web_invoked=False)
        is True
    )


@pytest.mark.asyncio
async def test_ar6_05_parent_cache_and_maintenance_exclusion_preserved_under_full_lifecycle() -> (
    None
):
    rig = build_cache_rig(require_write_fence=True)
    parent_key = parent_cache_key("rev_" + "a" * 64, "parent_" + "1" * 64)
    rig.raw.values[parent_key] = "immutable-parent"
    runtime = build_runtime(rig=rig, knowledge=Knowledge())
    await runtime.start()

    await runtime.query(request("session-a"))
    assert _query_key("session-a") in rig.raw.values

    # Concurrent delayed commit stalls while maintenance drains cleanly.
    rig.raw.set_mode = "delay"
    delayed_query = asyncio.create_task(runtime.query(request("session-b")))
    await asyncio.wait_for(rig.raw.set_started.wait(), timeout=2.0)
    maintenance_task = asyncio.create_task(runtime.maintenance())
    await asyncio.sleep(0)
    assert runtime.gate.mode is RuntimeMode.DRAINING
    assert runtime.sync.calls == 0

    rig.raw.set_release.set()
    rig.raw.set_mode = "store"
    await delayed_query
    sync_result = await maintenance_task
    assert sync_result.serving_safety.value == "SAFE_TO_SERVE"
    mode_after_sync: RuntimeMode = runtime.gate.mode
    assert mode_after_sync is RuntimeMode.SERVING
    assert _query_key("session-a") not in rig.raw.values
    assert _query_key("session-b") not in rig.raw.values
    assert rig.raw.values[parent_key] == "immutable-parent"
    await runtime.shutdown()
