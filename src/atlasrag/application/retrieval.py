"""Injected application runner for the retrieval_graph wrapper."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from atlasrag.domain.observation import current_invocation_id, observation_invocation
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse
from atlasrag.graphs.retrieval.graph import (
    RetrievalGraphServices,
    build_retrieval_graph,
)
from atlasrag.services.cache.session import QueryCacheStatus


@dataclass(frozen=True)
class RetrievalExecution:
    """Final validated response paired with truthful cache lookup status."""

    response: FinalResponse
    cache_status: QueryCacheStatus


class RetrievalRunner:
    def __init__(self, services: RetrievalGraphServices) -> None:
        self._services = services

    async def run_with_facts(self, request: UserTurnRequest) -> RetrievalExecution:
        """Execute retrieval_graph and return both FinalResponse and cache lookup status."""

        request = UserTurnRequest.model_validate(request)
        facts: dict[str, bool] = {}
        cache_facts: dict[str, str] = {}
        graph = build_retrieval_graph(
            self._services,
            execution_facts=facts,
            cache_facts=cache_facts,
        )
        if current_invocation_id() is not None:
            output = await graph.ainvoke({"request": request})
        else:
            with observation_invocation(uuid4().hex):
                output = await graph.ainvoke({"request": request})
        return RetrievalExecution(
            response=FinalResponse.model_validate(output["final_response"]),
            cache_status=QueryCacheStatus(
                cache_facts.get("cache_status", QueryCacheStatus.UNKNOWN)
            ),
        )

    async def run(self, request: UserTurnRequest) -> FinalResponse:
        return (await self.run_with_facts(request)).response
