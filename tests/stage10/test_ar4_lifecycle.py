"""A-R4 runtime cache lifecycle, maintenance exclusion and settlement."""

from __future__ import annotations

import asyncio

import pytest
from cache_support import (
    UNSAFE,
    FakeClock,
    Knowledge,
    build_cache_rig,
    build_runtime,
    core_result,
    rebuild_rig,
    request,
)
from runtime_harness import pump_until

from atlasrag.application.hashing import request_hash
from atlasrag.domain.errors import (
    MaintenanceDrainTimeoutError,
    RuntimeNotReadyError,
    RuntimeShutdownFailedError,
)
from atlasrag.providers.cache.keys import query_cache_key
from atlasrag.runtime import cache_freshness as freshness_module
from atlasrag.runtime.modes import RuntimeMode


def _key(session: str, query: str = "question") -> str:
    return query_cache_key(session, "runtime", request_hash(request(session, query)))


@pytest.mark.asyncio
async def test_full_session_cache_lifecycle_and_maintenance_invalidation() -> None:
    rig = build_cache_rig()
    knowledge = Knowledge()
    runtime = build_runtime(rig=rig, knowledge=knowledge)
    await runtime.start()
    assert runtime.gate.mode is RuntimeMode.SERVING

    first = await runtime.query(request("session-a"))
    assert first.response.answer_text == "Answer v1 [L1]."
    assert rig.raw.set_calls == 1
    hit = await runtime.query(request("session-a"))
    assert hit.response.answer_text == "Answer v1 [L1]."
    assert rig.raw.set_calls == 1

    other_session = await runtime.query(request("session-b"))
    assert other_session.response.answer_text == "Answer v1 [L1]."
    assert rig.raw.set_calls == 2
    assert _key("session-a") in rig.raw.values
    assert _key("session-b") in rig.raw.values

    result = await runtime.maintenance()

    assert result.serving_safety.value == "SAFE_TO_SERVE"
    assert knowledge.version == 2
    assert runtime.gate.mode is RuntimeMode.SERVING
    assert rig.marker.required is False
    assert _key("session-a") not in rig.raw.values
    assert _key("session-b") not in rig.raw.values

    refreshed = await runtime.query(request("session-a"))
    assert refreshed.response.answer_text == "Answer v2 [L1]."
    assert _key("session-a") in rig.raw.values
    second_hit = await runtime.query(request("session-a"))
    assert second_hit.response.answer_text == "Answer v2 [L1]."

    await runtime.shutdown()


@pytest.mark.asyncio
async def test_restart_with_marker_true_invalidates_before_cache_trust() -> None:
    rig = build_cache_rig()
    knowledge = Knowledge()
    runtime = build_runtime(rig=rig, knowledge=knowledge)
    await runtime.start()
    await runtime.query(request("session-a"))
    assert _key("session-a") in rig.raw.values
    await runtime.shutdown()

    rig.marker.required = True
    fresh = rebuild_rig(rig)
    restarted = build_runtime(rig=fresh, knowledge=knowledge)
    await restarted.start()

    assert restarted.gate.mode is RuntimeMode.SERVING
    assert fresh.marker.required is False
    assert fresh.raw.values == {}
    assert restarted.order[:4] == [
        "acquire",
        "reconcile",
        "source_safety",
        "verify",
    ]

    response = await restarted.query(request("session-a"))
    assert response.response.answer_text == "Answer v1 [L1]."
    assert _key("session-a") in fresh.raw.values


@pytest.mark.asyncio
async def test_cache_lookup_and_write_occur_while_query_lease_is_active() -> None:
    rig = build_cache_rig()
    runtime = build_runtime(rig=rig, knowledge=Knowledge())
    await runtime.start()
    observed: list[tuple[str, int]] = []
    rig.raw.observer = lambda name: observed.append((name, runtime.gate.active_queries))

    await runtime.query(request("session-a"))

    assert observed
    assert all(active == 1 for name, active in observed if name in {"get", "set"})
    assert {name for name, _ in observed} >= {"get", "set"}


