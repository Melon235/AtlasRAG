"""Injected application runner for the retrieval_graph wrapper."""

from __future__ import annotations

from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse
from atlasrag.graphs.retrieval.graph import (
    RetrievalGraphServices,
    build_retrieval_graph,
)


class RetrievalRunner:
    def __init__(self, services: RetrievalGraphServices) -> None:
        self._graph = build_retrieval_graph(services)

    async def run(self, request: UserTurnRequest) -> FinalResponse:
        request = UserTurnRequest.model_validate(request)
        output = await self._graph.ainvoke({"request": request})
        return FinalResponse.model_validate(output["final_response"])
