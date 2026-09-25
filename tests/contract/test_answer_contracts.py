"""Contract tests for the Evidence-to-Answer boundary and answer outputs."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import BaseModel, ValidationError

from atlasrag.domain.answer import (
    AnswerInput,
    AnswerLocalEvidence,
    AnswerWebEvidence,
    GenerationResult,
    OutputReviewResult,
    WorkingEvidenceBundle,
)
from atlasrag.domain.enums import (
    EvidenceType,
    LocalAnswerEvidenceStatus,
    OutputReviewDecision,
    OutputReviewReasonCode,
    WebEvidenceRepresentation,
)
from atlasrag.domain.evidence import LocalEvidence, LocalProvenance
from atlasrag.domain.web import WebEvidence


def _local_evidence(*, context_id: str = "parent-1") -> LocalEvidence:
    return LocalEvidence(
        evidence_type=EvidenceType.TEXT_PARENT,
        context_id=context_id,
        document_id="document-1",
        revision_id="revision-1",
        content="Authoritative local context.",
        provenance=LocalProvenance(
            file_name="guide.md",
            relative_source_path="docs/guide.md",
        ),
    )


def _web_evidence(*, search_rank: int = 1) -> WebEvidence:
    return WebEvidence.model_validate(
        {
            "title": "AtlasRAG documentation",
            "url": "https://docs.example.com/atlasrag",
            "domain": "docs.example.com",
            "search_rank": search_rank,
            "content": "Grounded Web evidence.",
            "representation": WebEvidenceRepresentation.SUMMARY,
        }
    )


def _local_wrapper(
    *, citation_id: str = "L1", context_id: str = "parent-1"
) -> AnswerLocalEvidence:
    return AnswerLocalEvidence(
        citation_id=citation_id,
        evidence=_local_evidence(context_id=context_id),
    )


def _web_wrapper(*, citation_id: str = "W1", search_rank: int = 1) -> AnswerWebEvidence:
    return AnswerWebEvidence(
        citation_id=citation_id,
        evidence=_web_evidence(search_rank=search_rank),
    )


def _unordered_collection(kind: str, values: tuple[object, ...]) -> object:
    if kind == "set":
        return set(values)
    if kind == "frozenset":
        return frozenset(values)
    if kind == "mapping":
        return dict.fromkeys(values)
    if kind == "generator":
        return (value for value in values)
    raise AssertionError(f"unsupported unordered collection kind: {kind}")


def _assert_extra_field_rejected(
    model_type: type[BaseModel], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        model_type.model_validate({**payload, "unexpected": True})

    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"


def _assert_frozen(model_factory: Callable[[], BaseModel], field_name: str) -> None:
    model = model_factory()

    with pytest.raises(ValidationError) as exc_info:
        setattr(model, field_name, "changed")

    assert exc_info.value.errors()[0]["type"] == "frozen_instance"


def test_answer_boundary_models_have_exact_field_sets() -> None:
    assert tuple(AnswerLocalEvidence.model_fields) == ("citation_id", "evidence")
    assert tuple(AnswerWebEvidence.model_fields) == ("citation_id", "evidence")
    assert tuple(AnswerInput.model_fields) == (
        "original_query",
        "local_evidence_status",
        "local_evidence",
        "web_evidence",
    )
    assert tuple(WorkingEvidenceBundle.model_fields) == (
        "local_evidence",
        "web_evidence",
    )


@pytest.mark.parametrize("citation_id", ("L1", "L9", "L10", "L999"))
def test_answer_local_evidence_accepts_only_canonical_local_ids(
    citation_id: str,
) -> None:
    wrapper = _local_wrapper(citation_id=citation_id)

    assert wrapper.citation_id == citation_id


@pytest.mark.parametrize(
    "citation_id",
    ("", "L0", "L01", "L001", "l1", " L1", "L1 ", "W1", "L1\n", b"L1"),
)
def test_answer_local_evidence_rejects_noncanonical_local_ids(
    citation_id: object,
) -> None:
    with pytest.raises(ValidationError, match="citation_id"):
        AnswerLocalEvidence.model_validate(
            {"citation_id": citation_id, "evidence": _local_evidence()}
        )


@pytest.mark.parametrize("citation_id", ("W1", "W9", "W10", "W999"))
def test_answer_web_evidence_accepts_only_canonical_web_ids(
    citation_id: str,
) -> None:
    wrapper = _web_wrapper(citation_id=citation_id)

    assert wrapper.citation_id == citation_id


@pytest.mark.parametrize(
    "citation_id",
    ("", "W0", "W01", "W001", "w1", " W1", "W1 ", "L1", "W1\n", b"W1"),
)
def test_answer_web_evidence_rejects_noncanonical_web_ids(
    citation_id: object,
) -> None:
    with pytest.raises(ValidationError, match="citation_id"):
        AnswerWebEvidence.model_validate(
            {"citation_id": citation_id, "evidence": _web_evidence()}
        )


def test_answer_input_preserves_a_nonblank_original_query() -> None:
    answer_input = AnswerInput(
        original_query="  What is AtlasRAG?  ",
        local_evidence_status=LocalAnswerEvidenceStatus.NONE,
    )

    assert answer_input.original_query == "  What is AtlasRAG?  "
    assert answer_input.local_evidence == ()
    assert answer_input.web_evidence == ()


@pytest.mark.parametrize("blank", ("", " ", "\t\n"))
def test_answer_input_rejects_blank_original_query(blank: str) -> None:
    with pytest.raises(ValidationError, match="original_query"):
        AnswerInput(
            original_query=blank,
            local_evidence_status=LocalAnswerEvidenceStatus.NONE,
        )


def test_answer_input_rejects_byte_query_and_status() -> None:
    with pytest.raises(ValidationError, match="original_query"):
        AnswerInput.model_validate(
            {
                "original_query": b"question",
                "local_evidence_status": "NONE",
            }
        )

    with pytest.raises(ValidationError, match="local_evidence_status"):
        AnswerInput.model_validate(
            {
                "original_query": "question",
                "local_evidence_status": b"NONE",
            }
        )


def test_answer_input_accepts_legitimate_json_enum_and_array_values() -> None:
    restored = AnswerInput.model_validate_json(
        '{"original_query":"  raw question  ",'
        '"local_evidence_status":"NONE",'
        '"local_evidence":[],"web_evidence":[]}'
    )

    assert restored.original_query == "  raw question  "
    assert restored.local_evidence_status is LocalAnswerEvidenceStatus.NONE
    assert restored.local_evidence == ()
    assert restored.web_evidence == ()


def test_ambiguous_cannot_cross_the_answer_boundary() -> None:
    assert "AMBIGUOUS" not in {status.value for status in LocalAnswerEvidenceStatus}

    with pytest.raises(ValidationError, match="local_evidence_status"):
        AnswerInput.model_validate(
            {
                "original_query": "question",
                "local_evidence_status": "AMBIGUOUS",
            }
        )


@pytest.mark.parametrize(
    ("status", "local_evidence"),
    (
        (LocalAnswerEvidenceStatus.SUFFICIENT, (_local_wrapper(),)),
        (
            LocalAnswerEvidenceStatus.SUFFICIENT,
            (
                _local_wrapper(citation_id="L1", context_id="parent-1"),
                _local_wrapper(citation_id="L2", context_id="parent-2"),
            ),
        ),
        (LocalAnswerEvidenceStatus.INSUFFICIENT_BEST_MATCH, (_local_wrapper(),)),
        (LocalAnswerEvidenceStatus.NONE, ()),
    ),
)
def test_answer_input_accepts_frozen_local_status_cardinalities(
    status: LocalAnswerEvidenceStatus,
    local_evidence: tuple[AnswerLocalEvidence, ...],
) -> None:
    answer_input = AnswerInput(
        original_query="question",
        local_evidence_status=status,
        local_evidence=local_evidence,
    )

    assert answer_input.local_evidence == local_evidence
    assert isinstance(answer_input.local_evidence, tuple)


@pytest.mark.parametrize(
    ("status", "local_evidence"),
    (
        (LocalAnswerEvidenceStatus.SUFFICIENT, ()),
        (LocalAnswerEvidenceStatus.INSUFFICIENT_BEST_MATCH, ()),
        (
            LocalAnswerEvidenceStatus.INSUFFICIENT_BEST_MATCH,
            (
                _local_wrapper(citation_id="L1", context_id="parent-1"),
                _local_wrapper(citation_id="L2", context_id="parent-2"),
            ),
        ),
        (LocalAnswerEvidenceStatus.NONE, (_local_wrapper(),)),
    ),
)
def test_answer_input_rejects_invalid_local_status_cardinalities(
    status: LocalAnswerEvidenceStatus,
    local_evidence: tuple[AnswerLocalEvidence, ...],
) -> None:
    with pytest.raises(ValidationError, match="local_evidence"):
        AnswerInput(
            original_query="question",
            local_evidence_status=status,
            local_evidence=local_evidence,
        )


@pytest.mark.parametrize("model_name", ("answer_input", "working_bundle"))
@pytest.mark.parametrize("field_name", ("local_evidence", "web_evidence"))
@pytest.mark.parametrize("unordered_kind", ("set", "frozenset", "mapping", "generator"))
def test_answer_evidence_collections_reject_unordered_python_inputs(
    model_name: str,
    field_name: str,
    unordered_kind: str,
) -> None:
    values: tuple[object, ...]
    if field_name == "local_evidence":
        values = (_local_wrapper(),)
    else:
        values = (_web_wrapper(),)
    collection = _unordered_collection(unordered_kind, values)

    with pytest.raises(ValidationError, match=field_name):
        if model_name == "answer_input":
            AnswerInput.model_validate(
                {
                    "original_query": "question",
                    "local_evidence_status": "SUFFICIENT",
                    "local_evidence": [_local_wrapper()],
                    field_name: collection,
                }
            )
        else:
            WorkingEvidenceBundle.model_validate({field_name: collection})


@pytest.mark.parametrize("model_name", ("answer_input", "working_bundle"))
@pytest.mark.parametrize("field_name", ("local_evidence", "web_evidence"))
def test_answer_evidence_collections_reject_duplicate_citation_ids(
    model_name: str,
    field_name: str,
) -> None:
    if field_name == "local_evidence":
        duplicates: tuple[object, ...] = (
            _local_wrapper(citation_id="L1", context_id="parent-1"),
            _local_wrapper(citation_id="L1", context_id="parent-2"),
        )
    else:
        duplicates = (
            _web_wrapper(citation_id="W1", search_rank=1),
            _web_wrapper(citation_id="W1", search_rank=2),
        )

    with pytest.raises(ValidationError, match="citation"):
        if model_name == "answer_input":
            AnswerInput.model_validate(
                {
                    "original_query": "question",
                    "local_evidence_status": "NONE",
                    field_name: duplicates,
                }
            )
        else:
            WorkingEvidenceBundle.model_validate({field_name: duplicates})


@pytest.mark.parametrize("model_name", ("answer_input", "working_bundle"))
def test_answer_evidence_collections_enforce_global_citation_uniqueness(
    model_name: str,
) -> None:
    local = _local_wrapper(citation_id="L1")
    forged_cross_kind_duplicate = AnswerWebEvidence.model_construct(
        citation_id="L1",
        evidence=_web_evidence(),
    )

    with pytest.raises(ValidationError, match="citation"):
        if model_name == "answer_input":
            AnswerInput(
                original_query="question",
                local_evidence_status=LocalAnswerEvidenceStatus.SUFFICIENT,
                local_evidence=(local,),
                web_evidence=(forged_cross_kind_duplicate,),
            )
        else:
            WorkingEvidenceBundle(
                local_evidence=(local,),
                web_evidence=(forged_cross_kind_duplicate,),
            )


def test_answer_input_and_working_bundle_accept_json_arrays_as_tuples() -> None:
    answer_input = AnswerInput(
        original_query="question",
        local_evidence_status=LocalAnswerEvidenceStatus.SUFFICIENT,
        local_evidence=(_local_wrapper(),),
        web_evidence=(_web_wrapper(),),
    )
    bundle = WorkingEvidenceBundle(
        local_evidence=answer_input.local_evidence,
        web_evidence=answer_input.web_evidence,
    )

    restored_input = AnswerInput.model_validate_json(answer_input.model_dump_json())
    restored_bundle = WorkingEvidenceBundle.model_validate_json(
        bundle.model_dump_json()
    )

    assert restored_input == answer_input
    assert restored_bundle == bundle
    assert isinstance(restored_input.local_evidence, tuple)
    assert isinstance(restored_input.web_evidence, tuple)
    assert isinstance(restored_bundle.local_evidence, tuple)
    assert isinstance(restored_bundle.web_evidence, tuple)


@pytest.mark.parametrize(
    ("model_type", "payload"),
    (
        (
            AnswerLocalEvidence,
            {"citation_id": "L1", "evidence": _local_evidence()},
        ),
        (
            AnswerWebEvidence,
            {"citation_id": "W1", "evidence": _web_evidence()},
        ),
        (
            AnswerInput,
            {
                "original_query": "question",
                "local_evidence_status": "NONE",
            },
        ),
        (WorkingEvidenceBundle, {}),
    ),
)
def test_answer_boundary_models_reject_unknown_fields(
    model_type: type[BaseModel], payload: dict[str, object]
) -> None:
    _assert_extra_field_rejected(model_type, payload)


@pytest.mark.parametrize(
    ("model_factory", "field_name"),
    (
        (_local_wrapper, "citation_id"),
        (_web_wrapper, "citation_id"),
        (
            lambda: AnswerInput(
                original_query="question",
                local_evidence_status=LocalAnswerEvidenceStatus.NONE,
            ),
            "original_query",
        ),
        (WorkingEvidenceBundle, "local_evidence"),
    ),
)
def test_answer_boundary_models_are_immutable(
    model_factory: Callable[[], BaseModel], field_name: str
) -> None:
    _assert_frozen(model_factory, field_name)


def test_generation_and_review_models_have_exact_field_sets() -> None:
    assert tuple(GenerationResult.model_fields) == (
        "answer_text",
        "used_citation_ids",
    )
    assert tuple(OutputReviewResult.model_fields) == ("decision", "reason_codes")


def test_generation_result_preserves_nonblank_answer_text() -> None:
    result = GenerationResult(answer_text="  Grounded answer.  ")

    assert result.answer_text == "  Grounded answer.  "
    assert result.used_citation_ids == ()
    assert not hasattr(result, "url")
    assert not hasattr(result, "document_id")
    assert not hasattr(result, "source")


@pytest.mark.parametrize("blank", ("", " ", "\t\n"))
def test_generation_result_rejects_blank_answer_text(blank: str) -> None:
    with pytest.raises(ValidationError, match="answer_text"):
        GenerationResult(answer_text=blank)


def test_generation_result_rejects_byte_answer_text() -> None:
    with pytest.raises(ValidationError, match="answer_text"):
        GenerationResult.model_validate({"answer_text": b"answer"})


@pytest.mark.parametrize(
    "citation_ids",
    (
        (),
        ("L1",),
        ("W1",),
        ("L1", "W1", "L20", "W300"),
    ),
)
def test_generation_result_accepts_ordered_unique_local_and_web_ids(
    citation_ids: tuple[str, ...],
) -> None:
    result = GenerationResult(
        answer_text="Grounded answer.",
        used_citation_ids=citation_ids,
    )

    assert result.used_citation_ids == citation_ids
    assert isinstance(result.used_citation_ids, tuple)


@pytest.mark.parametrize(
    "citation_id",
    (
        "",
        "L0",
        "L01",
        "W0",
        "W01",
        "l1",
        "w1",
        " L1",
        "W1 ",
        "X1",
        "L1\n",
        b"L1",
    ),
)
def test_generation_result_rejects_noncanonical_citation_ids(
    citation_id: object,
) -> None:
    with pytest.raises(ValidationError, match="used_citation_ids"):
        GenerationResult.model_validate(
            {
                "answer_text": "answer",
                "used_citation_ids": [citation_id],
            }
        )


def test_generation_result_rejects_duplicate_citation_ids() -> None:
    with pytest.raises(ValidationError, match="citation"):
        GenerationResult(
            answer_text="answer",
            used_citation_ids=("L1", "W1", "L1"),
        )


@pytest.mark.parametrize("unordered_kind", ("set", "frozenset", "mapping", "generator"))
def test_generation_result_rejects_unordered_citation_inputs(
    unordered_kind: str,
) -> None:
    collection = _unordered_collection(unordered_kind, ("L1", "W1"))

    with pytest.raises(ValidationError, match="used_citation_ids"):
        GenerationResult.model_validate(
            {
                "answer_text": "answer",
                "used_citation_ids": collection,
            }
        )


def test_generation_result_accepts_json_array_roundtrip() -> None:
    result = GenerationResult(
        answer_text="Grounded answer.",
        used_citation_ids=("L1", "W1"),
    )

    restored = GenerationResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert isinstance(restored.used_citation_ids, tuple)


def test_output_review_result_accepts_all_frozen_reason_codes() -> None:
    result = OutputReviewResult(
        decision=OutputReviewDecision.REGENERATE,
        reason_codes=tuple(OutputReviewReasonCode),
    )

    assert result.reason_codes == tuple(OutputReviewReasonCode)
    assert isinstance(result.reason_codes, tuple)
    assert tuple(code.value for code in result.reason_codes) == (
        "UNKNOWN_CITATION",
        "CITATION_SET_MISMATCH",
        "INVALID_OUTPUT_SCHEMA",
        "FORGED_SOURCE_REFERENCE",
        "SOURCE_BOUNDARY_VIOLATION",
        "CONTROL_LEAKAGE",
        "PROTOCOL_CONTENT",
    )


def test_output_review_result_accepts_legitimate_json_enum_strings_and_arrays() -> None:
    result = OutputReviewResult.model_validate_json(
        '{"decision":"BLOCK",'
        '"reason_codes":["FORGED_SOURCE_REFERENCE","CONTROL_LEAKAGE"]}'
    )

    assert result.decision is OutputReviewDecision.BLOCK
    assert result.reason_codes == (
        OutputReviewReasonCode.FORGED_SOURCE_REFERENCE,
        OutputReviewReasonCode.CONTROL_LEAKAGE,
    )


@pytest.mark.parametrize("field_name", ("decision", "reason_codes"))
def test_output_review_result_rejects_byte_enum_inputs(field_name: str) -> None:
    payload: dict[str, object] = {
        "decision": "ACCEPT",
        "reason_codes": ["UNKNOWN_CITATION"],
    }
    payload[field_name] = (
        b"ACCEPT" if field_name == "decision" else [b"UNKNOWN_CITATION"]
    )

    with pytest.raises(ValidationError, match=field_name):
        OutputReviewResult.model_validate(payload)


@pytest.mark.parametrize("unordered_kind", ("set", "frozenset", "mapping", "generator"))
def test_output_review_result_rejects_unordered_reason_code_inputs(
    unordered_kind: str,
) -> None:
    collection = _unordered_collection(
        unordered_kind,
        (
            OutputReviewReasonCode.UNKNOWN_CITATION,
            OutputReviewReasonCode.CONTROL_LEAKAGE,
        ),
    )

    with pytest.raises(ValidationError, match="reason_codes"):
        OutputReviewResult.model_validate(
            {"decision": "REGENERATE", "reason_codes": collection}
        )


def test_output_review_result_rejects_duplicate_reason_codes() -> None:
    with pytest.raises(ValidationError, match="reason_codes"):
        OutputReviewResult(
            decision=OutputReviewDecision.REGENERATE,
            reason_codes=(
                OutputReviewReasonCode.UNKNOWN_CITATION,
                OutputReviewReasonCode.UNKNOWN_CITATION,
            ),
        )


@pytest.mark.parametrize(
    ("decision", "reason_codes"),
    (
        (OutputReviewDecision.ACCEPT, (OutputReviewReasonCode.CONTROL_LEAKAGE,)),
        (OutputReviewDecision.REGENERATE, ()),
        (OutputReviewDecision.BLOCK, ()),
    ),
)
def test_output_review_result_does_not_invent_decision_cardinality_rules(
    decision: OutputReviewDecision,
    reason_codes: tuple[OutputReviewReasonCode, ...],
) -> None:
    result = OutputReviewResult(decision=decision, reason_codes=reason_codes)

    assert result.decision is decision
    assert result.reason_codes == reason_codes
    assert not hasattr(result, "retry_count")


@pytest.mark.parametrize(
    ("model_type", "payload"),
    (
        (GenerationResult, {"answer_text": "answer"}),
        (OutputReviewResult, {"decision": "ACCEPT"}),
    ),
)
def test_generation_and_review_models_reject_unknown_fields(
    model_type: type[BaseModel], payload: dict[str, object]
) -> None:
    _assert_extra_field_rejected(model_type, payload)


@pytest.mark.parametrize(
    ("model_factory", "field_name"),
    (
        (lambda: GenerationResult(answer_text="answer"), "answer_text"),
        (
            lambda: OutputReviewResult(decision=OutputReviewDecision.ACCEPT),
            "decision",
        ),
    ),
)
def test_generation_and_review_models_are_immutable(
    model_factory: Callable[[], BaseModel], field_name: str
) -> None:
    _assert_frozen(model_factory, field_name)
