"""Fail-closed validation of canonical parsed documents and complete chunk sets."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, TypeVar, cast, overload

import pytest
from pydantic import BaseModel, ValidationError

import atlasrag.services.ingestion.validation as validation_service
from atlasrag.domain.enums import SourceType
from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.ingestion.chunks import (
    ChunkRecord,
    ChunkType,
    ElementSpan,
    TextChildProvenance,
    TextParentProvenance,
)
from atlasrag.domain.ingestion.document import (
    DocumentContainer,
    DocumentMetadata,
    ParseInfo,
)
from atlasrag.domain.ingestion.elements import (
    CanonicalElement,
    TableData,
    render_table_data,
)
from atlasrag.domain.ingestion.enums import ElementType, ParseOutcome, ParseReliability
from atlasrag.domain.ingestion.source import CanonicalSource, derive_document_id
from atlasrag.domain.ingestion.validation import (
    ValidationOutcome,
    ValidationReasonCode,
    ValidationResult,
)
from atlasrag.services.ingestion.chunking import ChunkingProfile, build_chunks
from atlasrag.services.ingestion.identity import build_chunk, build_element
from atlasrag.services.ingestion.validation import (
    validate_chunk_set,
    validate_parsed_document,
)

DOCUMENT_ID = derive_document_id("reports/data.xlsx")
REVISION_ID = "rev_" + "a" * 64
OTHER_REVISION_ID = "rev_" + "b" * 64
_ModelT = TypeVar("_ModelT", bound=BaseModel)


class _CountingSequence(Sequence[object]):
    def __init__(self, values: Sequence[object]) -> None:
        self._values = tuple(values)
        self.inspections = 0

    def __len__(self) -> int:
        return len(self._values)

    @overload
    def __getitem__(self, index: int) -> object: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[object]: ...

    def __getitem__(self, index: int | slice) -> object | Sequence[object]:
        if isinstance(index, slice):
            self.inspections += len(range(*index.indices(len(self))))
        else:
            self.inspections += 1
        return self._values[index]

    def __iter__(self) -> Iterator[object]:
        for value in self._values:
            self.inspections += 1
            yield value


def _forge(model: _ModelT, **changes: object) -> _ModelT:
    """Bypass Pydantic so the production validator sees malformed models."""
    values = {
        name: getattr(model, name)
        for name in type(model).model_fields
        if hasattr(model, name)
    }
    values.update(changes)
    return type(model).model_construct(**values)


def _table_data() -> TableData:
    anchor = SourceAnchor(sheet_name="Data", cell_range="A1:B4")
    return TableData(
        headers=("Name", "Value"),
        rows=(("Alpha", "1"), ("Beta", "2"), ("Gamma", "3")),
        caption="Metrics",
        sheet_name="Data",
        source_anchor=anchor,
    )


def _element(
    order: int,
    content: str,
    *,
    section_path: tuple[str, ...] = ("Overview",),
) -> CanonicalElement:
    return build_element(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        element_type=ElementType.PARAGRAPH,
        order=order,
        content=content,
        section_path=section_path,
    )


def _table_element(order: int = 2) -> CanonicalElement:
    data = _table_data()
    return build_element(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        element_type=ElementType.TABLE,
        order=order,
        content=render_table_data(data),
        section_path=("Data",),
        source_anchor=data.source_anchor,
        table_data=data,
    )


def _parse_info(
    outcome: ParseOutcome = ParseOutcome.SUCCESS,
    reliability: ParseReliability = ParseReliability.RELIABLE,
) -> ParseInfo:
    warning_codes: tuple[str, ...] = ()
    if reliability is ParseReliability.PARTIAL:
        warning_codes = ("PROVIDER_PARTIAL",)
    return ParseInfo(
        outcome=outcome,
        reliability=reliability,
        parser_name="test-parser",
        parser_version="1",
        warning_codes=warning_codes,
    )


def _document(
    *,
    parse_info: ParseInfo | None = None,
    elements: tuple[CanonicalElement, ...] | None = None,
) -> DocumentContainer:
    canonical_elements = elements or (
        _element(0, "Alpha words continue."),
        _element(1, "Beta words."),
        _table_element(),
        _element(3, "Tail text.", section_path=("Tail",)),
    )
    return DocumentContainer(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        source=CanonicalSource(
            relative_source_path="reports/data.xlsx",
            file_name="data.xlsx",
            source_type=SourceType.XLSX,
        ),
        metadata=DocumentMetadata(title="Data", sheet_names=("Data",)),
        parse_info=parse_info or _parse_info(),
        elements=canonical_elements,
    )


def _chunks(document: DocumentContainer | None = None) -> tuple[ChunkRecord, ...]:
    return build_chunks(
        document or _document(),
        ChunkingProfile(
            parent_max_chars=100,
            child_max_chars=8,
            table_slice_max_chars=58,
        ),
    )


def _result_codes(result: ValidationResult) -> set[ValidationReasonCode]:
    return set(result.reason_codes)


def _assert_canonical_reason_order(result: ValidationResult) -> None:
    order = {code: index for index, code in enumerate(ValidationReasonCode)}
    assert list(result.reason_codes) == sorted(
        result.reason_codes, key=order.__getitem__
    )
    assert len(result.reason_codes) == len(set(result.reason_codes))


def test_validation_result_is_closed_minimal_immutable_and_coherent() -> None:
    assert [outcome.value for outcome in ValidationOutcome] == ["PASS", "WARN", "FAIL"]
    assert tuple(ValidationResult.model_fields) == ("outcome", "reason_codes")

    passed = ValidationResult(outcome=ValidationOutcome.PASS, reason_codes=())
    warned = ValidationResult(
        outcome=ValidationOutcome.WARN,
        reason_codes=(ValidationReasonCode.PARSE_PARTIAL,),
    )
    failed = ValidationResult(
        outcome=ValidationOutcome.FAIL,
        reason_codes=(ValidationReasonCode.DOCUMENT_SCHEMA_INVALID,),
    )

    assert passed.model_dump(mode="json") == {"outcome": "PASS", "reason_codes": []}
    assert warned.outcome is ValidationOutcome.WARN
    assert failed.outcome is ValidationOutcome.FAIL
    assert (
        not {
            "message",
            "reasoning",
            "provider_payload",
            "benchmark_score",
            "chain_of_thought",
        }
        & ValidationResult.model_fields.keys()
    )
    with pytest.raises(ValidationError, match="frozen"):
        passed.outcome = ValidationOutcome.FAIL
    with pytest.raises(ValidationError, match="Extra inputs"):
        ValidationResult.model_validate(
            {"outcome": "PASS", "reason_codes": (), "message": "free form"}
        )


@pytest.mark.parametrize(
    ("outcome", "reason_codes"),
    [
        (ValidationOutcome.PASS, (ValidationReasonCode.PARSE_PARTIAL,)),
        (ValidationOutcome.WARN, ()),
        (
            ValidationOutcome.WARN,
            (ValidationReasonCode.DOCUMENT_SCHEMA_INVALID,),
        ),
        (ValidationOutcome.FAIL, (ValidationReasonCode.PARSE_PARTIAL,)),
        (ValidationOutcome.FAIL, ()),
        (
            ValidationOutcome.WARN,
            (
                ValidationReasonCode.PARSE_PARTIAL,
                ValidationReasonCode.PARSE_PARTIAL,
            ),
        ),
        (
            ValidationOutcome.WARN,
            (
                ValidationReasonCode.PARSE_PARTIAL,
                ValidationReasonCode.PARSE_DEGRADED,
            ),
        ),
    ],
)
def test_validation_result_rejects_incoherent_duplicate_or_unordered_codes(
    outcome: ValidationOutcome,
    reason_codes: tuple[ValidationReasonCode, ...],
) -> None:
    with pytest.raises(ValidationError):
        ValidationResult(outcome=outcome, reason_codes=reason_codes)


def test_valid_reliable_parsed_and_chunked_outputs_pass() -> None:
    document = _document()

    parsed_result = validate_parsed_document(document)
    chunk_result = validate_chunk_set(document, _chunks(document))

    assert parsed_result == ValidationResult(
        outcome=ValidationOutcome.PASS, reason_codes=()
    )
    assert chunk_result == ValidationResult(
        outcome=ValidationOutcome.PASS, reason_codes=()
    )


def test_public_validators_fail_closed_for_a_non_document_object() -> None:
    malformed = cast(DocumentContainer, object())

    parsed_result = validate_parsed_document(malformed)
    chunk_result = validate_chunk_set(malformed, ())

    for result in (parsed_result, chunk_result):
        assert result.outcome is ValidationOutcome.FAIL
        assert {
            ValidationReasonCode.DOCUMENT_SCHEMA_INVALID,
            ValidationReasonCode.CANONICAL_JSON_INVALID,
        } <= _result_codes(result)
        _assert_canonical_reason_order(result)
    assert ValidationReasonCode.CHUNK_SET_EMPTY in chunk_result.reason_codes


def test_public_validators_fail_closed_for_deeply_nested_forged_data() -> None:
    nested: object = "leaf"
    for _ in range(1_200):
        nested = [nested]
    document = _document()
    malformed = _forge(
        document,
        metadata=DocumentMetadata.model_construct(title=cast(Any, nested)),
    )

    parsed_result = validate_parsed_document(malformed)
    chunk_result = validate_chunk_set(malformed, ())

    for result in (parsed_result, chunk_result):
        assert result.outcome is ValidationOutcome.FAIL
        assert {
            ValidationReasonCode.DOCUMENT_SCHEMA_INVALID,
            ValidationReasonCode.CANONICAL_JSON_INVALID,
        } <= _result_codes(result)
        _assert_canonical_reason_order(result)


def test_public_validators_fail_closed_for_a_deep_domain_model_chain() -> None:
    node: object = DocumentMetadata.model_construct(title="leaf")
    for _ in range(1_200):
        node = DocumentMetadata.model_construct(title=cast(Any, node))
    malformed = _forge(_document(), metadata=node)

    parsed_result = validate_parsed_document(malformed)
    chunk_result = validate_chunk_set(malformed, ())

    for result in (parsed_result, chunk_result):
        assert result.outcome is ValidationOutcome.FAIL
        assert {
            ValidationReasonCode.DOCUMENT_SCHEMA_INVALID,
            ValidationReasonCode.CANONICAL_JSON_INVALID,
        } <= _result_codes(result)
        _assert_canonical_reason_order(result)


def test_sparse_strictly_increasing_element_orders_are_valid() -> None:
    document = _document(
        elements=(
            _element(0, "Alpha words continue."),
            _element(2, "Beta words."),
            _table_element(4),
            _element(7, "Tail text.", section_path=("Tail",)),
        )
    )
    assert [element.order for element in document.elements] == [0, 2, 4, 7]
    chunks = _chunks(document)

    assert validate_parsed_document(document) == ValidationResult(
        outcome=ValidationOutcome.PASS,
        reason_codes=(),
    )
    assert validate_chunk_set(document, chunks) == ValidationResult(
        outcome=ValidationOutcome.PASS,
        reason_codes=(),
    )


@pytest.mark.parametrize(
    "orders",
    [
        (0, cast(Any, "2"), 4, 7),
        (0, 2, 2, 7),
        (0, 4, 2, 7),
    ],
)
def test_invalid_element_order_type_duplicate_or_reversal_fails_closed(
    orders: tuple[object, ...],
) -> None:
    document = _document()
    malformed = _forge(
        document,
        elements=tuple(
            _forge(element, order=order)
            for element, order in zip(document.elements, orders, strict=True)
        ),
    )

    result = validate_parsed_document(malformed)

    assert result.outcome is ValidationOutcome.FAIL
    assert ValidationReasonCode.ELEMENT_ORDER_INVALID in result.reason_codes


@pytest.mark.parametrize(
    ("outcome", "reliability", "expected_codes"),
    [
        (
            ParseOutcome.SUCCESS,
            ParseReliability.PARTIAL,
            (ValidationReasonCode.PARSE_PARTIAL,),
        ),
        (
            ParseOutcome.DEGRADED,
            ParseReliability.PARTIAL,
            (
                ValidationReasonCode.PARSE_DEGRADED,
                ValidationReasonCode.PARSE_PARTIAL,
            ),
        ),
    ],
)
def test_only_structurally_usable_partial_or_degraded_parse_warns(
    outcome: ParseOutcome,
    reliability: ParseReliability,
    expected_codes: tuple[ValidationReasonCode, ...],
) -> None:
    document = _document(parse_info=_parse_info(outcome, reliability))

    parsed_result = validate_parsed_document(document)
    chunk_result = validate_chunk_set(document, _chunks(document))

    assert parsed_result == ValidationResult(
        outcome=ValidationOutcome.WARN, reason_codes=expected_codes
    )
    assert chunk_result == parsed_result


def test_failed_or_empty_parse_fails_closed() -> None:
    failed_info = ParseInfo(
        outcome=ParseOutcome.FAILED,
        reliability=ParseReliability.UNAVAILABLE,
        parser_name="test-parser",
        parser_version="1",
        failure_code="PROVIDER_FAILED",
    )
    failed = DocumentContainer(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        source=_document().source,
        metadata=DocumentMetadata(),
        parse_info=failed_info,
        elements=(),
    )

    result = validate_parsed_document(failed)

    assert result.outcome is ValidationOutcome.FAIL
    assert ValidationReasonCode.PARSE_UNUSABLE in result.reason_codes
    assert ValidationReasonCode.DOCUMENT_EMPTY in result.reason_codes


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        (
            lambda document: _forge(document, document_id="doc_" + "0" * 64),
            ValidationReasonCode.DOCUMENT_ID_INVALID,
        ),
        (
            lambda document: _forge(document, revision_id="invalid"),
            ValidationReasonCode.DOCUMENT_REVISION_INVALID,
        ),
        (
            lambda document: _forge(
                document,
                source=_forge(document.source, source_type=cast(Any, "CSV")),
            ),
            ValidationReasonCode.DOCUMENT_SOURCE_INVALID,
        ),
        (
            lambda document: _forge(
                document,
                elements=tuple(
                    _forge(element, order=0) for element in document.elements
                ),
            ),
            ValidationReasonCode.ELEMENT_ORDER_INVALID,
        ),
        (
            lambda document: _forge(
                document,
                elements=(document.elements[0], document.elements[0]),
            ),
            ValidationReasonCode.ELEMENT_ID_DUPLICATE,
        ),
        (
            lambda document: _forge(
                document,
                elements=(
                    _forge(document.elements[0], document_id="doc_" + "0" * 64),
                    *document.elements[1:],
                ),
            ),
            ValidationReasonCode.ELEMENT_DOCUMENT_MISMATCH,
        ),
        (
            lambda document: _forge(
                document,
                elements=(
                    _forge(document.elements[0], revision_id=OTHER_REVISION_ID),
                    *document.elements[1:],
                ),
            ),
            ValidationReasonCode.ELEMENT_REVISION_MISMATCH,
        ),
        (
            lambda document: _forge(
                document,
                elements=(
                    _forge(document.elements[0], element_id="elem_" + "0" * 64),
                    *document.elements[1:],
                ),
            ),
            ValidationReasonCode.ELEMENT_ID_INVALID,
        ),
        (
            lambda document: _forge(
                document,
                elements=(
                    _forge(document.elements[0], section_path=("bad\nsection",)),
                    *document.elements[1:],
                ),
            ),
            ValidationReasonCode.SECTION_PATH_INVALID,
        ),
    ],
)
def test_parsed_validation_detects_forged_identity_and_structure(
    mutator: Any,
    expected_code: ValidationReasonCode,
) -> None:
    malformed = cast(DocumentContainer, mutator(_document()))

    result = validate_parsed_document(malformed)

    assert result.outcome is ValidationOutcome.FAIL
    assert expected_code in result.reason_codes
    _assert_canonical_reason_order(result)


def test_parsed_validation_detects_unicode_json_anchor_and_table_defects() -> None:
    document = _document()
    bad_text = _forge(document.elements[0], content="bad\ud800text")
    table_element = document.elements[2]
    assert table_element.table_data is not None
    bad_anchor = SourceAnchor(sheet_name="Other")
    bad_table = _forge(table_element.table_data, source_anchor=bad_anchor)
    bad_table_element = _forge(table_element, table_data=bad_table)
    malformed = _forge(
        document,
        elements=(
            bad_text,
            document.elements[1],
            bad_table_element,
            document.elements[3],
        ),
    )

    result = validate_parsed_document(malformed)

    assert result.outcome is ValidationOutcome.FAIL
    assert {
        ValidationReasonCode.DOCUMENT_SCHEMA_INVALID,
        ValidationReasonCode.UNICODE_INVALID,
        ValidationReasonCode.CANONICAL_JSON_INVALID,
        ValidationReasonCode.SOURCE_ANCHOR_INVALID,
        ValidationReasonCode.TABLE_INVARIANT_INVALID,
    } <= _result_codes(result)
    _assert_canonical_reason_order(result)


def test_table_element_content_must_match_its_canonical_table_rendering() -> None:
    document = _document()
    source_table = document.elements[2]
    assert source_table.element_type is ElementType.TABLE
    assert source_table.table_data is not None
    attacked_table = build_element(
        document_id=source_table.document_id,
        revision_id=source_table.revision_id,
        element_type=source_table.element_type,
        order=source_table.order,
        content="Attacker-controlled replacement table content",
        section_path=source_table.section_path,
        source_anchor=source_table.source_anchor,
        metadata=source_table.metadata,
        table_data=source_table.table_data,
    )
    attacked_document = _document(
        elements=(
            *document.elements[:2],
            attacked_table,
            *document.elements[3:],
        )
    )

    assert attacked_table.element_id != source_table.element_id
    DocumentContainer.model_validate(attacked_document)
    attacked_chunks = _chunks(attacked_document)

    expected = ValidationResult(
        outcome=ValidationOutcome.FAIL,
        reason_codes=(ValidationReasonCode.TABLE_INVARIANT_INVALID,),
    )
    assert validate_parsed_document(attacked_document) == expected
    assert validate_chunk_set(attacked_document, attacked_chunks) == expected


def test_parsed_validation_fails_closed_on_a_sparse_constructed_element() -> None:
    document = _document()
    sparse = CanonicalElement.model_construct(
        element_id=document.elements[0].element_id
    )
    malformed = _forge(document, elements=(sparse, *document.elements[1:]))

    result = validate_parsed_document(malformed)

    assert result.outcome is ValidationOutcome.FAIL
    assert ValidationReasonCode.DOCUMENT_SCHEMA_INVALID in result.reason_codes


def test_parsed_validation_fails_closed_on_a_forged_table_anchor_type() -> None:
    document = _document()
    table_element = document.elements[2]
    assert table_element.table_data is not None
    malformed_table = _forge(table_element.table_data, source_anchor=object())
    malformed_element = _forge(table_element, table_data=malformed_table)
    malformed = _forge(
        document,
        elements=(
            *document.elements[:2],
            malformed_element,
            *document.elements[3:],
        ),
    )

    result = validate_parsed_document(malformed)

    assert result.outcome is ValidationOutcome.FAIL
    assert {
        ValidationReasonCode.DOCUMENT_SCHEMA_INVALID,
        ValidationReasonCode.SOURCE_ANCHOR_INVALID,
        ValidationReasonCode.TABLE_INVARIANT_INVALID,
    } <= _result_codes(result)


def test_chunk_validation_rejects_an_empty_chunk_set() -> None:
    result = validate_chunk_set(_document(), ())

    assert result.outcome is ValidationOutcome.FAIL
    assert result.reason_codes == (
        ValidationReasonCode.CHUNK_SET_EMPTY,
        ValidationReasonCode.PARENT_COVERAGE_INVALID,
        ValidationReasonCode.TABLE_COVERAGE_INVALID,
    )


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        (
            lambda chunks: (
                chunks[0],
                _forge(chunks[1], chunk_id=chunks[0].chunk_id),
                *chunks[2:],
            ),
            ValidationReasonCode.CHUNK_ID_DUPLICATE,
        ),
        (
            lambda chunks: (
                _forge(chunks[0], order=4),
                *chunks[1:],
            ),
            ValidationReasonCode.CHUNK_ORDER_INVALID,
        ),
        (
            lambda chunks: (
                _forge(chunks[0], document_id="doc_" + "0" * 64),
                *chunks[1:],
            ),
            ValidationReasonCode.CHUNK_DOCUMENT_MISMATCH,
        ),
        (
            lambda chunks: (
                _forge(chunks[1], revision_id=OTHER_REVISION_ID),
                chunks[0],
                *chunks[2:],
            ),
            ValidationReasonCode.CHUNK_REVISION_MISMATCH,
        ),
        (
            lambda chunks: (
                _forge(chunks[0], retrieval_role=cast(Any, "RETRIEVABLE")),
                *chunks[1:],
            ),
            ValidationReasonCode.CHUNK_SHAPE_INVALID,
        ),
        (
            lambda chunks: (
                _forge(chunks[0], raw_content=""),
                *chunks[1:],
            ),
            ValidationReasonCode.CHUNK_CONTENT_EMPTY,
        ),
        (
            lambda chunks: (
                _forge(chunks[0], content_hash="0" * 64),
                *chunks[1:],
            ),
            ValidationReasonCode.CHUNK_CONTENT_HASH_INVALID,
        ),
        (
            lambda chunks: (
                _forge(chunks[0], chunk_id="chunk_" + "0" * 64),
                *chunks[1:],
            ),
            ValidationReasonCode.CHUNK_ID_INVALID,
        ),
    ],
)
def test_chunk_validation_detects_forged_core_invariants(
    mutator: Any,
    expected_code: ValidationReasonCode,
) -> None:
    malformed = cast(tuple[ChunkRecord, ...], mutator(_chunks()))

    result = validate_chunk_set(_document(), malformed)

    assert result.outcome is ValidationOutcome.FAIL
    assert expected_code in result.reason_codes
    _assert_canonical_reason_order(result)


def test_nonempty_whitespace_child_is_valid_when_exactly_source_backed() -> None:
    document = _document(elements=(_element(0, "a  b"),))
    chunks = build_chunks(
        document,
        ChunkingProfile(
            parent_max_chars=1,
            child_max_chars=1,
            table_slice_max_chars=58,
        ),
    )

    assert any(
        chunk.chunk_type is ChunkType.TEXT_CHILD
        and chunk.raw_content
        and not chunk.raw_content.strip()
        for chunk in chunks
    )
    assert validate_chunk_set(document, chunks).outcome is ValidationOutcome.PASS


def test_parent_reconstruction_accepts_the_profile_canonical_join_whitespace() -> None:
    document = _document(
        elements=(
            _element(0, "first"),
            _element(1, "second"),
        )
    )
    chunks = build_chunks(
        document,
        ChunkingProfile(
            parent_max_chars=100,
            child_max_chars=8,
            table_slice_max_chars=58,
            inter_element_separator="   ",
        ),
    )

    parent = next(
        chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_PARENT
    )
    assert parent.raw_content == "first   second"
    assert validate_chunk_set(document, chunks).outcome is ValidationOutcome.PASS


def test_chunk_validation_detects_broken_parent_reference_and_emission_order() -> None:
    chunks = list(_chunks())
    first_parent_index = next(
        index
        for index, chunk in enumerate(chunks)
        if chunk.chunk_type is ChunkType.TEXT_PARENT
    )
    child_index = first_parent_index + 1
    table_index = next(
        index
        for index, chunk in enumerate(chunks)
        if chunk.chunk_type is ChunkType.TABLE
    )
    chunks[child_index] = _forge(chunks[child_index], parent_id="chunk_" + "f" * 64)
    chunks[child_index], chunks[table_index] = chunks[table_index], chunks[child_index]
    malformed = tuple(_forge(chunk, order=index) for index, chunk in enumerate(chunks))

    result = validate_chunk_set(_document(), malformed)

    assert result.outcome is ValidationOutcome.FAIL
    assert {
        ValidationReasonCode.PARENT_REFERENCE_INVALID,
        ValidationReasonCode.PARENT_CHILD_ORDER_INVALID,
        ValidationReasonCode.CHUNK_SOURCE_ORDER_INVALID,
    } <= _result_codes(result)


def test_orphan_child_still_gets_independent_source_span_validation() -> None:
    document = _document()
    chunks = list(_chunks(document))
    child_index = next(
        index
        for index, chunk in enumerate(chunks)
        if chunk.chunk_type is ChunkType.TEXT_CHILD
    )
    child = chunks[child_index]
    assert isinstance(child.provenance, TextChildProvenance)
    source_span = child.provenance.source_spans[0]
    orphan_child = build_chunk(
        document_id=child.document_id,
        revision_id=child.revision_id,
        chunk_type=child.chunk_type,
        retrieval_role=child.retrieval_role,
        chunking_strategy=child.chunking_strategy,
        raw_content=child.raw_content,
        parent_id="chunk_" + "f" * 64,
        order=child.order,
        section_path=child.section_path,
        provenance=TextChildProvenance(
            source_spans=(
                ElementSpan(
                    element_id=source_span.element_id,
                    element_order=source_span.element_order,
                    char_start=100,
                    char_end=106,
                ),
            ),
            parent_char_start=child.provenance.parent_char_start,
            parent_char_end=child.provenance.parent_char_end,
        ),
    )
    assert ChunkRecord.model_validate(orphan_child) == orphan_child
    chunks[child_index] = orphan_child

    result = validate_chunk_set(document, tuple(chunks))

    assert result == ValidationResult(
        outcome=ValidationOutcome.FAIL,
        reason_codes=(
            ValidationReasonCode.PARENT_REFERENCE_INVALID,
            ValidationReasonCode.PARENT_CHILD_ORDER_INVALID,
            ValidationReasonCode.CHILD_LINEAGE_INVALID,
            ValidationReasonCode.CHILD_COVERAGE_INVALID,
        ),
    )


def test_nonadjacent_child_still_gets_independent_content_and_lineage_checks() -> None:
    document = _document()
    chunks = list(_chunks(document))
    child_index = next(
        index
        for index, chunk in enumerate(chunks)
        if chunk.chunk_type is ChunkType.TEXT_CHILD
    )
    child = chunks.pop(child_index)
    assert isinstance(child.provenance, TextChildProvenance)
    source_span = child.provenance.source_spans[0]
    source_element = next(
        element
        for element in document.elements
        if element.element_id == source_span.element_id
    )
    assert source_span.char_end < len(source_element.content)
    attacked_span = ElementSpan(
        element_id=source_span.element_id,
        element_order=source_span.element_order,
        char_start=source_span.char_start + 1,
        char_end=source_span.char_end + 1,
    )
    attacked_child = build_chunk(
        document_id=child.document_id,
        revision_id=child.revision_id,
        chunk_type=child.chunk_type,
        retrieval_role=child.retrieval_role,
        chunking_strategy=child.chunking_strategy,
        raw_content="X" * len(child.raw_content),
        parent_id=child.parent_id,
        order=child.order,
        section_path=child.section_path,
        provenance=TextChildProvenance(
            source_spans=(attacked_span, *child.provenance.source_spans[1:]),
            parent_char_start=child.provenance.parent_char_start,
            parent_char_end=child.provenance.parent_char_end,
        ),
    )
    assert ChunkRecord.model_validate(attacked_child) == attacked_child
    last_table_index = max(
        index
        for index, chunk in enumerate(chunks)
        if chunk.chunk_type is ChunkType.TABLE
    )
    chunks.insert(last_table_index + 1, attacked_child)
    attacked_chunks = tuple(
        _forge(chunk, order=index) for index, chunk in enumerate(chunks)
    )

    result = validate_chunk_set(document, attacked_chunks)

    assert result == ValidationResult(
        outcome=ValidationOutcome.FAIL,
        reason_codes=(
            ValidationReasonCode.PARENT_CHILD_ORDER_INVALID,
            ValidationReasonCode.CHUNK_SOURCE_ORDER_INVALID,
            ValidationReasonCode.CHILD_CONTENT_INVALID,
            ValidationReasonCode.CHILD_LINEAGE_INVALID,
            ValidationReasonCode.CHILD_COVERAGE_INVALID,
        ),
    )


def test_child_span_lookup_does_not_rescan_every_parent_source_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document(elements=tuple(_element(order, "x") for order in range(96)))
    chunks = build_chunks(
        document,
        ChunkingProfile(
            parent_max_chars=1_000,
            child_max_chars=3,
            table_slice_max_chars=58,
        ),
    )
    child_count = sum(chunk.chunk_type is ChunkType.TEXT_CHILD for chunk in chunks)
    original_parent_layout = validation_service._parent_layout
    probes: list[_CountingSequence] = []

    def instrumented_parent_layout(parent: ChunkRecord, elements: Any) -> Any:
        layout = original_parent_layout(parent, elements)
        if layout is not None:
            probe = _CountingSequence(layout.source_maps)
            object.__setattr__(layout, "source_maps", cast(Any, probe))
            probes.append(probe)
        return layout

    monkeypatch.setattr(
        validation_service,
        "_parent_layout",
        instrumented_parent_layout,
    )

    result = validate_chunk_set(document, chunks)

    assert result.outcome is ValidationOutcome.PASS
    source_map_count = sum(len(probe) for probe in probes)
    source_map_inspections = sum(probe.inspections for probe in probes)
    assert source_map_count == 96
    assert child_count >= 96
    assert source_map_inspections <= source_map_count + 8 * child_count


def test_chunk_validation_detects_parent_span_content_and_coverage_defects() -> None:
    chunks = list(_chunks())
    parent_index = next(
        index
        for index, chunk in enumerate(chunks)
        if chunk.chunk_type is ChunkType.TEXT_PARENT
    )
    parent = chunks[parent_index]
    assert isinstance(parent.provenance, TextParentProvenance)
    first_span = parent.provenance.source_spans[0]
    bad_span = _forge(first_span, char_start=-1)
    bad_provenance = _forge(
        parent.provenance,
        source_spans=(bad_span, *parent.provenance.source_spans[1:]),
    )
    chunks[parent_index] = _forge(parent, provenance=bad_provenance)

    result = validate_chunk_set(_document(), tuple(chunks))

    assert result.outcome is ValidationOutcome.FAIL
    assert {
        ValidationReasonCode.ELEMENT_SPAN_INVALID,
        ValidationReasonCode.PARENT_CONTENT_INVALID,
        ValidationReasonCode.PARENT_COVERAGE_INVALID,
    } <= _result_codes(result)


def test_chunk_validation_detects_child_range_substring_and_lineage_defects() -> None:
    chunks = list(_chunks())
    child_index = next(
        index
        for index, chunk in enumerate(chunks)
        if chunk.chunk_type is ChunkType.TEXT_CHILD
    )
    child = chunks[child_index]
    assert isinstance(child.provenance, TextChildProvenance)
    first_span = child.provenance.source_spans[0]
    bad_span = _forge(first_span, char_start=first_span.char_start + 1)
    bad_provenance = _forge(
        child.provenance,
        source_spans=(bad_span, *child.provenance.source_spans[1:]),
        parent_char_start=child.provenance.parent_char_start + 1,
    )
    chunks[child_index] = _forge(
        child,
        raw_content="not the parent substring",
        provenance=bad_provenance,
    )

    result = validate_chunk_set(_document(), tuple(chunks))

    assert result.outcome is ValidationOutcome.FAIL
    assert {
        ValidationReasonCode.CHILD_RANGE_INVALID,
        ValidationReasonCode.CHILD_CONTENT_INVALID,
        ValidationReasonCode.CHILD_LINEAGE_INVALID,
        ValidationReasonCode.CHILD_COVERAGE_INVALID,
    } <= _result_codes(result)


def test_chunk_validation_requires_every_table_row_once_with_exact_source_data() -> (
    None
):
    chunks = list(_chunks())
    table_indexes = [
        index
        for index, chunk in enumerate(chunks)
        if chunk.chunk_type is ChunkType.TABLE
    ]
    assert len(table_indexes) > 1
    table = chunks[table_indexes[0]]
    assert table.table_slice is not None
    bad_slice = _forge(
        table.table_slice,
        headers=("Wrong", "Header"),
        rows=(("Invented", "row"),),
    )
    chunks[table_indexes[0]] = _forge(table, table_slice=bad_slice)
    del chunks[table_indexes[-1]]
    malformed = tuple(_forge(chunk, order=index) for index, chunk in enumerate(chunks))

    result = validate_chunk_set(_document(), malformed)

    assert result.outcome is ValidationOutcome.FAIL
    assert {
        ValidationReasonCode.TABLE_SLICE_INVALID,
        ValidationReasonCode.TABLE_COVERAGE_INVALID,
    } <= _result_codes(result)


def test_chunk_validation_rejects_unknown_and_truncation_state() -> None:
    chunks = list(_chunks())
    object.__setattr__(chunks[0], "truncated", True)

    result = validate_chunk_set(_document(), tuple(chunks))

    assert result.outcome is ValidationOutcome.FAIL
    assert {
        ValidationReasonCode.CHUNK_UNKNOWN_FIELD,
        ValidationReasonCode.CHUNK_TRUNCATION_STATE,
    } <= _result_codes(result)


def test_chunk_validation_rejects_nonserializable_forged_content() -> None:
    chunks = list(_chunks())
    chunks[0] = _forge(chunks[0], raw_content=object())

    result = validate_chunk_set(_document(), tuple(chunks))

    assert result.outcome is ValidationOutcome.FAIL
    assert {
        ValidationReasonCode.CHUNK_SCHEMA_INVALID,
        ValidationReasonCode.CANONICAL_JSON_INVALID,
    } <= _result_codes(result)


def test_chunk_validation_fails_closed_on_a_sparse_constructed_chunk() -> None:
    chunks = list(_chunks())
    chunks[0] = ChunkRecord.model_construct(chunk_id=chunks[0].chunk_id)

    result = validate_chunk_set(_document(), tuple(chunks))

    assert result.outcome is ValidationOutcome.FAIL
    assert ValidationReasonCode.CHUNK_SCHEMA_INVALID in result.reason_codes


def test_chunk_validation_fails_closed_on_noncollection_child_spans() -> None:
    chunks = list(_chunks())
    child_index = next(
        index
        for index, chunk in enumerate(chunks)
        if chunk.chunk_type is ChunkType.TEXT_CHILD
    )
    child = chunks[child_index]
    assert isinstance(child.provenance, TextChildProvenance)
    parent = next(chunk for chunk in chunks if chunk.chunk_id == child.parent_id)
    assert parent.raw_content[0:6] == "Alpha "
    chunks[child_index] = _forge(
        child,
        raw_content="XXXXXX",
        provenance=_forge(
            child.provenance,
            source_spans=object(),
            parent_char_start=0,
            parent_char_end=6,
        ),
    )

    result = validate_chunk_set(_document(), tuple(chunks))

    assert result.outcome is ValidationOutcome.FAIL
    assert {
        ValidationReasonCode.CHUNK_SCHEMA_INVALID,
        ValidationReasonCode.CHILD_CONTENT_INVALID,
        ValidationReasonCode.CHILD_LINEAGE_INVALID,
    } <= _result_codes(result)
    assert ValidationReasonCode.CHILD_RANGE_INVALID not in result.reason_codes


def test_chunk_validation_fails_closed_on_noninteger_parent_span_bound() -> None:
    chunks = list(_chunks())
    parent_index = next(
        index
        for index, chunk in enumerate(chunks)
        if chunk.chunk_type is ChunkType.TEXT_PARENT
    )
    parent = chunks[parent_index]
    assert isinstance(parent.provenance, TextParentProvenance)
    spans = parent.provenance.source_spans
    malformed_span = _forge(spans[0], char_end=object())
    chunks[parent_index] = _forge(
        parent,
        provenance=_forge(
            parent.provenance,
            source_spans=(malformed_span, *spans[1:]),
        ),
    )

    result = validate_chunk_set(_document(), tuple(chunks))

    assert result.outcome is ValidationOutcome.FAIL
    assert {
        ValidationReasonCode.CHUNK_SCHEMA_INVALID,
        ValidationReasonCode.ELEMENT_SPAN_INVALID,
        ValidationReasonCode.PARENT_COVERAGE_INVALID,
    } <= _result_codes(result)


def test_table_chunk_must_reference_matching_table_element_and_slice_lineage() -> None:
    chunks = list(_chunks())
    table_index = next(
        index
        for index, chunk in enumerate(chunks)
        if chunk.chunk_type is ChunkType.TABLE
    )
    table = chunks[table_index]
    assert table.table_slice is not None
    bad_slice = _forge(table.table_slice, source_element_id="elem_" + "f" * 64)
    chunks[table_index] = _forge(table, table_slice=bad_slice)

    result = validate_chunk_set(_document(), tuple(chunks))

    assert result.outcome is ValidationOutcome.FAIL
    assert {
        ValidationReasonCode.TABLE_SOURCE_INVALID,
        ValidationReasonCode.TABLE_SLICE_INVALID,
    } <= _result_codes(result)
