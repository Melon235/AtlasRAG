"""A-R4 Query Cache identity, eligibility and prepare boundary."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cache_support import (
    build_cache_rig,
    cached_value,
    core_result,
    final_response,
    prepare,
    request,
)

from atlasrag.application.hashing import request_hash
from atlasrag.domain.enums import RagCompletionStatus
from atlasrag.domain.requests import QueryFilters, UserTurnRequest
from atlasrag.domain.results import RagCoreResult
from atlasrag.providers.cache.keys import parent_cache_key, query_cache_key
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.cache.session import CacheCommitOutcome, SessionQueryCache


@pytest.mark.asyncio
async def test_same_session_exact_request_hits_and_other_sessions_are_independent() -> (
    None
):
    rig = build_cache_rig()
    session_a = request("session-a", "question")
    session_b = request("session-b", "question")

    assert await rig.cache.lookup(session_a) is None
    assert await rig.cache.store(
        session_a, core_result(text="A [L1]."), web_invoked=False
    )
    assert await rig.cache.lookup(session_b) is None
    assert await rig.cache.store(
        session_b, core_result(text="B [L1]."), web_invoked=False
    )

    hit_a = await rig.cache.lookup(session_a)
    hit_b = await rig.cache.lookup(session_b)
    assert hit_a is not None and hit_a.answer_text == "A [L1]."
    assert hit_b is not None and hit_b.answer_text == "B [L1]."
    assert rig.raw.set_calls == 2
    assert len(rig.raw.values) == 2
    assert request_hash(session_a) == request_hash(session_b)


@pytest.mark.asyncio
async def test_changed_query_and_changed_filters_have_distinct_identity() -> None:
    rig = build_cache_rig()
    base = request("session", "question")
    changed_query = request("session", "other question")
    changed_filter = UserTurnRequest(
        session_id="session",
        query="question",
        filters=QueryFilters(file_name="guide.md"),
    )
    other_filter = UserTurnRequest(
        session_id="session",
        query="question",
        filters=QueryFilters(file_name="other.md"),
    )
    hashes = {
        request_hash(base),
        request_hash(changed_query),
        request_hash(changed_filter),
        request_hash(other_filter),
    }
    assert len(hashes) == 4

    for turn, text in (
        (base, "base [L1]."),
        (changed_query, "query [L1]."),
        (changed_filter, "filter [L1]."),
        (other_filter, "other [L1]."),
    ):
        assert await rig.cache.store(turn, core_result(text=text), web_invoked=False)
        hit = await rig.cache.lookup(turn)
        assert hit is not None and hit.answer_text == text
    assert len(rig.raw.values) == 4


@pytest.mark.asyncio
async def test_runtime_fingerprint_is_part_of_identity() -> None:
    rig = build_cache_rig()
    turn = request("session", "question")
    other = SessionQueryCache(
        redis=rig.provider,
        uow_factory=rig.marker,  # type: ignore[arg-type]
        runtime_fingerprint="other-runtime",
        ttl_seconds=60,
        policy=QueryCachePolicy(enabled=True),
        clock=lambda: datetime.now(UTC),
    )
    assert await rig.cache.store(turn, core_result(text="one [L1]."), web_invoked=False)
    assert await other.store(turn, core_result(text="two [L1]."), web_invoked=False)

    first = await rig.cache.lookup(turn)
    second = await other.lookup(turn)
    assert first is not None and first.answer_text == "one [L1]."
    assert second is not None and second.answer_text == "two [L1]."
    assert query_cache_key("session", "runtime", request_hash(turn)) in rig.raw.values
    assert (
        query_cache_key("session", "other-runtime", request_hash(turn))
        in rig.raw.values
    )


@pytest.mark.asyncio
async def test_prepare_does_not_submit_a_redis_write() -> None:
    rig = build_cache_rig()
    turn = request("session", "question")

    prepared = await prepare(rig, turn, result=core_result())

    assert prepared is not None
    assert rig.raw.set_calls == 0
    assert rig.raw.values == {}
    assert await rig.cache.commit(prepared) is CacheCommitOutcome.STORED
    assert rig.raw.set_calls == 1


@pytest.mark.asyncio
async def test_completed_local_result_is_the_only_eligible_result() -> None:
    eligible = core_result(text="eligible [L1].")
    ineligible = (
        core_result(text="degraded [L1].", status=RagCompletionStatus.DEGRADED_SUCCESS),
        core_result(text="safe [L1].", status=RagCompletionStatus.SAFE_FAILURE),
        core_result(text="uncited.", cited=False),
    )
    rig = build_cache_rig()
    turn = request("session", "question")
    assert await rig.cache.prepare(turn, eligible, web_invoked=False) is not None
    for result in ineligible:
        assert await rig.cache.prepare(turn, result, web_invoked=False) is None
    assert rig.raw.set_calls == 0


@pytest.mark.asyncio
async def test_web_provenance_time_sensitivity_and_oversize_bypass() -> None:
    rig = build_cache_rig(max_entry_bytes=10)
    turn = request("session", "question")
    assert await rig.cache.prepare(turn, core_result(), web_invoked=True) is None
    assert await rig.cache.prepare(turn, core_result(), web_invoked=None) is None
    time_sensitive = request("session", "latest status today")
    assert (
        await rig.cache.prepare(time_sensitive, core_result(), web_invoked=False)
        is None
    )
    assert await rig.cache.prepare(turn, core_result(), web_invoked=False) is None
    assert rig.raw.set_calls == 0


@pytest.mark.asyncio
async def test_web_derived_citation_is_not_cacheable() -> None:
    rig = build_cache_rig()
    web_derived = RagCoreResult(
        final_response=final_response("web [W1]."),
        completion_status=RagCompletionStatus.SUCCESS,
    )
    web_derived = web_derived.model_copy(
        update={
            "final_response": web_derived.final_response.model_copy(
                update={
                    "citations": (
                        web_derived.final_response.citations[0].model_copy(
                            update={"citation_id": "W1"}
                        ),
                    )
                }
            )
        }
    )
    prepared = await rig.cache.prepare(
        request("session", "question"), web_derived, web_invoked=False
    )
    assert prepared is None
    assert rig.raw.set_calls == 0


@pytest.mark.asyncio
async def test_time_sensitive_lookup_bypasses_redis() -> None:
    rig = build_cache_rig()
    turn = request("session", "latest status today")
    stale = cached_value("stale [L1].")
    rig.raw.values[query_cache_key("session", "runtime", request_hash(turn))] = (
        stale.model_dump_json()
    )
    assert await rig.cache.lookup(turn) is None
    assert rig.raw.get_calls == 0


@pytest.mark.asyncio
async def test_disabled_cache_performs_no_redis_operations_and_freshness_still_runs() -> (
    None
):
    rig = build_cache_rig(enabled=False, marker_required=True)
    turn = request("session", "question")
    stale = cached_value("stale [L1].")
    rig.raw.values[query_cache_key("session", "runtime", request_hash(turn))] = (
        stale.model_dump_json()
    )

    assert await rig.cache.lookup(turn) is None
    assert await rig.cache.store(turn, core_result(), web_invoked=False) is False
    assert rig.raw.get_calls == 0
    assert rig.raw.set_calls == 0
    assert rig.marker.required is True

    assert (await rig.barrier.enforce()).value == "TRUSTED"
    assert rig.marker.required is False
    assert rig.raw.values == {}

    enabled = SessionQueryCache(
        redis=rig.provider,
        uow_factory=rig.marker,
        runtime_fingerprint="runtime",
        ttl_seconds=60,
        policy=QueryCachePolicy(enabled=True),
        clock=lambda: datetime.now(UTC),
    )
    assert await enabled.lookup(turn) is None
    assert await enabled.store(turn, core_result(), web_invoked=False) is True
    assert (await enabled.lookup(turn)) is not None


def test_frozen_key_contract_and_session_isolated_identity() -> None:
    assert (
        query_cache_key("session", "fingerprint", "hash")
        == "atlasrag:query:session:fingerprint:hash"
    )
    assert parent_cache_key("revision", "parent") == "atlasrag:parent:revision:parent"
    first = request("session-a", "question")
    second = request("session-b", "question")
    assert request_hash(first) == request_hash(second)
    assert query_cache_key(
        "session-a", "runtime", request_hash(first)
    ) != query_cache_key("session-b", "runtime", request_hash(second))
