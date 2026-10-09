"""Canonical parsed-document and element contract tests."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from atlasrag.domain.enums import SourceType
from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.ingestion.document import (
    DocumentContainer,
    DocumentMetadata,
    ParseInfo,
)
from atlasrag.domain.ingestion.elements import (
    CanonicalElement,
    CanonicalElementMetadata,
)
from atlasrag.domain.ingestion.enums import (
    ElementType,
    ListKind,
    ParseOutcome,
    ParseReliability,
)
from atlasrag.domain.ingestion.source import CanonicalSource, derive_document_id
from atlasrag.services.ingestion.identity import build_element

DOCUMENT_ID = derive_document_id("docs/guide.md")
REVISION_ID = "rev_" + "a" * 64


def canonical_source() -> CanonicalSource:
    return CanonicalSource(
        relative_source_path="docs/guide.md",
        file_name="guide.md",
        source_type=SourceType.MD,
    )


def reliable_parse_info() -> ParseInfo:
    return ParseInfo(
        outcome=ParseOutcome.SUCCESS,
        reliability=ParseReliability.RELIABLE,
        parser_name="atlasrag-markdown",
        parser_version="1.0.0",
    )


def paragraph(
    *,
    order: int = 0,
    content: str = "Canonical paragraph.",
    section_path: tuple[str, ...] = ("Introduction",),
) -> CanonicalElement:
    return build_element(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        element_type=ElementType.PARAGRAPH,
        order=order,
        content=content,
        section_path=section_path,
    )


def document(**overrides: Any) -> DocumentContainer:
    values: dict[str, object] = {
        "document_id": DOCUMENT_ID,
        "revision_id": REVISION_ID,
        "source": canonical_source(),
        "metadata": DocumentMetadata(title="Guide", language="en"),
        "parse_info": reliable_parse_info(),
        "elements": (paragraph(),),
    }
    values.update(overrides)
    return DocumentContainer.model_validate(values)


def test_stage3_parsed_enums_are_exact_and_stable() -> None:
    assert [item.value for item in ParseOutcome] == [
        "SUCCESS",
        "DEGRADED",
        "FAILED",
    ]
    assert [item.value for item in ParseReliability] == [
        "RELIABLE",
        "PARTIAL",
        "UNAVAILABLE",
    ]
    assert [item.value for item in ElementType] == [
        "HEADING",
        "PARAGRAPH",
        "LIST",
        "CODE",
        "TABLE",
    ]
    assert [item.value for item in ListKind] == ["ORDERED", "UNORDERED"]


@pytest.mark.parametrize(
    ("outcome", "reliability", "warning_codes", "failure_code"),
    [
        (ParseOutcome.SUCCESS, ParseReliability.RELIABLE, (), None),
        (
            ParseOutcome.SUCCESS,
            ParseReliability.PARTIAL,
            ("PARTIAL_EXTRACTION",),
            None,
        ),
        (
            ParseOutcome.DEGRADED,
            ParseReliability.PARTIAL,
            ("OCR_DEGRADED",),
            None,
        ),
        (
            ParseOutcome.FAILED,
            ParseReliability.UNAVAILABLE,
            (),
            "CORRUPT_DOCUMENT",
        ),
    ],
)
def test_parse_info_accepts_only_frozen_meaningful_combinations(
    outcome: ParseOutcome,
    reliability: ParseReliability,
    warning_codes: tuple[str, ...],
    failure_code: str | None,
) -> None:
    info = ParseInfo(
        outcome=outcome,
        reliability=reliability,
        parser_name="provider",
        parser_version="4.0.11",
        warning_codes=warning_codes,
        failure_code=failure_code,
    )

    assert info.outcome is outcome
    assert info.reliability is reliability


@pytest.mark.parametrize(
    ("outcome", "reliability"),
    [
        (ParseOutcome.SUCCESS, ParseReliability.UNAVAILABLE),
        (ParseOutcome.DEGRADED, ParseReliability.RELIABLE),
        (ParseOutcome.DEGRADED, ParseReliability.UNAVAILABLE),
        (ParseOutcome.FAILED, ParseReliability.RELIABLE),
        (ParseOutcome.FAILED, ParseReliability.PARTIAL),
    ],
)
def test_parse_info_rejects_invalid_outcome_reliability_pairs(
    outcome: ParseOutcome, reliability: ParseReliability
) -> None:
    with pytest.raises(ValidationError, match="outcome.*reliability"):
        ParseInfo(
            outcome=outcome,
            reliability=reliability,
            parser_name="provider",
            parser_version="1",
            warning_codes=("DEGRADED",),
            failure_code="FAILED" if outcome is ParseOutcome.FAILED else None,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "outcome": "SUCCESS",
            "reliability": "PARTIAL",
            "warning_codes": (),
            "failure_code": None,
        },
        {
            "outcome": "DEGRADED",
            "reliability": "PARTIAL",
            "warning_codes": (),
            "failure_code": None,
        },
        {
            "outcome": "FAILED",
            "reliability": "UNAVAILABLE",
            "warning_codes": (),
            "failure_code": None,
        },
        {
            "outcome": "SUCCESS",
            "reliability": "RELIABLE",
            "warning_codes": (),
            "failure_code": "UNEXPECTED",
        },
    ],
)
def test_parse_info_requires_stable_explanatory_codes(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ParseInfo(
            parser_name="provider",
            parser_version="1",
            **payload,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "code",
    ["lowercase", "HAS SPACE", "Traceback: provider failed", "", "_PREFIX"],
)
def test_parse_info_rejects_unstable_or_free_form_reason_codes(code: str) -> None:
    with pytest.raises(ValidationError, match="stable reason code"):
        ParseInfo(
            outcome=ParseOutcome.FAILED,
            reliability=ParseReliability.UNAVAILABLE,
            parser_name="provider",
            parser_version="1",
            failure_code=code,
        )


def test_parse_info_rejects_duplicate_warning_codes_and_unordered_input() -> None:
    with pytest.raises(ValidationError, match="unique"):
        ParseInfo(
            outcome=ParseOutcome.SUCCESS,
            reliability=ParseReliability.PARTIAL,
            parser_name="provider",
            parser_version="1",
            warning_codes=("PARTIAL_EXTRACTION", "PARTIAL_EXTRACTION"),
        )
    with pytest.raises(ValidationError, match="list or tuple"):
        ParseInfo.model_validate(
            {
                "outcome": "SUCCESS",
                "reliability": "PARTIAL",
                "parser_name": "provider",
                "parser_version": "1",
                "warning_codes": {"PARTIAL_EXTRACTION"},
            }
        )


def test_document_metadata_is_typed_normalized_and_provider_payload_free() -> None:
    metadata = DocumentMetadata(
        title="Cafe\u0301\r\nGuide",
        authors=("Ada", "林"),
        language="en",
        page_count=3,
        sheet_names=("Summary", "数据"),
    )

    assert metadata.title == "Café\nGuide"
    assert metadata.model_dump(mode="json") == {
        "title": "Café\nGuide",
        "authors": ["Ada", "林"],
        "language": "en",
        "page_count": 3,
        "sheet_names": ["Summary", "数据"],
    }
    with pytest.raises(ValidationError, match="Extra inputs"):
        DocumentMetadata.model_validate(
            {"title": "Guide", "provider_timestamp": "2026-10-09T00:00:00Z"}
        )


def test_element_metadata_is_typed_and_cross_validated() -> None:
    heading = build_element(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        element_type=ElementType.HEADING,
        order=0,
        content="Overview",
        section_path=("Overview",),
        metadata=CanonicalElementMetadata(heading_level=2),
    )
    listing = build_element(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        element_type=ElementType.LIST,
        order=1,
        content="First item",
        section_path=("Overview",),
        metadata=CanonicalElementMetadata(
            list_kind=ListKind.ORDERED,
            list_depth=1,
        ),
    )
    code = build_element(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        element_type=ElementType.CODE,
        order=2,
        content="  print('x')\n",
        metadata=CanonicalElementMetadata(code_language="python"),
    )

    assert heading.metadata.heading_level == 2
    assert listing.metadata.list_kind is ListKind.ORDERED
    assert listing.metadata.list_depth == 1
    assert code.content == "  print('x')\n"


@pytest.mark.parametrize(
    ("element_type", "metadata"),
    [
        (ElementType.HEADING, CanonicalElementMetadata()),
        (
            ElementType.PARAGRAPH,
            CanonicalElementMetadata(heading_level=1),
        ),
        (ElementType.LIST, CanonicalElementMetadata()),
        (
            ElementType.CODE,
            CanonicalElementMetadata(list_kind=ListKind.UNORDERED),
        ),
        (
            ElementType.TABLE,
            CanonicalElementMetadata(code_language="sql"),
        ),
    ],
)
def test_element_type_rejects_contradictory_metadata(
    element_type: ElementType, metadata: CanonicalElementMetadata
) -> None:
    with pytest.raises(ValidationError, match="metadata"):
        build_element(
            document_id=DOCUMENT_ID,
            revision_id=REVISION_ID,
            element_type=element_type,
            order=0,
            content="content",
            metadata=metadata,
        )


def test_text_normalization_preserves_case_punctuation_and_code_whitespace() -> None:
    prose = paragraph(content="Cafe\u0301! KEEP Case.\r\nNext\rLine")
    code = build_element(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        element_type=ElementType.CODE,
        order=1,
        content="\tif True:\r\n    print('Yes!')  \r",
        metadata=CanonicalElementMetadata(code_language="python"),
    )

    assert prose.content == "Café! KEEP Case.\nNext\nLine"
    assert code.content == "\tif True:\n    print('Yes!')  \n"


@pytest.mark.parametrize("content", ["", " \t\n", "nul\x00text", "bad\x07text"])
def test_elements_reject_empty_or_unsafe_text(content: str) -> None:
    with pytest.raises(ValueError, match="canonical text"):
        paragraph(content=content)


def test_element_id_is_deterministic_and_semantically_sensitive() -> None:
    baseline = paragraph()
    same = paragraph()
    changed_content = paragraph(content="Changed.")
    changed_order = paragraph(order=1)
    changed_revision = build_element(
        document_id=DOCUMENT_ID,
        revision_id="rev_" + "b" * 64,
        element_type=ElementType.PARAGRAPH,
        order=0,
        content="Canonical paragraph.",
        section_path=("Introduction",),
    )
    changed_anchor = build_element(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        element_type=ElementType.PARAGRAPH,
        order=0,
        content="Canonical paragraph.",
        section_path=("Introduction",),
        source_anchor=SourceAnchor(heading="Introduction"),
    )

    assert baseline.element_id == same.element_id
    assert baseline.element_id.startswith("elem_")
    assert len(baseline.element_id) == 69
    assert (
        len(
            {
                baseline.element_id,
                changed_content.element_id,
                changed_order.element_id,
                changed_revision.element_id,
                changed_anchor.element_id,
            }
        )
        == 5
    )


def test_element_source_anchor_is_canonical_before_identity_hashing() -> None:
    decomposed = build_element(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        element_type=ElementType.PARAGRAPH,
        order=0,
        content="Canonical paragraph.",
        source_anchor=SourceAnchor(heading="Cafe\u0301"),
    )
    precomposed = build_element(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        element_type=ElementType.PARAGRAPH,
        order=0,
        content="Canonical paragraph.",
        source_anchor=SourceAnchor(heading="Café"),
    )

    assert decomposed.source_anchor == SourceAnchor(heading="Café")
    assert decomposed.element_id == precomposed.element_id
    with pytest.raises(ValueError, match="single line"):
        build_element(
            document_id=DOCUMENT_ID,
            revision_id=REVISION_ID,
            element_type=ElementType.PARAGRAPH,
            order=0,
            content="Canonical paragraph.",
            source_anchor=SourceAnchor(heading="bad\nanchor"),
        )


def test_element_rejects_forged_id_and_invalid_section_path() -> None:
    valid = paragraph()
    payload = valid.model_dump()
    payload["element_id"] = "elem_" + "0" * 64

    with pytest.raises(ValidationError, match="element_id"):
        type(valid).model_validate(payload)
    with pytest.raises(ValueError, match="section_path"):
        paragraph(section_path=("Introduction", " "))


def test_document_container_requires_matching_identity_and_increasing_order() -> None:
    first = paragraph(order=0)
    later = paragraph(order=2, content="Later.")

    parsed = document(elements=(first, later))

    assert [element.order for element in parsed.elements] == [0, 2]
    with pytest.raises(ValidationError, match="strictly increasing"):
        document(elements=(later, first))
    with pytest.raises(ValidationError, match="document_id"):
        document(document_id="doc_" + "0" * 64)


def test_document_container_rejects_duplicate_element_ids() -> None:
    duplicate = paragraph(order=0)

    with pytest.raises(ValidationError, match="element_id.*unique"):
        document(elements=(duplicate, duplicate))


def test_usable_document_requires_elements_and_failed_document_forbids_them() -> None:
    with pytest.raises(ValidationError, match="usable parse.*elements"):
        document(elements=())

    failed = document(
        parse_info=ParseInfo(
            outcome=ParseOutcome.FAILED,
            reliability=ParseReliability.UNAVAILABLE,
            parser_name="provider",
            parser_version="1",
            failure_code="CORRUPT_DOCUMENT",
        ),
        elements=(),
    )
    assert failed.elements == ()

    with pytest.raises(ValidationError, match="failed parse.*elements"):
        document(
            parse_info=ParseInfo(
                outcome=ParseOutcome.FAILED,
                reliability=ParseReliability.UNAVAILABLE,
                parser_name="provider",
                parser_version="1",
                failure_code="CORRUPT_DOCUMENT",
            ),
            elements=(paragraph(),),
        )


def test_document_container_is_versioned_immutable_and_json_round_trips() -> None:
    parsed = document()

    restored = DocumentContainer.model_validate_json(parsed.model_dump_json())

    assert parsed.schema_version == 1
    assert restored == parsed
    with pytest.raises(ValidationError):
        DocumentContainer.model_validate({**parsed.model_dump(), "schema_version": 2})
    with pytest.raises(ValidationError, match="frozen"):
        parsed.revision_id = "rev_" + "b" * 64


def test_document_container_revalidates_constructed_nested_element() -> None:
    valid = paragraph()
    forged = type(valid).model_construct(
        **{**valid.model_dump(), "content": "invalid\x00content"}
    )

    with pytest.raises(ValidationError):
        document(elements=(forged,))