@pytest.mark.asyncio
async def test_maintenance_waits_for_delayed_write_and_invalidates_it() -> None:
    rig = build_cache_rig()
    rig.raw.set_mode = "delay"
    runtime = build_runtime(rig=rig, knowledge=Knowledge())
    await runtime.start()
    query_task = asyncio.create_task(runtime.query(request("session-a")))
    await asyncio.wait_for(rig.raw.set_started.wait(), timeout=2)

    maintenance_task = asyncio.create_task(runtime.maintenance())
    await pump_until(
        lambda: runtime.gate.mode is RuntimeMode.DRAINING,
        label="maintenance draining",
    )
    assert runtime.sync.calls == 0
    query_task.cancel()
    await asyncio.sleep(0)
    assert not query_task.done()
    assert runtime.gate.active_queries == 1

    rig.raw.set_release.set()
    rig.raw.set_mode = "store"
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(query_task, timeout=2)
    result = await asyncio.wait_for(maintenance_task, timeout=2)

    assert result.serving_safety.value == "SAFE_TO_SERVE"
    assert runtime.sync.calls == 1
    assert runtime.gate.mode is RuntimeMode.SERVING
    assert _key("session-a") not in rig.raw.values
    refreshed = await runtime.query(request("session-a"))
    assert refreshed.response.answer_text == "Answer v2 [L1]."


@pytest.mark.asyncio
async def test_maintenance_drain_timeout_preserves_stalled_query() -> None:
    rig = build_cache_rig()
    rig.raw.set_mode = "delay"
    runtime = build_runtime(rig=rig, knowledge=Knowledge(), drain_timeout=0.01)
    await runtime.start()
    query_task = asyncio.create_task(runtime.query(request("session-a")))
    await asyncio.wait_for(rig.raw.set_started.wait(), timeout=2)

    with pytest.raises(MaintenanceDrainTimeoutError):
        await runtime.maintenance()

    assert runtime.sync.calls == 0
    assert runtime.gate.mode is RuntimeMode.SERVING
    assert runtime.gate.active_queries == 1
    assert not query_task.done()

    rig.raw.set_release.set()
    rig.raw.set_mode = "store"
    response = await asyncio.wait_for(query_task, timeout=2)
    assert response.response.answer_text == "Answer v1 [L1]."
    assert runtime.gate.active_queries == 0
    result = await runtime.maintenance()
    assert result.serving_safety.value == "SAFE_TO_SERVE"


@pytest.mark.asyncio
async def test_shutdown_stalled_write_fails_then_recovers_after_settlement() -> None:
    rig = build_cache_rig()
    rig.raw.set_mode = "delay"
    runtime = build_runtime(rig=rig, knowledge=Knowledge(), shutdown_timeout=0.02)
    await runtime.start()
    query_task = asyncio.create_task(runtime.query(request("session-a")))
    await asyncio.wait_for(rig.raw.set_started.wait(), timeout=2)

    with pytest.raises(RuntimeShutdownFailedError):
        await asyncio.wait_for(runtime.shutdown(), timeout=2)

    assert runtime.close.calls == 0
    assert runtime.ownership.released == 0
    assert runtime.gate.mode is RuntimeMode.SHUTTING_DOWN
    assert runtime.gate.active_queries == 1
    assert not query_task.done()

    rig.raw.set_release.set()
    rig.raw.set_mode = "store"
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(query_task, timeout=2)
    assert runtime.gate.active_queries == 0

    await asyncio.wait_for(runtime.shutdown(), timeout=2)
    assert runtime.close.calls == 1
    assert runtime.ownership.released == 1


@pytest.mark.asyncio
async def test_invalidation_stall_keeps_admission_closed_until_proven() -> None:
    rig = build_cache_rig()
    runtime = build_runtime(rig=rig, knowledge=Knowledge())
    await runtime.start()
    await runtime.query(request("session-a"))
    rig.raw.unlink_mode = "delay"
    maintenance_task = asyncio.create_task(runtime.maintenance())
    await asyncio.wait_for(rig.raw.unlink_started.wait(), timeout=2)

    assert runtime.gate.mode is RuntimeMode.MAINTENANCE
    assert runtime.sync.calls == 1
    assert rig.marker.required is True
    with pytest.raises(RuntimeNotReadyError):
        await runtime.query(request("session-b"))

    rig.raw.unlink_mode = "ok"
    rig.raw.unlink_release.set()
    await asyncio.wait_for(maintenance_task, timeout=2)
    assert rig.marker.required is False
    assert runtime.gate.mode is RuntimeMode.SERVING
    refreshed = await runtime.query(request("session-a"))
    assert refreshed.response.answer_text == "Answer v2 [L1]."


@pytest.mark.asyncio
async def test_cancelled_invalidation_blocks_runtime_and_keeps_marker_true() -> None:
    rig = build_cache_rig()
    runtime = build_runtime(rig=rig, knowledge=Knowledge())
    await runtime.start()
    await runtime.query(request("session-a"))
    rig.raw.unlink_mode = "delay"
    maintenance_task = asyncio.create_task(runtime.maintenance())
    await asyncio.wait_for(rig.raw.unlink_started.wait(), timeout=2)

    maintenance_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(maintenance_task, timeout=2)
    rig.raw.unlink_release.set()

    assert runtime.gate.mode is RuntimeMode.BLOCKED
    assert rig.marker.required is True
    assert await runtime.cache.lookup(request("session-a")) is None
    assert (
        await runtime.cache.store(
            request("session-a"), core_result(), web_invoked=False
        )
        is False
    )
    with pytest.raises(RuntimeNotReadyError):
        await runtime.query(request("session-b"))


