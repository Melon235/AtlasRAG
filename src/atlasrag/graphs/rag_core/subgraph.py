"""Thin rag_core_subgraph connecting existing Part II business capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from atlasrag.application.answer import AnswerRunner
from atlasrag.application.local_evidence import LocalEvidenceRunner
from atlasrag.application.web_evidence import WebEvidenceRunner
from atlasrag.domain.enums import AnswerExecutionOutcome
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse, RagCoreResult
from atlasrag.domain.web import WebEvidenceRequest
from atlasrag.graphs.state.rag_core import RagCoreInput, RagCoreOutput, RagCoreState
from atlasrag.services.rag_core.boundary import AnswerInputBuilder
from atlasrag.services.rag_core.completion import CompletionAssessor
from atlasrag.services.rag_core.query import QueryConstructionService
from atlasrag.services.rag_core.routing import (
    CorrectiveAction,
    CorrectiveRoutingService,
)

_SAFE_FAILURE_TEXT = "Unable to answer from the available evidence."


@dataclass(frozen=True)
class RagCoreServices:
    query_construction: QueryConstructionService
    local_evidence: LocalEvidenceRunner
    routing: CorrectiveRoutingService
    web_evidence: WebEvidenceRunner
    answer_input: AnswerInputBuilder
    answer: AnswerRunner
    completion: CompletionAssessor


def build_rag_core_subgraph(
    services: RagCoreServices, *, expose_state: bool = False
) -> CompiledStateGraph[RagCoreState, None, RagCoreInput, RagCoreOutput | RagCoreState]:
    async def construct_query(state: RagCoreState) -> dict[str, object]:
        request = UserTurnRequest.model_validate(state["request"])
        constructed = await services.query_construction.construct(request)
        return {
            "base_local_request": constructed.request,
            "current_local_request": constructed.request,
            "local_attempt_index": 0,
        }

    async def local_evidence(state: RagCoreState) -> dict[str, object]:
        request = state["current_local_request"]
        attempt = int(state.get("local_attempt_index", 0)) + 1
        result = await services.local_evidence.run(request)
        return {
            "local_attempt_index": attempt,
            "local_result": result,
        }

    async def route_local_evidence(state: RagCoreState) -> dict[str, object]:
        # The conditional edge owns the decision; keep this node a thin marker.
        return {}

    def after_route(state: RagCoreState) -> str:
        decision = services.routing.decide(
            base=state["base_local_request"],
            current=state["current_local_request"],
            result=state["local_result"],
            attempt_index=state["local_attempt_index"],
        )
        return decision.action.value

    async def prepare_retry(state: RagCoreState) -> dict[str, object]:
        decision = services.routing.decide(
            base=state["base_local_request"],
            current=state["current_local_request"],
            result=state["local_result"],
            attempt_index=state["local_attempt_index"],
        )
        if (
            decision.action is not CorrectiveAction.RETRY_LOCAL
            or decision.request is None
        ):
            raise ValueError("prepare_retry requires a RETRY_LOCAL decision")
        return {"current_local_request": decision.request}

    async def web_evidence(state: RagCoreState) -> dict[str, object]:
        result = await services.web_evidence.run(
            WebEvidenceRequest(original_query=state["request"].query)
        )
        return {"web_result": result}

    async def build_answer_input(state: RagCoreState) -> dict[str, object]:
        result = services.answer_input.build(
            original_query=state["request"].query,
            local_result=state["local_result"],
            web_result=state.get("web_result"),
        )
        return {"answer_input": result}

    def after_answer_input(state: RagCoreState) -> str:
        answer_input = state["answer_input"]
        if not answer_input.local_evidence and not answer_input.web_evidence:
            return "safe_failure"
        return "answer"

    async def answer(state: RagCoreState) -> dict[str, object]:
        final_response, outcome = await services.answer.run(state["answer_input"])
        return {
            "final_response": final_response,
            "answer_outcome": outcome,
        }

    async def safe_failure(state: RagCoreState) -> dict[str, object]:
        return {
            "final_response": FinalResponse(
                answer_text=_SAFE_FAILURE_TEXT, citations=()
            ),
            "answer_outcome": AnswerExecutionOutcome.SAFE_FAILURE,
        }

    async def finalize_result(state: RagCoreState) -> dict[str, object]:
        answer_input = state["answer_input"]
        completion = services.completion.assess(
            answer_outcome=state["answer_outcome"],
            answer_input=answer_input,
            local_result=state.get("local_result"),
            web_result=state.get("web_result"),
        )
        result = RagCoreResult(
            final_response=state["final_response"],
            completion_status=completion,
        )
        return {"result": result}

    graph = StateGraph(
        RagCoreState, input_schema=RagCoreInput, output_schema=RagCoreOutput
    )
    graph.add_node("construct_query", construct_query)
    graph.add_node("local_evidence", local_evidence)
    graph.add_node("route_local_evidence", route_local_evidence)
    graph.add_node("prepare_retry", prepare_retry)
    graph.add_node("web_evidence", web_evidence)
    graph.add_node("build_answer_input", build_answer_input)
    graph.add_node("answer", answer)
    graph.add_node("safe_failure", safe_failure)
    graph.add_node("finalize_result", finalize_result)
    graph.add_edge(START, "construct_query")
    graph.add_edge("construct_query", "local_evidence")
    graph.add_edge("local_evidence", "route_local_evidence")
    graph.add_conditional_edges(
        "route_local_evidence",
        after_route,
        {
            "ANSWER": "build_answer_input",
            "RETRY_LOCAL": "prepare_retry",
            "WEB": "web_evidence",
        },
    )
    graph.add_edge("prepare_retry", "local_evidence")
    graph.add_edge("web_evidence", "build_answer_input")
    graph.add_conditional_edges(
        "build_answer_input",
        after_answer_input,
        {"answer": "answer", "safe_failure": "safe_failure"},
    )
    graph.add_edge("answer", "finalize_result")
    graph.add_edge("safe_failure", "finalize_result")
    graph.add_edge("finalize_result", END)
    return cast(
        CompiledStateGraph[
            RagCoreState, None, RagCoreInput, RagCoreOutput | RagCoreState
        ],
        graph.compile(),
    )
