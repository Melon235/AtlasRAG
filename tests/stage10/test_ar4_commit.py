"""A-R4 prepare/commit boundary, ambiguous writes and same-key concurrency."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from cache_support import (
    Knowledge,
    build_cache_rig,
    build_rag_core,
    core_result,
    request,
)

from atlasrag.application.retrieval import RetrievalRunner
from atlasrag.domain.errors import (
    DependencyUnavailableError,
    InvariantViolationError,
)
from atlasrag.graphs.retrieval.graph import RetrievalGraphServices
from atlasrag.providers.cache.keys import query_cache_key
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.cache.session import (
    CacheCommitOutcome,
    SessionQueryCache,
)


@pytest.mark.asyncio
async def test_cancellation_during_preparation_submits_no_write() -> None:
    rig = build_cache_rig()
    rig.marker.enter_gate = asyncio.Event()
    task = asyncio.create_task(
        rig.cache.prepare(request("a"), core_result(), web_invoked=False)
    )
    await asyncio.wait_for(rig.marker.enter_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    rig.marker.enter_gate.set()
    assert rig.raw.set_calls == 0


@pytest.mark.asyncio
async def test_cancellation_immediately_before_commit_submits_no_write() -> None:
    rig = build_cache_rig()
    prepared = await rig.cache.prepare(request("a"), core_result(), web_invoked=False)
    assert prepared is not None
    task = asyncio.create_task(rig.cache.commit(prepared))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.raw.set_calls == 0


@pytest.mark.asyncio
async def test_acknowledged_commit_stores_one_entry() -> None:
    rig = build_cache_rig()
    prepared = await rig.cache.prepare(request("a"), core_result(), web_invoked=False)
    assert prepared is not None
    assert await rig.cache.commit(prepared) is CacheCommitOutcome.STORED
    assert rig.raw.set_calls == 1
    assert len(rig.raw.values) == 1
    assert await rig.cache.store(request("a"), core_result(), web_invoked=False) is True


@pytest.mark.asyncio
async def test_cancellation_during_submitted_write_settles_then_propagates() -> None:
    rig = build_cache_rig()
    rig.raw.set_mode = "delay"
    prepared = await rig.cache.prepare(request("a"), core_result(), web_invoked=False)
    assert prepared is not None
    task = asyncio.create_task(rig.cache.commit(prepared))
    await asyncio.wait_for(rig.raw.set_started.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert rig.raw.set_calls == 1
    rig.raw.set_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert len(rig.raw.values) == 1


@pytest.mark.asyncio
async def test_repeated_cancellation_during_write_settles_without_hanging() -> None:
    rig = build_cache_rig()
    rig.raw.set_mode = "delay"
    prepared = await rig.cache.prepare(request("a"), core_result(), web_invoked=False)
    assert prepared is not None
    task = asyncio.create_task(rig.cache.commit(prepared))
    await asyncio.wait_for(rig.raw.set_started.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    rig.raw.set_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert len(rig.raw.values) == 1


@pytest.mark.asyncio
async def test_cancellation_after_write_acknowledgement_keeps_valid_entry() -> None:
    rig = build_cache_rig()
    rig.raw.set_mode = "apply_delay"
    prepared = await rig.cache.prepare(
        request("a"), core_result(text="kept [L1]."), web_invoked=False
    )
    assert prepared is not None
    task = asyncio.create_task(rig.cache.commit(prepared))
    await asyncio.wait_for(rig.raw.set_started.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    rig.raw.set_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    hit = await rig.cache.lookup(request("a"))
    assert hit is not None and hit.answer_text == "kept [L1]."


@pytest.mark.asyncio
async def test_store_wrapper_reports_false_for_unknown_outcome() -> None:
    rig = build_cache_rig()
    rig.raw.set_mode = "apply_timeout"
    assert (
        await rig.cache.store(request("a"), core_result(), web_invoked=False) is False
    )
    assert rig.raw.set_calls == 1
    assert len(rig.raw.values) == 1


@pytest.mark.asyncio
async def test_actual_retrieval_graph_stores_then_hits() -> None:
    rig = build_cache_rig()
    knowledge = Knowledge()
    runner = RetrievalRunner(
        RetrievalGraphServices(cache=rig.cache, rag_core=build_rag_core(knowledge))
    )

    first = await runner.run(request("a"))
    second = await runner.run(request("a"))

    assert first == second
    assert first.answer_text == "Answer v1 [L1]."
    assert rig.raw.set_calls == 1
    assert rig.raw.get_calls == 2


@pytest.mark.asyncio
async def test_canceled_application_request_is_not_reported_successful() -> None:
    rig = build_cache_rig()
    rig.raw.set_mode = "delay"
    runner = RetrievalRunner(
        RetrievalGraphServices(cache=rig.cache, rag_core=build_rag_core(Knowledge()))
    )
    task = asyncio.create_task(runner.run(request("a")))
    await asyncio.wait_for(rig.raw.set_started.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    rig.raw.set_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert len(rig.raw.values) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected", "stored"),
    [
        ("store", CacheCommitOutcome.STORED, True),
        ("apply_timeout", CacheCommitOutcome.UNKNOWN, True),
        ("apply_connection", CacheCommitOutcome.UNKNOWN, True),
        ("apply_bad_ack", CacheCommitOutcome.UNKNOWN, True),
        ("reject", CacheCommitOutcome.UNKNOWN, False),
        ("unavailable_before", CacheCommitOutcome.UNKNOWN, False),
    ],
)
async def test_submitted_write_outcome_matrix(
    mode: str, expected: CacheCommitOutcome, stored: bool
) -> None:
    rig = build_cache_rig()
    rig.raw.set_mode = mode
    prepared = await rig.cache.prepare(request("a"), core_result(), web_invoked=False)
    assert prepared is not None
    assert await rig.cache.commit(prepared) is expected
    assert (len(rig.raw.values) == 1) is stored
    assert rig.raw.delete_calls == 0


@pytest.mark.asyncio
async def test_dependency_error_without_execution_detail_is_unknown() -> None:
    class Unknown:
        async def put_query(self, *args: object, **kwargs: object) -> None:
            raise DependencyUnavailableError()

    rig = build_cache_rig()
    cache = SessionQueryCache(
        redis=Unknown(),  # type: ignore[arg-type]
        uow_factory=rig.marker,  # type: ignore[arg-type]
        runtime_fingerprint="runtime",
        ttl_seconds=60,
        policy=QueryCachePolicy(enabled=True),
        clock=lambda: datetime.now(UTC),
    )
    prepared = await rig.cache.prepare(request("a"), core_result(), web_invoked=False)
    assert prepared is not None
    assert await cache.commit(prepared) is CacheCommitOutcome.UNKNOWN


@pytest.mark.asyncio
async def test_programming_error_propagates_from_commit() -> None:
    class Broken:
        async def put_query(self, *args: object, **kwargs: object) -> None:
            raise InvariantViolationError

    rig = build_cache_rig()
    cache = SessionQueryCache(
        redis=Broken(),  # type: ignore[arg-type]
        uow_factory=rig.marker,  # type: ignore[arg-type]
        runtime_fingerprint="runtime",
        ttl_seconds=60,
        policy=QueryCachePolicy(enabled=True),
        clock=lambda: datetime.now(UTC),
    )
    prepared = await rig.cache.prepare(request("a"), core_result(), web_invoked=False)
    assert prepared is not None
    with pytest.raises(InvariantViolationError):
        await cache.commit(prepared)


@pytest.mark.asyncio
async def test_ambiguous_write_is_never_deleted_by_cleanup() -> None:
    rig = build_cache_rig()
    rig.raw.set_mode = "apply_bad_ack"
    prepared = await rig.cache.prepare(request("a"), core_result(), web_invoked=False)
    assert prepared is not None
    assert await rig.cache.commit(prepared) is CacheCommitOutcome.UNKNOWN
    assert rig.raw.delete_calls == 0
    assert len(rig.raw.values) == 1


@pytest.mark.asyncio
async def test_no_write_attempt_paths_report_no_stored_outcome() -> None:
    rig = build_cache_rig()
    assert (
        await rig.cache.store(
            request("a"), core_result(text="uncited.", cited=False), web_invoked=False
        )
        is False
    )
    assert rig.raw.set_calls == 0

    marker_rig = build_cache_rig(marker_required=True)
    assert (
        await marker_rig.cache.store(request("a"), core_result(), web_invoked=False)
        is False
    )
    assert marker_rig.raw.set_calls == 0


@pytest.mark.asyncio
async def test_concurrent_same_key_misses_both_publish_and_last_value_is_valid() -> (
    None
):
    rig = build_cache_rig()
    turn = request("a")
    assert await rig.cache.lookup(turn) is None
    assert await rig.cache.lookup(turn) is None
    assert rig.raw.set_calls == 0
    first = await rig.cache.prepare(
        turn, core_result(text="first [L1]."), web_invoked=False
    )
    second = await rig.cache.prepare(
        turn, core_result(text="second [L1]."), web_invoked=False
    )
    assert first is not None and second is not None

    outcomes = await asyncio.gather(rig.cache.commit(first), rig.cache.commit(second))

    assert set(outcomes) == {CacheCommitOutcome.STORED}
    assert len(rig.raw.values) == 1
    hit = await rig.cache.lookup(turn)
    assert hit is not None and hit.answer_text in {"first [L1].", "second [L1]."}
    assert rig.raw.delete_calls == 0


@pytest.mark.asyncio
async def test_canceled_same_key_request_does_not_delete_other_valid_entry() -> None:
    rig = build_cache_rig()
    turn = request("a")
    canceled = await rig.cache.prepare(
        turn, core_result(text="canceled [L1]."), web_invoked=False
    )
    valid = await rig.cache.prepare(
        turn, core_result(text="valid [L1]."), web_invoked=False
    )
    assert canceled is not None and valid is not None
    assert await rig.cache.commit(valid) is CacheCommitOutcome.STORED
    task = asyncio.create_task(rig.cache.commit(canceled))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    hit = await rig.cache.lookup(turn)
    assert hit is not None and hit.answer_text == "valid [L1]."
    assert rig.raw.delete_calls == 0


@pytest.mark.asyncio
async def test_ambiguous_same_key_write_preserves_other_valid_entry() -> None:
    rig = build_cache_rig()
    turn = request("a")
    first = await rig.cache.prepare(
        turn, core_result(text="ambiguous [L1]."), web_invoked=False
    )
    second = await rig.cache.prepare(
        turn, core_result(text="valid [L1]."), web_invoked=False
    )
    assert first is not None and second is not None
    rig.raw.set_modes = ["apply_timeout", "store"]

    outcomes = await asyncio.gather(rig.cache.commit(first), rig.cache.commit(second))

    assert set(outcomes) == {
        CacheCommitOutcome.UNKNOWN,
        CacheCommitOutcome.STORED,
    }
    assert len(rig.raw.values) == 1
    hit = await rig.cache.lookup(turn)
    assert hit is not None
    assert rig.raw.delete_calls == 0


@pytest.mark.asyncio
async def test_concurrent_different_sessions_do_not_leak_values() -> None:
    rig = build_cache_rig()
    first = await rig.cache.prepare(
        request("a"), core_result(text="A [L1]."), web_invoked=False
    )
    second = await rig.cache.prepare(
        request("b"), core_result(text="B [L1]."), web_invoked=False
    )
    assert first is not None and second is not None

    await asyncio.gather(rig.cache.commit(first), rig.cache.commit(second))

    hit_a = await rig.cache.lookup(request("a"))
    hit_b = await rig.cache.lookup(request("b"))
    assert hit_a is not None and hit_a.answer_text == "A [L1]."
    assert hit_b is not None and hit_b.answer_text == "B [L1]."
    assert len(rig.raw.values) == 2
    assert query_cache_key("a", "runtime", "hash") != query_cache_key(
        "b", "runtime", "hash"
    )


@pytest.mark.asyncio
async def test_incomplete_results_never_publish_under_concurrency() -> None:
    rig = build_cache_rig()
    turn = request("a")

    async def attempt() -> bool:
        return await rig.cache.store(
            turn,
            core_result(text="uncited.", cited=False),
            web_invoked=False,
        )

    assert await asyncio.gather(*(attempt() for _ in range(3))) == [
        False,
        False,
        False,
    ]
    assert rig.raw.set_calls == 0
    assert rig.raw.values == {}


@pytest.mark.asyncio
async def test_stalled_write_cannot_return_a_stored_outcome() -> None:
    rig = build_cache_rig()
    rig.raw.set_mode = "delay"
    prepared = await rig.cache.prepare(request("a"), core_result(), web_invoked=False)
    assert prepared is not None
    task = asyncio.create_task(rig.cache.commit(prepared))
    await asyncio.wait_for(rig.raw.set_started.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    rig.raw.set_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert len(rig.raw.values) == 1
    assert rig.raw.delete_calls == 0
