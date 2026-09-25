"""Contract tests for canonical local evidence and its outcomes."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import BaseModel, ValidationError

from atlasrag.domain.enums import (
    EvidenceType,
    LocalEvidenceStatus,
    LocalExecutionStatus,
)
from atlasrag.domain.evidence import (
    EvidenceRef,
    LocalEvidence,
    LocalEvidenceResult,
    LocalProvenance,
    SourceAnchor,
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


def _provenance() -> LocalProvenance:
    return LocalProvenance(
        file_name="guide.pdf",
        relative_source_path="manuals/guide.pdf",
        section_path=("Chapter 1", "Overview"),
        source_anchor=SourceAnchor(page_number=2, heading="Overview"),
    )


def _evidence(
    *,
    evidence_type: EvidenceType = EvidenceType.TEXT_PARENT,
    context_id: str = "parent-1",
) -> LocalEvidence:
    return LocalEvidence(
        evidence_type=evidence_type,
        context_id=context_id,
        document_id="document-1",
        revision_id="revision-1",
        content="Complete canonical context.",
        provenance=_provenance(),
    )


def _assert_extra_field_rejected(
    model_type: type[BaseModel], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        model_type.model_validate({**payload, "unexpected": True})

    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"


def test_local_evidence_models_have_exact_field_sets() -> None:
    assert tuple(SourceAnchor.model_fields) == (
        "page_number",
        "heading",
        "sheet_name",
        "cell_range",
    )
    assert tuple(LocalProvenance.model_fields) == (
        "file_name",
        "relative_source_path",
        "section_path",
        "source_anchor",
    )
    assert tuple(EvidenceRef.model_fields) == (
        "evidence_type",
        "context_id",
        "document_id",
        "revision_id",
    )
    assert tuple(LocalEvidence.model_fields) == (
        "evidence_type",
        "context_id",
        "document_id",
        "revision_id",
        "content",
        "provenance",
    )
    assert tuple(LocalEvidenceResult.model_fields) == (
        "execution_status",
        "evidence_status",
        "selected_evidence",
        "best_available_evidence",
    )


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("page_number", 3),
        ("heading", "Introduction"),
        ("sheet_name", "Q1"),
        ("cell_range", "A1:C8"),
    ),
)
def test_source_anchor_accepts_each_minimal_anchor_kind(
    field_name: str, value: object
) -> None:
    anchor = SourceAnchor.model_validate({field_name: value})

    assert getattr(anchor, field_name) == value


def test_source_anchor_requires_at_least_one_value() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        SourceAnchor()


@pytest.mark.parametrize("field_name", ("heading", "sheet_name", "cell_range"))
def test_source_anchor_rejects_blank_optional_strings(field_name: str) -> None:
    with pytest.raises(ValidationError, match=field_name):
        SourceAnchor.model_validate({field_name: " \n "})


@pytest.mark.parametrize("page_number", (0, -1))
def test_source_anchor_requires_positive_page_numbers(page_number: int) -> None:
    with pytest.raises(ValidationError) as exc_info:
        SourceAnchor(page_number=page_number)

    assert exc_info.value.errors()[0]["type"] == "greater_than_equal"


@pytest.mark.parametrize("malformed_page_number", (True, "1"))
def test_source_anchor_rejects_boolean_and_string_page_numbers(
    malformed_page_number: object,
) -> None:
    with pytest.raises(ValidationError, match="page_number"):
        SourceAnchor.model_validate({"page_number": malformed_page_number})


def test_local_provenance_is_typed_and_uses_an_immutable_section_path() -> None:
    provenance = LocalProvenance.model_validate(
        {
            "file_name": " workbook.xlsx ",
            "relative_source_path": "reports/workbook.xlsx",
            "section_path": [" Workbook ", "Sheet 1"],
            "source_anchor": {
                "sheet_name": "Sheet 1",
                "cell_range": "A1:B4",
            },
        }
    )

    assert provenance.file_name == " workbook.xlsx "
    assert provenance.relative_source_path == "reports/workbook.xlsx"
    assert provenance.section_path == (" Workbook ", "Sheet 1")
    assert isinstance(provenance.section_path, tuple)
    assert provenance.source_anchor == SourceAnchor(
        sheet_name="Sheet 1", cell_range="A1:B4"
    )


def test_local_provenance_defaults_to_no_section_or_anchor() -> None:
    provenance = LocalProvenance(file_name="notes.md", relative_source_path="notes.md")

    assert provenance.section_path == ()
    assert provenance.source_anchor is None


@pytest.mark.parametrize("field_name", ("file_name", "relative_source_path"))
def test_local_provenance_rejects_blank_required_strings(field_name: str) -> None:
    payload = {"file_name": "notes.md", "relative_source_path": "notes.md"}
    payload[field_name] = " \t "

    with pytest.raises(ValidationError, match=field_name):
        LocalProvenance.model_validate(payload)


@pytest.mark.parametrize("field_name", ("file_name", "relative_source_path"))
def test_local_provenance_rejects_byte_text_fields(field_name: str) -> None:
    payload: dict[str, object] = {
        "file_name": "notes.md",
        "relative_source_path": "notes.md",
    }
    payload[field_name] = b"bytes are not text"

    with pytest.raises(ValidationError, match=field_name):
        LocalProvenance.model_validate(payload)


@pytest.mark.parametrize(
    "relative_path",
    (
        "",
        ".",
        "./notes.md",
        "docs//notes.md",
        "docs/./notes.md",
        "docs/notes.md/",
        " docs/notes.md",
        "docs/notes.md ",
        "docs/\x00notes.md",
        "/srv/atlasrag/notes.md",
        "../notes.md",
        "documents/../notes.md",
        r"C:\documents\notes.md",
        r"C:notes.md",
        r"\documents\notes.md",
        r"documents\notes.md",
        r"documents\..\notes.md",
        r"\\server\share\notes.md",
    ),
)
def test_local_provenance_rejects_noncanonical_or_unsafe_paths(
    relative_path: str,
) -> None:
    with pytest.raises(ValidationError, match="relative_source_path"):
        LocalProvenance(
            file_name="notes.md",
            relative_source_path=relative_path,
        )


def test_local_provenance_accepts_canonical_posix_relative_path() -> None:
    provenance = LocalProvenance(
        file_name="notes.md",
        relative_source_path="docs/notes.md",
    )

    assert provenance.relative_source_path == "docs/notes.md"


def test_local_provenance_rejects_blank_section_path_elements() -> None:
    with pytest.raises(ValidationError, match="section_path"):
        LocalProvenance(
            file_name="notes.md",
            relative_source_path="notes.md",
            section_path=("Chapter", " "),
        )


@pytest.mark.parametrize("evidence_type", tuple(EvidenceType))
def test_evidence_ref_accepts_only_canonical_evidence_types(
    evidence_type: EvidenceType,
) -> None:
    evidence_ref = EvidenceRef(
        evidence_type=evidence_type,
        context_id="context-1",
        document_id="document-1",
        revision_id="revision-1",
    )

    assert evidence_ref.evidence_type is evidence_type


def test_evidence_ref_rejects_text_child() -> None:
    with pytest.raises(ValidationError) as exc_info:
        EvidenceRef(
            evidence_type="TEXT_CHILD",  # type: ignore[arg-type]
            context_id="child-1",
            document_id="document-1",
            revision_id="revision-1",
        )

    assert exc_info.value.errors()[0]["type"] == "enum"


@pytest.mark.parametrize("field_name", ("context_id", "document_id", "revision_id"))
def test_evidence_ref_rejects_blank_ids(field_name: str) -> None:
    payload = {
        "evidence_type": EvidenceType.TABLE,
        "context_id": "table-1",
        "document_id": "document-1",
        "revision_id": "revision-1",
    }
    payload[field_name] = "  "

    with pytest.raises(ValidationError, match=field_name):
        EvidenceRef.model_validate(payload)


@pytest.mark.parametrize("field_name", ("context_id", "document_id", "revision_id"))
def test_evidence_ref_rejects_byte_ids(field_name: str) -> None:
    payload: dict[str, object] = {
        "evidence_type": EvidenceType.TABLE,
        "context_id": "table-1",
        "document_id": "document-1",
        "revision_id": "revision-1",
    }
    payload[field_name] = b"bytes are not text"

    with pytest.raises(ValidationError, match=field_name):
        EvidenceRef.model_validate(payload)


def test_local_evidence_has_stable_nonserialized_identity() -> None:
    first = _evidence()
    second = _evidence()

    expected = (
        "document-1",
        "revision-1",
        EvidenceType.TEXT_PARENT,
        "parent-1",
    )
    assert first.identity == expected
    assert second.identity == expected
    assert "identity" not in first.model_dump()


@pytest.mark.parametrize(
    "field_name", ("context_id", "document_id", "revision_id", "content")
)
def test_local_evidence_rejects_blank_ids_and_content(field_name: str) -> None:
    payload: dict[str, object] = {
        "evidence_type": EvidenceType.TEXT_PARENT,
        "context_id": "parent-1",
        "document_id": "document-1",
        "revision_id": "revision-1",
        "content": "canonical context",
        "provenance": _provenance(),
    }
    payload[field_name] = " \n "

    with pytest.raises(ValidationError, match=field_name):
        LocalEvidence.model_validate(payload)


@pytest.mark.parametrize(
    "field_name", ("context_id", "document_id", "revision_id", "content")
)
def test_local_evidence_rejects_byte_identity_and_content(field_name: str) -> None:
    payload: dict[str, object] = {
        "evidence_type": EvidenceType.TEXT_PARENT,
        "context_id": "parent-1",
        "document_id": "document-1",
        "revision_id": "revision-1",
        "content": "canonical context",
        "provenance": _provenance(),
    }
    payload[field_name] = b"bytes are not text"

    with pytest.raises(ValidationError, match=field_name):
        LocalEvidence.model_validate(payload)


def test_local_evidence_contains_no_child_or_runtime_citation_fields() -> None:
    evidence = _evidence()

    assert not hasattr(evidence, "child_id")
    assert not hasattr(evidence, "retrieval_text")
    assert not hasattr(evidence, "scores")
    assert not hasattr(evidence, "evidence_id")
    assert not hasattr(evidence, "citation_id")


def test_unavailable_local_execution_has_no_evidence_assessment() -> None:
    result = LocalEvidenceResult(execution_status=LocalExecutionStatus.UNAVAILABLE)

    assert result.evidence_status is None
    assert result.selected_evidence == ()
    assert result.best_available_evidence is None


@pytest.mark.parametrize(
    "execution_status", (LocalExecutionStatus.OK, LocalExecutionStatus.DEGRADED)
)
def test_available_local_execution_requires_an_evidence_status(
    execution_status: LocalExecutionStatus,
) -> None:
    with pytest.raises(ValidationError, match="evidence_status"):
        LocalEvidenceResult(execution_status=execution_status)


@pytest.mark.parametrize(
    "execution_status", (LocalExecutionStatus.OK, LocalExecutionStatus.DEGRADED)
)
def test_sufficient_local_evidence_requires_selected_evidence(
    execution_status: LocalExecutionStatus,
) -> None:
    selected = _evidence()
    result = LocalEvidenceResult.model_validate(
        {
            "execution_status": execution_status,
            "evidence_status": LocalEvidenceStatus.SUFFICIENT,
            "selected_evidence": [selected],
        }
    )

    assert result.execution_status is execution_status
    assert result.evidence_status is LocalEvidenceStatus.SUFFICIENT
    assert result.selected_evidence == (selected,)
    assert isinstance(result.selected_evidence, tuple)

    with pytest.raises(ValidationError, match="selected_evidence"):
        LocalEvidenceResult(
            execution_status=execution_status,
            evidence_status=LocalEvidenceStatus.SUFFICIENT,
        )


def test_sufficient_local_evidence_does_not_overconstrain_best_match() -> None:
    evidence = _evidence()
    result = LocalEvidenceResult(
        execution_status=LocalExecutionStatus.OK,
        evidence_status=LocalEvidenceStatus.SUFFICIENT,
        selected_evidence=(evidence,),
        best_available_evidence=evidence,
    )

    assert result.best_available_evidence == evidence


@pytest.mark.parametrize("best_available", (None, _evidence()))
def test_insufficient_local_evidence_has_no_selection_and_optional_best_match(
    best_available: LocalEvidence | None,
) -> None:
    result = LocalEvidenceResult(
        execution_status=LocalExecutionStatus.OK,
        evidence_status=LocalEvidenceStatus.INSUFFICIENT,
        best_available_evidence=best_available,
    )

    assert result.selected_evidence == ()
    assert result.best_available_evidence == best_available


def test_insufficient_local_evidence_rejects_selected_evidence() -> None:
    with pytest.raises(ValidationError, match="selected_evidence"):
        LocalEvidenceResult(
            execution_status=LocalExecutionStatus.OK,
            evidence_status=LocalEvidenceStatus.INSUFFICIENT,
            selected_evidence=(_evidence(),),
        )


def test_ambiguous_local_evidence_carries_no_evidence_forward() -> None:
    result = LocalEvidenceResult(
        execution_status=LocalExecutionStatus.DEGRADED,
        evidence_status=LocalEvidenceStatus.AMBIGUOUS,
    )

    assert result.selected_evidence == ()
    assert result.best_available_evidence is None


@pytest.mark.parametrize(
    "payload",
    (
        {"selected_evidence": (_evidence(),)},
        {"best_available_evidence": _evidence()},
    ),
)
def test_ambiguous_local_evidence_rejects_selected_or_best_evidence(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="AMBIGUOUS"):
        LocalEvidenceResult.model_validate(
            {
                "execution_status": LocalExecutionStatus.OK,
                "evidence_status": LocalEvidenceStatus.AMBIGUOUS,
                **payload,
            }
        )


@pytest.mark.parametrize(
    "payload",
    (
        {"evidence_status": LocalEvidenceStatus.SUFFICIENT},
        {"selected_evidence": (_evidence(),)},
        {"best_available_evidence": _evidence()},
    ),
)
def test_unavailable_local_execution_rejects_all_evidence_data(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="UNAVAILABLE"):
        LocalEvidenceResult.model_validate(
            {"execution_status": LocalExecutionStatus.UNAVAILABLE, **payload}
        )


@pytest.mark.parametrize("unordered_kind", ("set", "frozenset", "mapping", "generator"))
@pytest.mark.parametrize(
    ("model_type", "payload", "field_name", "values"),
    (
        (
            LocalProvenance,
            {
                "file_name": "notes.md",
                "relative_source_path": "notes.md",
            },
            "section_path",
            ("Chapter 1", "Overview"),
        ),
        (
            LocalEvidenceResult,
            {
                "execution_status": LocalExecutionStatus.OK,
                "evidence_status": LocalEvidenceStatus.SUFFICIENT,
            },
            "selected_evidence",
            (_evidence(context_id="parent-1"), _evidence(context_id="parent-2")),
        ),
    ),
)
def test_ordered_local_evidence_collections_reject_unordered_iterables(
    model_type: type[BaseModel],
    payload: dict[str, object],
    field_name: str,
    values: tuple[object, ...],
    unordered_kind: str,
) -> None:
    with pytest.raises(ValidationError, match=field_name):
        model_type.model_validate(
            {
                **payload,
                field_name: _unordered_collection(unordered_kind, values),
            }
        )


@pytest.mark.parametrize(
    ("model", "field_name"),
    (
        (
            LocalProvenance(
                file_name="notes.md",
                relative_source_path="notes.md",
                section_path=("Chapter 1", "Overview"),
            ),
            "section_path",
        ),
        (
            LocalEvidenceResult(
                execution_status=LocalExecutionStatus.OK,
                evidence_status=LocalEvidenceStatus.SUFFICIENT,
                selected_evidence=(_evidence(),),
            ),
            "selected_evidence",
        ),
    ),
)
def test_ordered_local_evidence_collections_accept_json_arrays(
    model: BaseModel, field_name: str
) -> None:
    restored = type(model).model_validate_json(model.model_dump_json())

    assert isinstance(getattr(restored, field_name), tuple)


@pytest.mark.parametrize(
    ("model_type", "payload"),
    (
        (SourceAnchor, {"page_number": 1}),
        (
            LocalProvenance,
            {"file_name": "notes.md", "relative_source_path": "notes.md"},
        ),
        (
            EvidenceRef,
            {
                "evidence_type": "TABLE",
                "context_id": "table-1",
                "document_id": "document-1",
                "revision_id": "revision-1",
            },
        ),
        (
            LocalEvidence,
            {
                "evidence_type": "TEXT_PARENT",
                "context_id": "parent-1",
                "document_id": "document-1",
                "revision_id": "revision-1",
                "content": "context",
                "provenance": _provenance(),
            },
        ),
        (
            LocalEvidenceResult,
            {
                "execution_status": "OK",
                "evidence_status": "INSUFFICIENT",
            },
        ),
    ),
)
def test_local_evidence_models_reject_unknown_fields(
    model_type: type[BaseModel], payload: dict[str, object]
) -> None:
    _assert_extra_field_rejected(model_type, payload)


@pytest.mark.parametrize(
    ("model_factory", "field_name"),
    (
        (lambda: SourceAnchor(page_number=1), "page_number"),
        (_provenance, "file_name"),
        (
            lambda: EvidenceRef(
                evidence_type=EvidenceType.TABLE,
                context_id="table-1",
                document_id="document-1",
                revision_id="revision-1",
            ),
            "context_id",
        ),
        (_evidence, "content"),
        (
            lambda: LocalEvidenceResult(
                execution_status=LocalExecutionStatus.OK,
                evidence_status=LocalEvidenceStatus.INSUFFICIENT,
            ),
            "evidence_status",
        ),
    ),
)
def test_local_evidence_models_are_immutable(
    model_factory: Callable[[], BaseModel], field_name: str
) -> None:
    model = model_factory()

    with pytest.raises(ValidationError) as exc_info:
        setattr(model, field_name, "changed")

    assert exc_info.value.errors()[0]["type"] == "frozen_instance"
