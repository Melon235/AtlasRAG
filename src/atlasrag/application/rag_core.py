"""Injected application runner for one RagCore invocation."""

from __future__ import annotations

from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import RagCoreResult
from atlasrag.graphs.rag_core.subgraph import RagCoreServices, build_rag_core_subgraph


class RagCoreRunner:
    def __init__(self, services: RagCoreServices) -> None:
        self._graph = build_rag_core_subgraph(services)

    async def run(self, request: UserTurnRequest) -> RagCoreResult:
        request = UserTurnRequest.model_validate(request)
        output = await self._graph.ainvoke({"request": request})
        return RagCoreResult.model_validate(output["result"])
