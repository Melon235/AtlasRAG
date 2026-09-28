"""Regression tests for validation of nested domain-model instances."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import BaseModel, ValidationError, create_model

from atlasrag.domain.answer import (
    AnswerInput,
    AnswerLocalEvidence,
    AnswerWebEvidence,
    WorkingEvidenceBundle,
)
from atlasrag.domain.enums import (
    EvidenceType,
    LocalAnswerEvidenceStatus,
    RagCompletionStatus,
    SourceKind,
    WebEvidenceRepresentation,
)
from atlasrag.domain.evidence import LocalEvidence, LocalProvenance
from atlasrag.domain.results import (
    FinalLocalCitation,
    FinalResponse,
    FinalWebCitation,
    RagCoreResult,
)
from atlasrag.domain.web import WebEvidence


def _local_evidence() -> LocalEvidence:
    return LocalEvidence(
        evidence_type=EvidenceType.TEXT_PARENT,
        context_id="parent-1",
        document_id="document-1",
        revision_id="revision-1",
        content="Authoritative local context.",
        provenance=LocalProvenance(
            file_name="guide.md",
            relative_source_path="docs/guide.md",
        ),
    )


def _web_evidence() -> WebEvidence:
    return WebEvidence.model_validate(
        {
            "title": "AtlasRAG documentation",
            "url": "https://docs.example.com/atlasrag",
            "domain": "docs.example.com",
            "search_rank": 1,
            "content": "Grounded Web evidence.",
            "representation": WebEvidenceRepresentation.SUMMARY,
        }
    )


def _local_wrapper() -> AnswerLocalEvidence:
    return AnswerLocalEvidence(citation_id="L1", evidence=_local_evidence())


def _web_wrapper() -> AnswerWebEvidence:
    return AnswerWebEvidence(citation_id="W1", evidence=_web_evidence())


def _final_local_citation() -> FinalLocalCitation:
    return FinalLocalCitation(
        citation_id="L1",
        file_name="guide.md",
        relative_source_path="docs/guide.md",
        evidence_type=EvidenceType.TEXT_PARENT,
    )


def _final_web_citation() -> FinalWebCitation:
    return FinalWebCitation.model_validate(
        {
            "citation_id": "W1",
            "title": "AtlasRAG documentation",
            "url": "https://docs.example.com/atlasrag",
            "domain": "docs.example.com",
        }
    )


@pytest.mark.parametrize(
    "container_factory",
    (
        lambda wrapper: AnswerInput(
            original_query="question",
            local_evidence_status=LocalAnswerEvidenceStatus.SUFFICIENT,
            local_evidence=(wrapper,),
        ),
        lambda wrapper: WorkingEvidenceBundle(local_evidence=(wrapper,)),
    ),
    ids=("answer-input", "working-bundle"),
)
def test_answer_containers_reject_constructed_wrapper_with_web_id(
    container_factory: Callable[[AnswerLocalEvidence], BaseModel],
) -> None:
    forged_wrapper = AnswerLocalEvidence.model_construct(
        citation_id="W7",
        evidence=_local_evidence(),
    )

    with pytest.raises(ValidationError, match="citation_id"):
        container_factory(forged_wrapper)


@pytest.mark.parametrize(
    "container_factory",
    (
        lambda wrapper: AnswerInput(
            original_query="question",
            local_evidence_status=LocalAnswerEvidenceStatus.SUFFICIENT,
            local_evidence=(wrapper,),
        ),
        lambda wrapper: WorkingEvidenceBundle(local_evidence=(wrapper,)),
    ),
    ids=("answer-input", "working-bundle"),
)
def test_answer_containers_reject_constructed_wrapper_with_web_evidence(
    container_factory: Callable[[AnswerLocalEvidence], BaseModel],
) -> None:
    forged_wrapper = AnswerLocalEvidence.model_construct(
        citation_id="L7",
        evidence=_web_evidence(),
    )

    with pytest.raises(ValidationError, match="evidence"):
        container_factory(forged_wrapper)


def test_final_response_rejects_constructed_local_citation_with_web_id() -> None:
    forged_citation = FinalLocalCitation.model_construct(
        citation_id="W7",
        source_kind=SourceKind.LOCAL,
        file_name="guide.md",
        relative_source_path="docs/guide.md",
        evidence_type=EvidenceType.TEXT_PARENT,
        source_anchor=None,
    )

    with pytest.raises(ValidationError, match="citation_id"):
        FinalResponse(answer_text="answer", citations=(forged_citation,))


def test_final_response_rejects_relaxed_local_citation_subclass() -> None:
    relaxed_citation_type = create_model(
        "RelaxedFinalLocalCitation",
        __base__=FinalLocalCitation,
        citation_id=(str, ...),
    )
    relaxed_citation = relaxed_citation_type(
        citation_id="W7",
        source_kind=SourceKind.LOCAL,
        file_name="guide.md",
        relative_source_path="docs/guide.md",
        evidence_type=EvidenceType.TEXT_PARENT,
    )

    with pytest.raises(ValidationError, match="citation_id"):
        FinalResponse.model_validate(
            {"answer_text": "answer", "citations": [relaxed_citation]}
        )


def test_rag_core_result_rejects_constructed_blank_final_response() -> None:
    forged_response = FinalResponse.model_construct(
        answer_text=" \t ",
        citations=(),
    )

    with pytest.raises(ValidationError, match="answer_text"):
        RagCoreResult(
            final_response=forged_response,
            completion_status=RagCompletionStatus.SAFE_FAILURE,
        )


def test_parent_boundary_rejects_constructed_nested_surrogate_text() -> None:
    valid = _local_evidence()
    forged = LocalEvidence.model_construct(
        **{**valid.model_dump(), "content": "invalid-\ud800-content"}
    )

    with pytest.raises(ValidationError, match="surrogate"):
        AnswerLocalEvidence(citation_id="L1", evidence=forged)


def _valid_nested_models() -> tuple[BaseModel, ...]:
    answer_input = AnswerInput.model_validate(
        {
            "original_query": "question",
            "local_evidence_status": "SUFFICIENT",
            "local_evidence": [_local_wrapper()],
            "web_evidence": [_web_wrapper()],
        }
    )
    bundle = WorkingEvidenceBundle(
        local_evidence=answer_input.local_evidence,
        web_evidence=answer_input.web_evidence,
    )
    response = FinalResponse(
        answer_text="Grounded answer.",
        citations=(_final_local_citation(), _final_web_citation()),
    )
    core_result = RagCoreResult.model_validate(
        {
            "final_response": response,
            "completion_status": "DEGRADED_SUCCESS",
        }
    )
    return answer_input, bundle, response, core_result


@pytest.mark.parametrize(
    "model",
    _valid_nested_models(),
    ids=lambda model: type(model).__name__,
)
def test_valid_nested_models_retain_semantic_json_roundtrip(model: BaseModel) -> None:
    restored = type(model).model_validate_json(model.model_dump_json())

    assert restored == model
