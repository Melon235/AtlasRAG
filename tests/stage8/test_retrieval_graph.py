"""Retrieval graph cache HIT/MISS/write boundary tests."""

from __future__ import annotations

from typing import Any, cast

import pytest

from atlasrag.application.rag_core import RagCoreExecution
from atlasrag.application.retrieval import RetrievalRunner
from atlasrag.domain.enums import RagCompletionStatus
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse, RagCoreResult
from atlasrag.graphs.retrieval.graph import RetrievalGraphServices


class FakeCache:
    def __init__(self, hit: FinalResponse | None = None) -> None:
        self.hit = hit
        self.lookups = 0
        self.stores = 0

    async def lookup(self, request: UserTurnRequest) -> FinalResponse | None:
        self.lookups += 1
        return self.hit

    async def store(
        self,
        request: UserTurnRequest,
        result: RagCoreResult,
        *,
        web_invoked: bool = False,
    ) -> bool:
        self.stores += 1
        return True


class FakeRagCore:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request: UserTurnRequest) -> RagCoreResult:
        self.calls += 1
        return RagCoreResult(
            final_response=FinalResponse(answer_text="generated", citations=()),
            completion_status=RagCompletionStatus.SAFE_FAILURE,
        )

    async def run_with_facts(self, request: UserTurnRequest) -> RagCoreExecution:
        return RagCoreExecution(result=await self.run(request), web_invoked=False)


@pytest.mark.asyncio
async def test_trusted_hit_skips_rag_core_and_write() -> None:
    cached = FinalResponse(answer_text="cached", citations=())
    cache = FakeCache(cached)
    rag = FakeRagCore()
    services = RetrievalGraphServices(cache=cast(Any, cache), rag_core=cast(Any, rag))
    response = await RetrievalRunner(services).run(
        UserTurnRequest(session_id="s", query="q")
    )
    assert response == cached
    assert cache.lookups == 1 and cache.stores == 0 and rag.calls == 0


@pytest.mark.asyncio
async def test_miss_executes_rag_core_then_store() -> None:
    cache = FakeCache()
    rag = FakeRagCore()
    services = RetrievalGraphServices(cache=cast(Any, cache), rag_core=cast(Any, rag))
    response = await RetrievalRunner(services).run(
        UserTurnRequest(session_id="s", query="q")
    )
    assert response.answer_text == "generated"
    assert cache.lookups == 1 and cache.stores == 1 and rag.calls == 1
