"""Injected application runner for one Web evidence acquisition."""

from __future__ import annotations

from atlasrag.domain.web import WebEvidenceRequest, WebEvidenceResult
from atlasrag.graphs.web_evidence.subgraph import (
    WebEvidenceServices,
    build_web_evidence_subgraph,
)


class WebEvidenceRunner:
    def __init__(self, services: WebEvidenceServices) -> None:
        self._graph = build_web_evidence_subgraph(services)

    async def run(self, request: WebEvidenceRequest) -> WebEvidenceResult:
        request = WebEvidenceRequest.model_validate(request)
        output = await self._graph.ainvoke({"request": request})
        return WebEvidenceResult.model_validate(output["result"])
