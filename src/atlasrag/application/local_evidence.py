"""Injected application runner for one local evidence attempt."""

from __future__ import annotations

from atlasrag.domain.evidence import LocalEvidenceResult
from atlasrag.domain.requests import LocalRetrievalRequest
from atlasrag.graphs.local_evidence.subgraph import build_local_evidence_subgraph
from atlasrag.services.local_evidence.service import LocalEvidenceServices


class LocalEvidenceRunner:
    def __init__(self, services: LocalEvidenceServices) -> None:
        self._graph = build_local_evidence_subgraph(services)

    async def run(self, request: LocalRetrievalRequest) -> LocalEvidenceResult:
        request = LocalRetrievalRequest.model_validate(request)
        output = await self._graph.ainvoke({"request": request})
        return LocalEvidenceResult.model_validate(output["result"])
