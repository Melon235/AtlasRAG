"""Injected application runner for the retrieval_graph wrapper."""

from __future__ import annotations

from uuid import uuid4

from atlasrag.domain.observation import current_invocation_id, observation_invocation
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse
from atlasrag.graphs.retrieval.graph import (
    RetrievalGraphServices,
    build_retrieval_graph,
)


class RetrievalRunner:
    def __init__(self, services: RetrievalGraphServices) -> None:
        self._services = services

    async def run(self, request: UserTurnRequest) -> FinalResponse:
        request = UserTurnRequest.model_validate(request)
        facts: dict[str, bool] = {}
        graph = build_retrieval_graph(self._services, execution_facts=facts)
        if current_invocation_id() is not None:
            output = await graph.ainvoke({"request": request})
        else:
            with observation_invocation(uuid4().hex):
                output = await graph.ainvoke({"request": request})
        return FinalResponse.model_validate(output["final_response"])
