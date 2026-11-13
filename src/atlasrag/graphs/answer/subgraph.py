"""Thin answer_subgraph with bounded protocol regeneration."""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from atlasrag.domain.answer import (
    GenerationResult,
    OutputReviewResult,
    WorkingEvidenceBundle,
)
from atlasrag.domain.enums import (
    AnswerExecutionOutcome,
    OutputReviewDecision,
    OutputReviewReasonCode,
)
from atlasrag.domain.results import FinalResponse
from atlasrag.graphs.state.answer import (
    AnswerState,
    AnswerSubgraphInput,
    AnswerSubgraphOutput,
)
from atlasrag.services.answer.citations import CitationResolver
from atlasrag.services.answer.compression import (
    CompressionError,
    CompressionStrategy,
)
from atlasrag.services.answer.generation_input import GenerationInputBuilder
from atlasrag.services.answer.ports import (
    GenerationProtocolError,
    GenerationProvider,
)
from atlasrag.services.answer.profile import AnswerProfile
from atlasrag.services.answer.review import OutputReviewService

_SAFE_FAILURE_TEXT = "Unable to produce a verified answer from the supplied evidence."


@dataclass(frozen=True)
class AnswerServices:
    compression: CompressionStrategy
    builder: GenerationInputBuilder
    generation: GenerationProvider
    review: OutputReviewService
    resolver: CitationResolver
    profile: AnswerProfile


def build_answer_subgraph(
    services: AnswerServices,
) -> CompiledStateGraph[AnswerState, None, AnswerSubgraphInput, AnswerSubgraphOutput]:
    async def optional_compression(state: AnswerState) -> dict[str, object]:
        try:
            working = await services.compression.prepare(state["input"])
        except CompressionError:
            working = WorkingEvidenceBundle(
                local_evidence=state["input"].local_evidence,
                web_evidence=state["input"].web_evidence,
            )
        return {"working_evidence": working}

    async def generate_answer(state: AnswerState) -> dict[str, object]:
        attempt = int(state.get("generation_attempt_index", 0)) + 1
        generation_input = services.builder.build(
            state["input"], state["working_evidence"]
        )
        try:
            result = await services.generation.generate(generation_input)
        except GenerationProtocolError:
            return {
                "generation_attempt_index": attempt,
                "review_result": OutputReviewResult(
                    decision=OutputReviewDecision.REGENERATE,
                    reason_codes=(OutputReviewReasonCode.INVALID_OUTPUT_SCHEMA,),
                ),
            }
        return {
            "generation_attempt_index": attempt,
            "generation_result": result,
            # Placeholder so a later review recomputes; a protocol failure
            # installs INVALID_OUTPUT_SCHEMA instead and suppresses stale review.
            "review_result": OutputReviewResult(decision=OutputReviewDecision.ACCEPT),
        }

    async def review_output(state: AnswerState) -> dict[str, object]:
        existing = state.get("review_result")
        if isinstance(existing, OutputReviewResult) and any(
            code is OutputReviewReasonCode.INVALID_OUTPUT_SCHEMA
            for code in existing.reason_codes
        ):
            return {}
        generation = state.get("generation_result")
        if not isinstance(generation, GenerationResult):
            raise ValueError("review requires a validated GenerationResult")
        result = await services.review.review(generation, state["input"])
        return {"review_result": result}

    def after_review(state: AnswerState) -> str:
        review = state["review_result"]
        if review.decision is OutputReviewDecision.ACCEPT:
            return "resolve_citations"
        if review.decision is OutputReviewDecision.REGENERATE:
            attempt = int(state.get("generation_attempt_index", 0))
            if attempt <= services.profile.max_regenerations:
                return "generate_answer"
        return "safe_failure"

    async def resolve_citations(state: AnswerState) -> dict[str, object]:
        generation = state.get("generation_result")
        if not isinstance(generation, GenerationResult):
            raise ValueError("citation resolution requires GenerationResult")
        response = services.resolver.resolve(generation, state["input"])
        return {
            "final_response": response,
            "outcome": AnswerExecutionOutcome.SUCCESS,
        }

    async def safe_failure(state: AnswerState) -> dict[str, object]:
        return {
            "final_response": FinalResponse(
                answer_text=_SAFE_FAILURE_TEXT, citations=()
            ),
            "outcome": AnswerExecutionOutcome.SAFE_FAILURE,
        }

    graph = StateGraph(
        AnswerState,
        input_schema=AnswerSubgraphInput,
        output_schema=AnswerSubgraphOutput,
    )
    graph.add_node("optional_compression", optional_compression)
    graph.add_node("generate_answer", generate_answer)
    graph.add_node("review_output", review_output)
    graph.add_node("resolve_citations", resolve_citations)
    graph.add_node("safe_failure", safe_failure)
    graph.add_edge(START, "optional_compression")
    graph.add_edge("optional_compression", "generate_answer")
    graph.add_edge("generate_answer", "review_output")
    graph.add_conditional_edges(
        "review_output",
        after_review,
        {
            "resolve_citations": "resolve_citations",
            "generate_answer": "generate_answer",
            "safe_failure": "safe_failure",
        },
    )
    graph.add_edge("resolve_citations", END)
    graph.add_edge("safe_failure", END)
    return graph.compile()
