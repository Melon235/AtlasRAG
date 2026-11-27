"""Regressions for F-R10-02-02 key-unsafe domain-valid Session ID cache bypass."""

from __future__ import annotations

import pytest
from cache_support import (
    Knowledge,
    build_cache_rig,
    build_rag_core,
    core_result,
    request,
)

from atlasrag.application.hashing import request_hash
from atlasrag.application.retrieval import RetrievalRunner
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.graphs.retrieval.graph import RetrievalGraphServices
from atlasrag.providers.cache.keys import query_cache_key


@pytest.mark.asyncio
async def test_f_r10_02_02_safe_session_id_caching_unchanged() -> None:
    rig = build_cache_rig(require_write_fence=True)
    assert (await rig.barrier.enforce()).value == "TRUSTED"

    req = request("ordinary-user-1", "explain atlas")
    assert await rig.cache.lookup(req) is None
    assert (
        await rig.cache.store(
            req, core_result(text="cached ok [L1]."), web_invoked=False
        )
        is True
    )
    hit = await rig.cache.lookup(req)
    assert hit is not None
    assert hit.answer_text == "cached ok [L1]."
    assert (
        query_cache_key("ordinary-user-1", "runtime", request_hash(req))
        in rig.raw.values
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_session",
    [
        "tenant:user-1",
        "tenant:org:user-2",
        "session\n1",
        "session\t2",
        "session\x7f3",
    ],
)
async def test_f_r10_02_02_unsafe_domain_valid_session_id_bypasses_lookup_and_publication(
    unsafe_session: str,
) -> None:
    for cache_enabled in (False, True):
        rig = build_cache_rig(enabled=cache_enabled, require_write_fence=True)
        if cache_enabled:
            assert (await rig.barrier.enforce()).value == "TRUSTED"
        get_before = rig.raw.get_calls
        set_before = rig.raw.set_calls

        turn = UserTurnRequest(session_id=unsafe_session, query="what is atlas?")
        assert turn.session_id == unsafe_session

        # Direct lookup, prepare, and store bypass without error or Redis calls.
        assert await rig.cache.lookup(turn) is None
        prepared = await rig.cache.prepare(
            turn, core_result(text="direct [L1]."), web_invoked=False
        )
        assert prepared is None
        assert (
            await rig.cache.store(
                turn, core_result(text="direct [L1]."), web_invoked=False
            )
            is False
        )

        # Complete RAG + RetrievalRunner application execution succeeds normally.
        knowledge = Knowledge()
        rag_core = build_rag_core(knowledge)
        runner = RetrievalRunner(
            RetrievalGraphServices(cache=rig.cache, rag_core=rag_core)
        )
        response = await runner.run(turn)
        assert response.answer_text == knowledge.text
        assert rig.raw.get_calls == get_before
        assert rig.raw.set_calls == set_before
        assert rig.raw.query_values() == {}


@pytest.mark.asyncio
async def test_f_r10_02_02_multiple_distinct_unsafe_sessions_never_collide_or_touch_redis() -> (
    None
):
    rig = build_cache_rig(enabled=True, require_write_fence=True)
    assert (await rig.barrier.enforce()).value == "TRUSTED"
    knowledge = Knowledge()
    runner = RetrievalRunner(
        RetrievalGraphServices(cache=rig.cache, rag_core=build_rag_core(knowledge))
    )

    for sid in ("tenant:1", "tenant:2", "tenant:1\n"):
        resp = await runner.run(UserTurnRequest(session_id=sid, query="same question"))
        assert resp.answer_text == knowledge.text
    assert rig.raw.get_calls == 0
    assert rig.raw.set_calls == 0
    assert rig.raw.query_values() == {}
