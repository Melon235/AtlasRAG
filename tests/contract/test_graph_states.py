"""Exact reflection tests for graph input, State, and output schemas."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import (
    NotRequired,
    Required,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

from atlasrag.domain.answer import (
    AnswerInput,
    GenerationResult,
    OutputReviewResult,
    WorkingEvidenceBundle,
)
from atlasrag.domain.enums import AnswerExecutionOutcome
from atlasrag.domain.evidence import EvidenceRef, LocalEvidence, LocalEvidenceResult
from atlasrag.domain.requests import LocalRetrievalRequest, UserTurnRequest
from atlasrag.domain.results import FinalResponse, RagCoreResult
from atlasrag.domain.retrieval import CandidatePool, RetrievalBranchResult
from atlasrag.domain.web import (
    PreparedWebSource,
    WebEvidenceRequest,
    WebEvidenceResult,
    WebSearchResult,
)


@dataclass(frozen=True)
class ExpectedSchema:
    """One authoritative State schema expectation."""

    module: str
    required: frozenset[str]
    optional: frozenset[str]
    annotations: dict[str, object]


PUBLIC_STATE_TYPES = (
    "RetrievalGraphInput",
    "RetrievalGraphState",
    "RetrievalGraphOutput",
    "RagCoreInput",
    "RagCoreState",
    "RagCoreOutput",
    "LocalEvidenceSubgraphInput",
    "LocalEvidenceState",
    "LocalEvidenceSubgraphOutput",
    "WebEvidenceSubgraphInput",
    "WebEvidenceState",
    "WebEvidenceSubgraphOutput",
    "AnswerSubgraphInput",
    "AnswerState",
    "AnswerSubgraphOutput",
)

EXPECTED_SCHEMAS = {
    "RetrievalGraphInput": ExpectedSchema(
        module="retrieval",
        required=frozenset({"request"}),
        optional=frozenset(),
        annotations={"request": UserTurnRequest},
    ),
    "RetrievalGraphState": ExpectedSchema(
        module="retrieval",
        required=frozenset({"request"}),
        optional=frozenset({"rag_result", "final_response"}),
        annotations={
            "request": UserTurnRequest,
            "rag_result": RagCoreResult,
            "final_response": FinalResponse,
        },
    ),
    "RetrievalGraphOutput": ExpectedSchema(
        module="retrieval",
        required=frozenset({"final_response"}),
        optional=frozenset(),
        annotations={"final_response": FinalResponse},
    ),
    "RagCoreInput": ExpectedSchema(
        module="rag_core",
        required=frozenset({"request"}),
        optional=frozenset(),
        annotations={"request": UserTurnRequest},
    ),
    "RagCoreState": ExpectedSchema(
        module="rag_core",
        required=frozenset({"request", "local_attempt_index"}),
        optional=frozenset(
            {
                "base_local_request",
                "current_local_request",
                "local_result",
                "web_result",
                "answer_input",
                "final_response",
                "answer_outcome",
                "result",
            }
        ),
        annotations={
            "request": UserTurnRequest,
            "base_local_request": LocalRetrievalRequest,
            "current_local_request": LocalRetrievalRequest,
            "local_attempt_index": int,
            "local_result": LocalEvidenceResult,
            "web_result": WebEvidenceResult,
            "answer_input": AnswerInput,
            "final_response": FinalResponse,
            "answer_outcome": AnswerExecutionOutcome,
            "result": RagCoreResult,
        },
    ),
    "RagCoreOutput": ExpectedSchema(
        module="rag_core",
        required=frozenset({"result"}),
        optional=frozenset(),
        annotations={"result": RagCoreResult},
    ),
    "LocalEvidenceSubgraphInput": ExpectedSchema(
        module="local_evidence",
        required=frozenset({"request"}),
        optional=frozenset(),
        annotations={"request": LocalRetrievalRequest},
    ),
    "LocalEvidenceState": ExpectedSchema(
        module="local_evidence",
        required=frozenset({"request"}),
        optional=frozenset(
            {
                "dense_branch",
                "bm25_branch",
                "candidate_pool",
                "evidence_refs",
                "local_evidence",
                "result",
            }
        ),
        annotations={
            "request": LocalRetrievalRequest,
            "dense_branch": RetrievalBranchResult,
            "bm25_branch": RetrievalBranchResult,
            "candidate_pool": CandidatePool,
            "evidence_refs": tuple[EvidenceRef, ...],
            "local_evidence": tuple[LocalEvidence, ...],
            "result": LocalEvidenceResult,
        },
    ),
    "LocalEvidenceSubgraphOutput": ExpectedSchema(
        module="local_evidence",
        required=frozenset({"result"}),
        optional=frozenset(),
        annotations={"result": LocalEvidenceResult},
    ),
    "WebEvidenceSubgraphInput": ExpectedSchema(
        module="web_evidence",
        required=frozenset({"request"}),
        optional=frozenset(),
        annotations={"request": WebEvidenceRequest},
    ),
    "WebEvidenceState": ExpectedSchema(
        module="web_evidence",
        required=frozenset({"request"}),
        optional=frozenset({"search_results", "prepared_sources", "result"}),
        annotations={
            "request": WebEvidenceRequest,
            "search_results": tuple[WebSearchResult, ...],
            "prepared_sources": tuple[PreparedWebSource, ...],
            "result": WebEvidenceResult,
        },
    ),
    "WebEvidenceSubgraphOutput": ExpectedSchema(
        module="web_evidence",
        required=frozenset({"result"}),
        optional=frozenset(),
        annotations={"result": WebEvidenceResult},
    ),
    "AnswerSubgraphInput": ExpectedSchema(
        module="answer",
        required=frozenset({"input"}),
        optional=frozenset(),
        annotations={"input": AnswerInput},
    ),
    "AnswerState": ExpectedSchema(
        module="answer",
        required=frozenset({"input", "generation_attempt_index"}),
        optional=frozenset(
            {
                "working_evidence",
                "generation_result",
                "review_result",
                "final_response",
                "outcome",
            }
        ),
        annotations={
            "input": AnswerInput,
            "working_evidence": WorkingEvidenceBundle,
            "generation_attempt_index": int,
            "generation_result": GenerationResult,
            "review_result": OutputReviewResult,
            "final_response": FinalResponse,
            "outcome": AnswerExecutionOutcome,
        },
    ),
    "AnswerSubgraphOutput": ExpectedSchema(
        module="answer",
        required=frozenset({"final_response", "outcome"}),
        optional=frozenset(),
        annotations={
            "final_response": FinalResponse,
            "outcome": AnswerExecutionOutcome,
        },
    ),
}


def _state_package() -> ModuleType:
    return import_module("atlasrag.graphs.state")


def _schema(package: ModuleType, name: str) -> type[object]:
    return cast(type[object], getattr(package, name))


def _without_requiredness(annotation: object) -> object:
    if get_origin(annotation) in {Required, NotRequired}:
        return cast(object, get_args(annotation)[0])
    return annotation


def test_public_state_package_exports_exact_schema_surface() -> None:
    package = _state_package()

    assert cast(tuple[str, ...], package.__all__) == PUBLIC_STATE_TYPES
    assert tuple(EXPECTED_SCHEMAS) == PUBLIC_STATE_TYPES
    for name in PUBLIC_STATE_TYPES:
        assert getattr(package, name) is _schema(package, name)


def test_every_public_state_schema_is_a_total_typed_dict() -> None:
    package = _state_package()

    for name, expected in EXPECTED_SCHEMAS.items():
        schema = _schema(package, name)
        assert is_typeddict(schema), name
        assert cast(bool, getattr(schema, "__total__")) is True, name
        assert schema.__module__ == f"atlasrag.graphs.state.{expected.module}"


def test_state_schemas_have_exact_keys_and_resolved_annotations() -> None:
    package = _state_package()

    for name, expected in EXPECTED_SCHEMAS.items():
        schema = _schema(package, name)
        required = cast(frozenset[str], getattr(schema, "__required_keys__"))
        optional = cast(frozenset[str], getattr(schema, "__optional_keys__"))
        resolved = get_type_hints(schema, include_extras=True)

        assert required == expected.required, name
        assert optional == expected.optional, name
        assert set(resolved) == expected.required | expected.optional, name

        for key in expected.required:
            assert get_origin(resolved[key]) not in {Required, NotRequired}, (
                name,
                key,
            )
        for key in expected.optional:
            assert get_origin(resolved[key]) is NotRequired, (name, key)

        assert {
            key: _without_requiredness(annotation)
            for key, annotation in resolved.items()
        } == expected.annotations, name
