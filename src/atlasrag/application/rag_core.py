"""Injected application runner for one RagCore invocation."""

from __future__ import annotations

from dataclasses import dataclass

from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import RagCoreResult
from atlasrag.graphs.rag_core.subgraph import RagCoreServices, build_rag_core_subgraph


@dataclass(frozen=True)
class RagCoreExecution:
    """Internal result plus execution provenance used by the cache wrapper."""

    result: RagCoreResult
    web_invoked: bool


class RagCoreRunner:
    def __init__(self, services: RagCoreServices) -> None:
        self._graph = build_rag_core_subgraph(services)
        self._state_graph = build_rag_core_subgraph(services, expose_state=True)

    async def run(self, request: UserTurnRequest) -> RagCoreResult:
        request = UserTurnRequest.model_validate(request)
        output = await self._graph.ainvoke({"request": request})
        return RagCoreResult.model_validate(output["result"])

    async def run_with_facts(self, request: UserTurnRequest) -> RagCoreExecution:
        request = UserTurnRequest.model_validate(request)
        output = await self._state_graph.ainvoke({"request": request})
        return RagCoreExecution(
            result=RagCoreResult.model_validate(output["result"]),
            web_invoked="web_result" in output,
        )
