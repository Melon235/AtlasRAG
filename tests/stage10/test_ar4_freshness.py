"""A-R4 freshness marker ordering, ambiguity and recovery throttling."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from cache_support import (
    CacheRig,
    FakeClock,
    build_cache_rig,
    cached_value,
    core_result,
    request,
)

from atlasrag.application.hashing import request_hash
from atlasrag.domain.errors import (
    CanonicalDataError,
    DependencyTimeoutError,
    DependencyUnavailableError,
)
from atlasrag.providers.cache.keys import parent_cache_key, query_cache_key
from atlasrag.runtime import cache_freshness as freshness_module
from atlasrag.runtime.cache_freshness import CacheFreshnessState

NOW = datetime(2026, 11, 26, tzinfo=UTC)


def _query_key(session: str, query: str = "question") -> str:
    turn = request(session, query)
    return query_cache_key(session, "runtime", request_hash(turn))


def _seed_queries(rig: CacheRig, sessions: tuple[str, ...]) -> list[str]:
    keys = [_query_key(session) for session in sessions]
    for key in keys:
        rig.raw.values[key] = cached_value().model_dump_json()
    return keys


@pytest.mark.asyncio
async def test_marker_false_is_trusted_without_invalidation() -> None:
    rig = build_cache_rig(marker_required=False)
    key = _query_key("session")
    rig.raw.values[key] = cached_value().model_dump_json()

    assert await rig.barrier.enforce() is CacheFreshnessState.TRUSTED
    assert rig.raw.scan_calls == 0
    assert rig.raw.unlink_calls == 0
    assert key in rig.raw.values
    assert rig.marker.required is False


@pytest.mark.asyncio
async def test_invalidation_removes_only_query_namespace() -> None:
    rig = build_cache_rig(marker_required=True)
    query_keys = _seed_queries(rig, ("a", "b"))
    parent_keys = [
        parent_cache_key("rev_" + "a" * 64, "parent_" + "a" * 64),
        parent_cache_key("rev_" + "b" * 64, "parent_" + "b" * 64),
    ]
    for key in parent_keys:
        rig.raw.values[key] = "parent-payload"
    rig.raw.values["application:unrelated"] = "keep"

    assert await rig.barrier.enforce() is CacheFreshnessState.TRUSTED

    assert all(key not in rig.raw.values for key in query_keys)
    assert all(key in rig.raw.values for key in parent_keys)
    assert rig.raw.values["application:unrelated"] == "keep"
    assert rig.marker.required is False
    assert rig.raw.delete_calls == 0


@pytest.mark.asyncio
async def test_multi_page_scan_and_multiple_unlink_batches() -> None:
    rig = build_cache_rig(marker_required=True, page_size=2, scan_batch_size=2)
    query_keys = _seed_queries(rig, ("a", "b", "c", "d", "e"))

    assert await rig.barrier.enforce() is CacheFreshnessState.TRUSTED

    assert all(key not in rig.raw.values for key in query_keys)
    assert len(rig.raw.unlink_batches) >= 2
    assert rig.raw.scan_calls >= 3


@pytest.mark.asyncio
async def test_empty_query_namespace_still_clears_marker() -> None:
    rig = build_cache_rig(marker_required=True)
    assert await rig.barrier.enforce() is CacheFreshnessState.TRUSTED
    assert rig.marker.required is False
    assert rig.raw.unlink_calls == 0


@pytest.mark.asyncio
async def test_invalidation_failure_keeps_marker_and_bypasses_cache() -> None:
    rig = build_cache_rig(marker_required=True)
    key = _seed_queries(rig, ("a",))[0]
    rig.raw.unlink_mode = "timeout"

    assert await rig.barrier.enforce() is CacheFreshnessState.BYPASSED
    assert rig.marker.required is True
    assert key in rig.raw.values
    assert rig.marker.commit_calls == 0

    assert await rig.cache.lookup(request("a")) is None
    assert rig.raw.get_calls == 0
    assert (
        await rig.cache.store(request("a"), core_result(), web_invoked=False) is False
    )
    assert rig.raw.set_calls == 0


@pytest.mark.asyncio
async def test_partial_invalidation_failure_is_bypassed() -> None:
    rig = build_cache_rig(marker_required=True)
    keys = _seed_queries(rig, ("a", "b"))
    rig.raw.unlink_mode = "partial"

    assert await rig.barrier.enforce() is CacheFreshnessState.BYPASSED
    assert rig.marker.required is True
    assert any(key in rig.raw.values for key in keys)
    assert rig.marker.commit_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scan_mode", ["malformed", "invalid_key", "loop", "timeout"])
async def test_invalid_invalidation_responses_keep_marker(
    scan_mode: str,
) -> None:
    rig = build_cache_rig(marker_required=True)
    keys = _seed_queries(rig, ("a",))
    rig.raw.scan_mode = scan_mode

    assert await rig.barrier.enforce() is CacheFreshnessState.BYPASSED
    assert rig.marker.required is True
    assert all(key in rig.raw.values for key in keys)
    assert rig.marker.commit_calls == 0


@pytest.mark.asyncio
async def test_invalidation_timeout_keeps_marker() -> None:
    rig = build_cache_rig(marker_required=True)
    _seed_queries(rig, ("a",))
    rig.raw.scan_mode = "ok"
    rig.raw.unlink_mode = "timeout"

    assert await rig.barrier.enforce() is CacheFreshnessState.BYPASSED
    assert rig.marker.required is True


@pytest.mark.asyncio
async def test_cancellation_during_invalidation_keeps_marker_true() -> None:
    rig = build_cache_rig(marker_required=True)
    _seed_queries(rig, ("a",))
    rig.raw.unlink_mode = "delay"
    task = asyncio.create_task(rig.barrier.enforce())
    await asyncio.wait_for(rig.raw.unlink_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    rig.raw.unlink_release.set()

    assert rig.marker.required is True
    assert rig.marker.commit_calls == 0
    assert rig.raw.unlink_calls == 1


@pytest.mark.asyncio
async def test_complete_invalidation_precedes_marker_clear_and_confirmation() -> None:
    rig = build_cache_rig(marker_required=True)
    _seed_queries(rig, ("a",))

    assert await rig.barrier.enforce() is CacheFreshnessState.TRUSTED

    assert rig.marker.events[0] == "unit_enter"
    assert "marker_read" in rig.marker.events
    assert rig.marker.commit_calls == 1
    assert rig.marker.required is False
    assert rig.raw.unlink_calls == 1
    assert rig.marker.write_calls == 1
    assert rig.marker.read_calls == 2


@pytest.mark.asyncio
async def test_marker_read_unavailable_bypasses_cache_reads_and_writes() -> None:
    rig = build_cache_rig(marker_required=True)
    rig.marker.read_error = DependencyUnavailableError()

    assert await rig.barrier.enforce() is CacheFreshnessState.BYPASSED
    assert await rig.cache.lookup(request("a")) is None
    assert (
        await rig.cache.store(request("a"), core_result(), web_invoked=False) is False
    )
    assert rig.raw.get_calls == 0
    assert rig.raw.set_calls == 0


@pytest.mark.asyncio
async def test_marker_read_timeout_bypasses() -> None:
    rig = build_cache_rig(marker_required=True)
    rig.marker.read_error = DependencyTimeoutError()
    assert await rig.barrier.enforce() is CacheFreshnessState.BYPASSED
    assert rig.marker.required is True


@pytest.mark.asyncio
async def test_canonical_marker_corruption_propagates() -> None:
    rig = build_cache_rig(marker_required=True)
    rig.marker.read_error = CanonicalDataError()
    with pytest.raises(CanonicalDataError):
        await rig.barrier.enforce()
    with pytest.raises(CanonicalDataError):
        await rig.cache.lookup(request("a"))
    assert rig.raw.get_calls == 0


@pytest.mark.asyncio
async def test_marker_set_failure_keeps_marker_true() -> None:
    rig = build_cache_rig(marker_required=True)
    keys = _seed_queries(rig, ("a",))
    rig.marker.set_error = DependencyUnavailableError()

    assert await rig.barrier.enforce() is CacheFreshnessState.BYPASSED
    assert rig.marker.required is True
    assert rig.marker.commit_calls == 0
    assert all(key not in rig.raw.values for key in keys)
    assert await rig.cache.lookup(request("a")) is None
    assert rig.raw.get_calls == 0


@pytest.mark.asyncio
async def test_marker_commit_failure_rolls_back() -> None:
    rig = build_cache_rig(marker_required=True)
    _seed_queries(rig, ("a",))
    rig.marker.commit_mode = "raise_before"

    assert await rig.barrier.enforce() is CacheFreshnessState.BYPASSED
    assert rig.marker.required is True
    assert rig.marker.commit_calls == 1
    assert rig.marker.rollback_calls >= 1


@pytest.mark.asyncio
async def test_marker_commit_applied_with_lost_ack_is_conservatively_bypassed() -> None:
    rig = build_cache_rig(marker_required=True)
    _seed_queries(rig, ("a",))
    rig.marker.commit_mode = "apply_then_raise"

    assert await rig.barrier.enforce() is CacheFreshnessState.BYPASSED
    assert rig.marker.required is False
    assert rig.raw.values == {}

    # The persisted marker is the authority: complete invalidation happened
    # before the applied commit, so a fresh read may trust the empty cache.
    assert await rig.cache.lookup(request("a")) is None
    assert await rig.cache.store(request("a"), core_result(), web_invoked=False) is True
    assert (await rig.cache.lookup(request("a"))) is not None


@pytest.mark.asyncio
async def test_confirmatory_read_failure_remains_conservative() -> None:
    rig = build_cache_rig(marker_required=True)
    _seed_queries(rig, ("a",))
    rig.marker.read_errors = [None, DependencyUnavailableError()]

    assert await rig.barrier.enforce() is CacheFreshnessState.BYPASSED
    assert rig.marker.required is False
    assert rig.raw.values == {}
    assert await rig.cache.lookup(request("a")) is None
    assert await rig.cache.store(request("a"), core_result(), web_invoked=False) is True


@pytest.mark.asyncio
async def test_confirmatory_true_marker_keeps_cache_bypassed() -> None:
    rig = build_cache_rig(marker_required=True)
    key = _seed_queries(rig, ("a",))[0]
    rig.marker.commit_mode = "noop"

    assert await rig.barrier.enforce() is CacheFreshnessState.BYPASSED
    assert rig.marker.required is True
    assert key not in rig.raw.values
    assert await rig.cache.lookup(request("a")) is None
    assert rig.raw.get_calls == 0
    assert (
        await rig.cache.store(request("a"), core_result(), web_invoked=False) is False
    )
    assert rig.raw.set_calls == 0


@pytest.mark.asyncio
async def test_confirmed_false_after_complete_invalidation_reestablishes_trust() -> (
    None
):
    rig = build_cache_rig(marker_required=True)
    _seed_queries(rig, ("a",))
    rig.marker.read_errors = [None, DependencyUnavailableError()]
    assert await rig.barrier.enforce() is CacheFreshnessState.BYPASSED
    assert rig.marker.required is False

    assert await rig.barrier.enforce() is CacheFreshnessState.TRUSTED
    assert rig.marker.required is False
    assert await rig.cache.store(request("a"), core_result(), web_invoked=False) is True
    assert await rig.cache.lookup(request("a")) is not None


@pytest.mark.asyncio
async def test_concurrent_due_recovery_is_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(freshness_module, "monotonic", clock)
    rig = build_cache_rig(marker_required=True, retry_interval=10.0)
    _seed_queries(rig, ("a", "b"))

    results = await asyncio.gather(*(rig.barrier.recover_if_due() for _ in range(5)))

    assert results.count(CacheFreshnessState.TRUSTED) == 1
    assert results.count(None) == 4
    assert rig.raw.scan_calls == 1
    assert rig.marker.required is False


@pytest.mark.asyncio
async def test_failed_recovery_is_throttled_then_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(freshness_module, "monotonic", clock)
    rig = build_cache_rig(marker_required=True, retry_interval=10.0)
    keys = _seed_queries(rig, ("a",))
    rig.raw.unlink_mode = "timeout"

    assert await rig.barrier.recover_if_due() is CacheFreshnessState.BYPASSED
    assert rig.marker.required is True
    assert rig.raw.unlink_calls == 1
    assert await rig.barrier.recover_if_due() is None
    assert rig.raw.unlink_calls == 1

    clock.advance(11.0)
    rig.raw.unlink_mode = "ok"
    assert await rig.barrier.recover_if_due() is CacheFreshnessState.TRUSTED
    assert rig.marker.required is False
    assert all(key not in rig.raw.values for key in keys)


@pytest.mark.asyncio
async def test_enforce_is_not_throttled_by_lazy_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(freshness_module, "monotonic", clock)
    rig = build_cache_rig(marker_required=False, retry_interval=10.0)

    assert await rig.barrier.enforce() is CacheFreshnessState.TRUSTED
    rig.marker.required = True
    assert await rig.barrier.enforce() is CacheFreshnessState.TRUSTED
    assert rig.marker.required is False
    assert rig.marker.commit_calls == 1
    assert await rig.barrier.recover_if_due() is None


@pytest.mark.asyncio
async def test_concurrent_failed_due_recovery_is_attempted_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(freshness_module, "monotonic", clock)
    rig = build_cache_rig(marker_required=True, retry_interval=10.0)
    keys = _seed_queries(rig, ("a",))
    rig.raw.unlink_mode = "timeout"

    results = await asyncio.gather(*(rig.barrier.recover_if_due() for _ in range(5)))

    assert results.count(CacheFreshnessState.BYPASSED) == 1
    assert results.count(None) == 4
    assert rig.raw.unlink_calls == 1
    assert rig.marker.required is True
    assert all(key in rig.raw.values for key in keys)


@pytest.mark.asyncio
async def test_due_callers_waiting_behind_held_recovery_run_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(freshness_module, "monotonic", clock)
    rig = build_cache_rig(marker_required=True, retry_interval=10.0)
    keys = _seed_queries(rig, ("a",))
    rig.raw.unlink_mode = "timeout"
    rig.marker.enter_gate = asyncio.Event()

    active = asyncio.create_task(rig.barrier.enforce())
    await asyncio.wait_for(rig.marker.enter_started.wait(), timeout=2)
    clock.advance(11.0)
    callers = [asyncio.create_task(rig.barrier.recover_if_due()) for _ in range(5)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    rig.marker.enter_gate.set()
    await asyncio.wait_for(active, timeout=2)
    results = await asyncio.gather(*callers)

    assert results.count(CacheFreshnessState.BYPASSED) == 1
    assert results.count(None) == 4
    assert rig.raw.unlink_calls == 2
    assert rig.marker.required is True
    assert all(key in rig.raw.values for key in keys)