@pytest.mark.asyncio
async def test_unsafe_maintenance_result_cannot_authorize_cache_use() -> None:
    rig = build_cache_rig()
    runtime = build_runtime(rig=rig, knowledge=Knowledge())
    await runtime.start()
    await runtime.query(request("session-a"))
    runtime.sync.mode = "unsafe"

    result = await runtime.maintenance()

    assert result.serving_safety is UNSAFE.serving_safety
    assert runtime.gate.mode is RuntimeMode.BLOCKED
    assert rig.marker.required is True
    assert rig.marker.commit_calls == 1
    assert await runtime.cache.lookup(request("session-a")) is None
    assert rig.raw.get_calls == 1
    with pytest.raises(RuntimeNotReadyError):
        await runtime.query(request("session-b"))


@pytest.mark.asyncio
async def test_redis_outage_bypasses_cache_and_recovery_removes_stale_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(freshness_module, "monotonic", clock)
    rig = build_cache_rig(retry_interval=10.0)
    knowledge = Knowledge()
    runtime = build_runtime(rig=rig, knowledge=knowledge)
    await runtime.start()
    await runtime.query(request("session-a"))
    assert _key("session-a") in rig.raw.values
    stale_key = _key("session-a")

    rig.raw.get_mode = "unavailable"
    rig.raw.set_mode = "unavailable_before"
    rig.raw.scan_mode = "unavailable"
    rig.raw.unlink_mode = "unavailable"
    result = await runtime.maintenance()

    assert result.serving_safety.value == "SAFE_TO_SERVE"
    assert runtime.gate.mode is RuntimeMode.SERVING
    assert rig.marker.required is True
    assert stale_key in rig.raw.values
    reads = rig.raw.get_calls
    writes = rig.raw.set_calls

    degraded = await runtime.query(request("session-a"))
    assert degraded.response.answer_text == "Answer v2 [L1]."
    assert rig.raw.get_calls == reads
    assert rig.raw.set_calls == writes

    rig.raw.get_mode = "ok"
    rig.raw.set_mode = "store"
    rig.raw.scan_mode = "ok"
    rig.raw.unlink_mode = "ok"
    clock.advance(11.0)
    stale_payload = rig.raw.values[stale_key]
    event_mark = len(rig.raw.events)
    recovered = await runtime.query(request("session-a"))
    recovery_events = rig.raw.events[event_mark:]
    assert "unlink" in recovery_events
    assert recovery_events.index("unlink") < recovery_events.index("set")
    assert recovered.response.answer_text == "Answer v2 [L1]."
    assert rig.marker.required is False
    assert rig.raw.values[stale_key] != stale_payload
    assert "Answer v2" in rig.raw.values[stale_key]
    hit = await runtime.query(request("session-a"))
    assert hit.response.answer_text == "Answer v2 [L1]."


@pytest.mark.asyncio
async def test_lazy_recovery_cannot_overlap_maintenance_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(freshness_module, "monotonic", clock)
    rig = build_cache_rig(retry_interval=10.0)
    runtime = build_runtime(rig=rig, knowledge=Knowledge())
    await runtime.start()
    await runtime.query(request("session-a"))
    assert rig.marker.required is False

    rig.marker.required = True
    clock.advance(11.0)
    rig.raw.unlink_mode = "delay"
    query_task = asyncio.create_task(runtime.query(request("session-a")))
    await asyncio.wait_for(rig.raw.unlink_started.wait(), timeout=2)
    assert runtime.gate.active_queries == 1

    maintenance_task = asyncio.create_task(runtime.maintenance())
    await pump_until(
        lambda: runtime.gate.mode is RuntimeMode.DRAINING,
        label="maintenance draining",
    )
    assert runtime.sync.calls == 0

    rig.raw.unlink_mode = "ok"
    rig.raw.unlink_release.set()
    response = await asyncio.wait_for(query_task, timeout=2)
    assert response.response.answer_text == "Answer v1 [L1]."
    result = await asyncio.wait_for(maintenance_task, timeout=2)

    assert result.serving_safety.value == "SAFE_TO_SERVE"
    assert runtime.sync.calls == 1
    assert runtime.gate.mode is RuntimeMode.SERVING
    assert rig.marker.required is False
    refreshed = await runtime.query(request("session-a"))
    assert refreshed.response.answer_text == "Answer v2 [L1]."
