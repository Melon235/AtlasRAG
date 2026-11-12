"""Injected application runner for the Answer subgraph."""

from __future__ import annotations

from atlasrag.domain.answer import AnswerInput
from atlasrag.domain.enums import AnswerExecutionOutcome
from atlasrag.domain.results import FinalResponse
from atlasrag.graphs.answer.subgraph import AnswerServices, build_answer_subgraph


class AnswerRunner:
    def __init__(self, services: AnswerServices) -> None:
        self._graph = build_answer_subgraph(services)

    async def run(
        self, input: AnswerInput
    ) -> tuple[FinalResponse, AnswerExecutionOutcome]:
        input = AnswerInput.model_validate(input)
        output = await self._graph.ainvoke({"input": input})
        return (
            FinalResponse.model_validate(output["final_response"]),
            AnswerExecutionOutcome(output["outcome"]),
        )
