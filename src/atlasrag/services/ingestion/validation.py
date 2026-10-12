"""Pure fail-closed validation for canonical Stage 3 production output."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from atlasrag._canonical import canonical_sha256, validate_unicode_scalar_text
from atlasrag.domain.enums import SourceType
from atlasrag.domain.ingestion.chunks import (
    ChunkingStrategy,
    ChunkRecord,
    ChunkType,
    ElementSpan,
    RetrievalRole,
    TableProvenance,
    TableSlice,
    TableSliceMode,
    TextChildProvenance,
    TextParentProvenance,
    derive_chunk_content_hash,
    derive_chunk_id,
    render_table_slice,
)
from atlasrag.domain.ingestion.document import DocumentContainer, ParseInfo
from atlasrag.domain.ingestion.elements import (
    CanonicalElement,
    TableData,
    derive_element_id,
    normalize_canonical_text,
    normalize_section_path,
    normalize_source_anchor,
    render_table_data,
)
from atlasrag.domain.ingestion.enums import ElementType, ParseOutcome, ParseReliability
from atlasrag.domain.ingestion.source import (
    CanonicalSource,
    derive_document_id,
    source_type_for_path,
    validate_document_id,
    validate_revision_id,
)
from atlasrag.domain.ingestion.validation import (
    ValidationReasonCode,
    ValidationResult,
    outcome_for_reason_codes,
)

_ModelT = TypeVar("_ModelT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class _ParentSourceMap:
    parent_start: int
    parent_end: int
    span: ElementSpan


@dataclass(frozen=True, slots=True)
class _ParentLayout:
    expected_content: str
    source_maps: tuple[_ParentSourceMap, ...]
    source_ends: tuple[int, ...]
    join_separator: str | None


def _result(reasons: set[ValidationReasonCode]) -> ValidationResult:
    ordered = tuple(code for code in ValidationReasonCode if code in reasons)
    return ValidationResult(
        outcome=outcome_for_reason_codes(ordered),
        reason_codes=ordered,
    )


def _revalidates(model_type: type[_ModelT], value: object) -> bool:
    try:
        model_type.model_validate(value)
    except (ValidationError, TypeError, ValueError, RecursionError):
        return False
    return True


def _unicode_is_safe(value: object) -> bool:
    pending = [value]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            try:
                validate_unicode_scalar_text(current)
            except ValueError:
                return False
            continue
        if isinstance(current, BaseModel):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            pending.extend(vars(current).values())
            continue
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            for key, item in current.items():
                pending.append(key)
                pending.append(item)
            continue
        if isinstance(current, (list, tuple, set, frozenset)):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            pending.extend(current)
    return True


def _model_json_is_safe(value: object) -> bool:
    if not isinstance(value, BaseModel):
        return False
    try:
        canonical_sha256(value.model_dump(mode="python", warnings="error"))
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def _chunk_json_is_safe(chunks: Sequence[object]) -> bool:
    try:
        payload = [
            chunk.model_dump(mode="python", warnings="error")
            if isinstance(chunk, BaseModel)
            else chunk
            for chunk in chunks
        ]
        canonical_sha256(payload)
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def _unknown_model_fields(value: object) -> tuple[str, ...]:
    names: list[str] = []
    pending = [value]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, BaseModel):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            declared = set(type(current).model_fields)
            values = vars(current)
            actual = set(values)
            names.extend(sorted(actual - declared))
            extra = getattr(current, "__pydantic_extra__", None)
            if isinstance(extra, Mapping):
                names.extend(sorted(str(name) for name in extra))
            nested = [values[name] for name in sorted(declared & actual)]
            pending.extend(reversed(nested))
            continue
        if isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            pending.extend(reversed(current))
    return tuple(names)


def _has_all_model_fields(value: BaseModel) -> bool:
    return set(value.__class__.model_fields) <= vars(value).keys()


def _parsed_reason_codes(document: DocumentContainer) -> set[ValidationReasonCode]:
    reasons: set[ValidationReasonCode] = set()
    if not isinstance(document, DocumentContainer) or not _revalidates(
        DocumentContainer, document
    ):
        reasons.add(ValidationReasonCode.DOCUMENT_SCHEMA_INVALID)
    if isinstance(document, BaseModel) and _unknown_model_fields(document):
        reasons.add(ValidationReasonCode.DOCUMENT_SCHEMA_INVALID)

    document_id = getattr(document, "document_id", None)
    source = getattr(document, "source", None)
    try:
        if not isinstance(document_id, str):
            raise TypeError("document_id is not text")
        validate_document_id(document_id)
        if not isinstance(source, CanonicalSource) or not _has_all_model_fields(source):
            raise TypeError("source is not canonical")
        if document_id != derive_document_id(source.relative_source_path):
            raise ValueError("document identity mismatch")
    except (TypeError, ValueError, ValidationError):
        reasons.add(ValidationReasonCode.DOCUMENT_ID_INVALID)

    try:
        if not isinstance(source, CanonicalSource) or not _has_all_model_fields(source):
            raise TypeError("source is not canonical")
        CanonicalSource.model_validate(source)
        if not isinstance(source.source_type, SourceType):
            raise TypeError("source type is not closed")
        if source_type_for_path(source.relative_source_path) is not source.source_type:
            raise ValueError("source type mismatch")
    except (TypeError, ValueError, ValidationError, RecursionError):
        reasons.add(ValidationReasonCode.DOCUMENT_SOURCE_INVALID)

    revision_id = getattr(document, "revision_id", None)
    try:
        if not isinstance(revision_id, str):
            raise TypeError("revision_id is not text")
        validate_revision_id(revision_id)
    except (TypeError, ValueError):
        reasons.add(ValidationReasonCode.DOCUMENT_REVISION_INVALID)

    parse_info = getattr(document, "parse_info", None)
    if not isinstance(parse_info, ParseInfo) or not _revalidates(ParseInfo, parse_info):
        reasons.add(ValidationReasonCode.PARSE_UNUSABLE)
    else:
        if parse_info.outcome is ParseOutcome.DEGRADED:
            reasons.add(ValidationReasonCode.PARSE_DEGRADED)
        if parse_info.reliability is ParseReliability.PARTIAL:
            reasons.add(ValidationReasonCode.PARSE_PARTIAL)
        if (
            parse_info.outcome is ParseOutcome.FAILED
            or parse_info.reliability is ParseReliability.UNAVAILABLE
        ):
            reasons.add(ValidationReasonCode.PARSE_UNUSABLE)

    raw_elements = getattr(document, "elements", ())
    elements = tuple(raw_elements) if isinstance(raw_elements, (list, tuple)) else ()
    if not elements:
        reasons.add(ValidationReasonCode.DOCUMENT_EMPTY)
    if not isinstance(raw_elements, tuple):
        reasons.add(ValidationReasonCode.DOCUMENT_SCHEMA_INVALID)

    orders = [getattr(element, "order", None) for element in elements]
    orders_are_valid = True
    previous_order: int | None = None
    for order in orders:
        if type(order) is not int or order < 0:
            orders_are_valid = False
            break
        if previous_order is not None and previous_order >= order:
            orders_are_valid = False
            break
        previous_order = order
    if not orders_are_valid:
        reasons.add(ValidationReasonCode.ELEMENT_ORDER_INVALID)
    element_ids = [
        candidate_id
        for element in elements
        if isinstance(element, CanonicalElement)
        if isinstance(candidate_id := getattr(element, "element_id", None), str)
    ]
    if len(element_ids) != len(set(element_ids)):
        reasons.add(ValidationReasonCode.ELEMENT_ID_DUPLICATE)

    for element in elements:
        if not isinstance(element, CanonicalElement) or not _revalidates(
            CanonicalElement, element
        ):
            reasons.add(ValidationReasonCode.DOCUMENT_SCHEMA_INVALID)
        if not isinstance(element, CanonicalElement):
            continue
        if _unknown_model_fields(element):
            reasons.add(ValidationReasonCode.DOCUMENT_SCHEMA_INVALID)
        if not _has_all_model_fields(element):
            continue
        if element.document_id != document_id:
            reasons.add(ValidationReasonCode.ELEMENT_DOCUMENT_MISMATCH)
        if element.revision_id != revision_id:
            reasons.add(ValidationReasonCode.ELEMENT_REVISION_MISMATCH)
        try:
            expected_element_id = derive_element_id(
                revision_id=element.revision_id,
                order=element.order,
                element_type=element.element_type,
                content=element.content,
                section_path=element.section_path,
                source_anchor=element.source_anchor,
                table_data=element.table_data,
            )
            if element.element_id != expected_element_id:
                raise ValueError("element identity mismatch")
        except (TypeError, ValueError, ValidationError, RecursionError):
            reasons.add(ValidationReasonCode.ELEMENT_ID_INVALID)

        try:
            if (
                not isinstance(element.section_path, tuple)
                or normalize_section_path(element.section_path) != element.section_path
            ):
                raise ValueError("noncanonical section path")
        except (TypeError, ValueError, ValidationError):
            reasons.add(ValidationReasonCode.SECTION_PATH_INVALID)
        try:
            if normalize_source_anchor(element.source_anchor) != element.source_anchor:
                raise ValueError("noncanonical source anchor")
        except (TypeError, ValueError, ValidationError, RecursionError):
            reasons.add(ValidationReasonCode.SOURCE_ANCHOR_INVALID)

        table_data = element.table_data
        if element.element_type is ElementType.TABLE:
            table_is_valid = (
                isinstance(table_data, TableData)
                and _has_all_model_fields(table_data)
                and _revalidates(TableData, table_data)
            )
            if not table_is_valid:
                reasons.add(ValidationReasonCode.TABLE_INVARIANT_INVALID)
            if isinstance(table_data, TableData) and _has_all_model_fields(table_data):
                if table_is_valid and element.content != render_table_data(table_data):
                    reasons.add(ValidationReasonCode.TABLE_INVARIANT_INVALID)
                if element.source_anchor != table_data.source_anchor:
                    reasons.add(ValidationReasonCode.SOURCE_ANCHOR_INVALID)
                    reasons.add(ValidationReasonCode.TABLE_INVARIANT_INVALID)
                anchor_sheet = getattr(table_data.source_anchor, "sheet_name", None)
                if (
                    table_data.sheet_name is not None
                    and anchor_sheet is not None
                    and table_data.sheet_name != anchor_sheet
                ):
                    reasons.add(ValidationReasonCode.SOURCE_ANCHOR_INVALID)
                    reasons.add(ValidationReasonCode.TABLE_INVARIANT_INVALID)
        elif table_data is not None:
            reasons.add(ValidationReasonCode.TABLE_INVARIANT_INVALID)

    if not _unicode_is_safe(document):
        reasons.add(ValidationReasonCode.UNICODE_INVALID)
    if not _model_json_is_safe(document):
        reasons.add(ValidationReasonCode.CANONICAL_JSON_INVALID)
    return reasons


def validate_parsed_document(document: DocumentContainer) -> ValidationResult:
    """Validate one parsed candidate without repairing or replacing any value."""
    return _result(_parsed_reason_codes(document))


def _chunk_shape_is_valid(chunk: ChunkRecord) -> bool:
    if chunk.chunk_type is ChunkType.TEXT_PARENT:
        return (
            chunk.retrieval_role is RetrievalRole.CONTEXT_ONLY
            and chunk.chunking_strategy is ChunkingStrategy.STRUCTURE
            and chunk.parent_id is None
            and isinstance(chunk.provenance, TextParentProvenance)
            and chunk.table_slice is None
        )
    if chunk.chunk_type is ChunkType.TEXT_CHILD:
        return (
            chunk.retrieval_role is RetrievalRole.RETRIEVABLE
            and chunk.chunking_strategy is ChunkingStrategy.RECURSIVE
            and isinstance(chunk.parent_id, str)
            and isinstance(chunk.provenance, TextChildProvenance)
            and chunk.table_slice is None
        )
    if chunk.chunk_type is ChunkType.TABLE:
        return (
            chunk.retrieval_role is RetrievalRole.RETRIEVABLE
            and chunk.chunking_strategy is ChunkingStrategy.TABLE
            and chunk.parent_id is None
            and isinstance(chunk.provenance, TableProvenance)
            and isinstance(chunk.table_slice, TableSlice)
        )
    return False


def _element_lookup(document: DocumentContainer) -> dict[str, CanonicalElement]:
    raw_elements = getattr(document, "elements", ())
    if not isinstance(raw_elements, (list, tuple)):
        return {}
    lookup: dict[str, CanonicalElement] = {}
    for element in raw_elements:
        if (
            isinstance(element, CanonicalElement)
            and _has_all_model_fields(element)
            and _revalidates(CanonicalElement, element)
            and isinstance(element.element_id, str)
        ):
            lookup.setdefault(element.element_id, element)
    return lookup


def _spans_are_valid(
    spans: object,
    elements: Mapping[str, CanonicalElement],
    *,
    section_path: object,
) -> bool:
    if not isinstance(spans, tuple) or not spans:
        return False
    previous: ElementSpan | None = None
    for span in spans:
        if (
            not isinstance(span, ElementSpan)
            or not _has_all_model_fields(span)
            or not _revalidates(ElementSpan, span)
        ):
            return False
        element = elements.get(span.element_id)
        if (
            element is None
            or element.element_type is ElementType.TABLE
            or span.element_order != element.order
            or type(span.char_start) is not int
            or type(span.char_end) is not int
            or not 0 <= span.char_start < span.char_end <= len(element.content)
            or section_path != element.section_path
        ):
            return False
        if previous is not None and (
            span.element_order < previous.element_order
            or (
                span.element_order == previous.element_order
                and (
                    span.element_id != previous.element_id
                    or span.char_start < previous.char_end
                )
            )
        ):
            return False
        previous = span
    return True


def _parent_layout(
    parent: ChunkRecord,
    elements: Mapping[str, CanonicalElement],
) -> _ParentLayout | None:
    provenance = parent.provenance
    if (
        not isinstance(provenance, TextParentProvenance)
        or not _has_all_model_fields(provenance)
        or not _spans_are_valid(
            provenance.source_spans,
            elements,
            section_path=parent.section_path,
        )
    ):
        return None
    if not isinstance(parent.raw_content, str):
        return None
    source_length = sum(
        span.char_end - span.char_start for span in provenance.source_spans
    )
    transition_count = sum(
        left.element_id != right.element_id
        for left, right in zip(
            provenance.source_spans,
            provenance.source_spans[1:],
        )
    )
    separator_length = 0
    if transition_count:
        separator_total = len(parent.raw_content) - source_length
        if separator_total <= 0 or separator_total % transition_count:
            return None
        separator_length = separator_total // transition_count
    elif len(parent.raw_content) != source_length:
        return None

    parts: list[str] = []
    source_maps: list[_ParentSourceMap] = []
    rendered_length = 0
    previous_id: str | None = None
    join_separator: str | None = None
    for span in provenance.source_spans:
        element = elements[span.element_id]
        if previous_id is not None and previous_id != span.element_id:
            candidate_separator = parent.raw_content[
                rendered_length : rendered_length + separator_length
            ]
            try:
                canonical_separator = normalize_canonical_text(
                    candidate_separator, allow_empty=True
                )
            except ValueError:
                return None
            if (
                not canonical_separator
                or not canonical_separator.isspace()
                or canonical_separator != candidate_separator
                or (
                    join_separator is not None and candidate_separator != join_separator
                )
            ):
                return None
            join_separator = candidate_separator
            parts.append(candidate_separator)
            rendered_length += separator_length
        text = element.content[span.char_start : span.char_end]
        start = rendered_length
        parts.append(text)
        rendered_length += len(text)
        source_maps.append(
            _ParentSourceMap(
                parent_start=start,
                parent_end=rendered_length,
                span=span,
            )
        )
        previous_id = span.element_id
    canonical_source_maps = tuple(source_maps)
    return _ParentLayout(
        "".join(parts),
        canonical_source_maps,
        tuple(source.parent_end for source in canonical_source_maps),
        join_separator,
    )


def _parent_coverage_is_complete(
    document: DocumentContainer,
    parents: Sequence[ChunkRecord],
) -> bool:
    raw_elements = getattr(document, "elements", ())
    if not isinstance(raw_elements, (list, tuple)):
        return False
    expected = [
        element
        for element in raw_elements
        if isinstance(element, CanonicalElement)
        and _has_all_model_fields(element)
        and _revalidates(CanonicalElement, element)
        and element.element_type is not ElementType.TABLE
    ]
    spans: list[ElementSpan] = []
    for parent in parents:
        provenance = parent.provenance
        if (
            not isinstance(provenance, TextParentProvenance)
            or not _has_all_model_fields(provenance)
            or not isinstance(provenance.source_spans, tuple)
            or not all(
                isinstance(span, ElementSpan)
                and _has_all_model_fields(span)
                and _revalidates(ElementSpan, span)
                for span in provenance.source_spans
            )
        ):
            return False
        spans.extend(provenance.source_spans)
    element_index = 0
    character_index = 0
    for span in spans:
        if element_index >= len(expected):
            return False
        element = expected[element_index]
        if (
            span.element_id != element.element_id
            or span.element_order != element.order
            or span.char_start != character_index
            or span.char_end <= span.char_start
            or span.char_end > len(element.content)
        ):
            return False
        character_index = span.char_end
        if character_index == len(element.content):
            element_index += 1
            character_index = 0
    return element_index == len(expected) and character_index == 0


def _span_signature(span: ElementSpan) -> tuple[str, int, int, int]:
    return (
        span.element_id,
        span.element_order,
        span.char_start,
        span.char_end,
    )


def _expected_child_spans(
    layout: _ParentLayout,
    start: int,
    end: int,
) -> tuple[tuple[str, int, int, int], ...]:
    expected: list[tuple[str, int, int, int]] = []
    first_overlap = bisect_right(layout.source_ends, start)
    for source_index in range(first_overlap, len(layout.source_maps)):
        source = layout.source_maps[source_index]
        if source.parent_start >= end:
            break
        overlap_start = max(start, source.parent_start)
        overlap_end = min(end, source.parent_end)
        if overlap_start >= overlap_end:
            continue
        source_offset = overlap_start - source.parent_start
        expected.append(
            (
                source.span.element_id,
                source.span.element_order,
                source.span.char_start + source_offset,
                source.span.char_start + source_offset + overlap_end - overlap_start,
            )
        )
    return tuple(expected)


def _validate_table_chunks(
    document: DocumentContainer,
    chunks: Sequence[ChunkRecord],
    reasons: set[ValidationReasonCode],
) -> None:
    elements = _element_lookup(document)
    grouped: dict[str, list[ChunkRecord]] = {}
    for chunk in chunks:
        if chunk.chunk_type is not ChunkType.TABLE:
            continue
        provenance = chunk.provenance
        table_slice = chunk.table_slice
        if not isinstance(provenance, TableProvenance) or not isinstance(
            table_slice, TableSlice
        ):
            reasons.add(ValidationReasonCode.TABLE_SOURCE_INVALID)
            reasons.add(ValidationReasonCode.TABLE_SLICE_INVALID)
            continue
        if not _has_all_model_fields(provenance) or not _has_all_model_fields(
            table_slice
        ):
            reasons.add(ValidationReasonCode.TABLE_SOURCE_INVALID)
            reasons.add(ValidationReasonCode.TABLE_SLICE_INVALID)
            continue
        if (
            not isinstance(provenance.source_element_id, str)
            or type(provenance.source_element_order) is not int
        ):
            reasons.add(ValidationReasonCode.TABLE_SOURCE_INVALID)
            reasons.add(ValidationReasonCode.TABLE_SLICE_INVALID)
            continue
        source = elements.get(provenance.source_element_id)
        lineage_matches = (
            table_slice.source_element_id == provenance.source_element_id
            and table_slice.row_start == provenance.row_start
            and table_slice.row_end == provenance.row_end
            and table_slice.sheet_name == provenance.sheet_name
        )
        if not lineage_matches:
            reasons.add(ValidationReasonCode.TABLE_SOURCE_INVALID)
            reasons.add(ValidationReasonCode.TABLE_SLICE_INVALID)
        if (
            source is None
            or source.element_type is not ElementType.TABLE
            or source.order != provenance.source_element_order
            or source.section_path != chunk.section_path
            or not isinstance(source.table_data, TableData)
            or not _has_all_model_fields(source.table_data)
            or not _revalidates(TableData, source.table_data)
        ):
            reasons.add(ValidationReasonCode.TABLE_SOURCE_INVALID)
            continue
        data = source.table_data
        start = table_slice.row_start
        end = table_slice.row_end
        valid_range = (
            type(start) is int
            and type(end) is int
            and 0 <= start < end <= len(data.rows)
        )
        slice_matches = (
            valid_range
            and table_slice.source_element_id == source.element_id
            and table_slice.headers == data.headers
            and table_slice.rows == data.rows[start:end]
            and table_slice.caption == data.caption
            and table_slice.sheet_name == data.sheet_name
            and table_slice.source_anchor == data.source_anchor
            and table_slice.mode in (TableSliceMode.FULL, TableSliceMode.ROW_GROUP)
        )
        try:
            rendered_matches = chunk.raw_content == render_table_slice(table_slice)
        except (TypeError, ValueError, ValidationError, RecursionError):
            rendered_matches = False
        if not slice_matches or not lineage_matches or not rendered_matches:
            reasons.add(ValidationReasonCode.TABLE_SLICE_INVALID)
        grouped.setdefault(source.element_id, []).append(chunk)

    raw_elements = getattr(document, "elements", ())
    if not isinstance(raw_elements, (list, tuple)):
        reasons.add(ValidationReasonCode.TABLE_COVERAGE_INVALID)
        return
    for element in raw_elements:
        if (
            not isinstance(element, CanonicalElement)
            or not _has_all_model_fields(element)
            or not _revalidates(CanonicalElement, element)
            or element.element_type is not ElementType.TABLE
        ):
            continue
        if (
            not isinstance(element.table_data, TableData)
            or not _has_all_model_fields(element.table_data)
            or not _revalidates(TableData, element.table_data)
        ):
            reasons.add(ValidationReasonCode.TABLE_COVERAGE_INVALID)
            continue
        source_chunks = grouped.get(element.element_id, [])
        if not source_chunks:
            reasons.add(ValidationReasonCode.TABLE_COVERAGE_INVALID)
            continue
        slices = [chunk.table_slice for chunk in source_chunks]
        if not all(
            isinstance(item, TableSlice)
            and _has_all_model_fields(item)
            and _revalidates(TableSlice, item)
            for item in slices
        ):
            reasons.add(ValidationReasonCode.TABLE_COVERAGE_INVALID)
            continue
        typed_slices = [item for item in slices if isinstance(item, TableSlice)]
        expected_mode = (
            TableSliceMode.FULL if len(typed_slices) == 1 else TableSliceMode.ROW_GROUP
        )
        cursor = 0
        covered_rows: list[tuple[str, ...]] = []
        coverage_valid = True
        for table_slice in typed_slices:
            if (
                table_slice.mode is not expected_mode
                or table_slice.row_start != cursor
                or table_slice.row_end <= table_slice.row_start
            ):
                coverage_valid = False
            cursor = table_slice.row_end
            covered_rows.extend(table_slice.rows)
        if (
            not coverage_valid
            or cursor != len(element.table_data.rows)
            or tuple(covered_rows) != element.table_data.rows
        ):
            reasons.add(ValidationReasonCode.TABLE_COVERAGE_INVALID)


def validate_chunk_set(
    document: DocumentContainer,
    chunks: Sequence[ChunkRecord],
) -> ValidationResult:
    """Validate one complete ordered chunk set against its parsed document."""
    reasons = _parsed_reason_codes(document)
    if not isinstance(chunks, (list, tuple)):
        reasons.add(ValidationReasonCode.CHUNK_SCHEMA_INVALID)
        return _result(reasons)
    ordered_chunks = tuple(chunks)
    if not ordered_chunks:
        reasons.add(ValidationReasonCode.CHUNK_SET_EMPTY)

    if not _unicode_is_safe(ordered_chunks):
        reasons.add(ValidationReasonCode.UNICODE_INVALID)
    if not _chunk_json_is_safe(ordered_chunks):
        reasons.add(ValidationReasonCode.CANONICAL_JSON_INVALID)

    canonical_chunks: list[ChunkRecord] = []
    for chunk in ordered_chunks:
        if not isinstance(chunk, ChunkRecord):
            reasons.add(ValidationReasonCode.CHUNK_SCHEMA_INVALID)
            continue
        if not _revalidates(ChunkRecord, chunk):
            reasons.add(ValidationReasonCode.CHUNK_SCHEMA_INVALID)
        unknown_fields = _unknown_model_fields(chunk)
        if unknown_fields:
            reasons.add(ValidationReasonCode.CHUNK_UNKNOWN_FIELD)
        if any("truncat" in name.casefold() for name in unknown_fields):
            reasons.add(ValidationReasonCode.CHUNK_TRUNCATION_STATE)
        if not _has_all_model_fields(chunk):
            continue
        canonical_chunks.append(chunk)
        if not _chunk_shape_is_valid(chunk):
            reasons.add(ValidationReasonCode.CHUNK_SHAPE_INVALID)
        if chunk.document_id != getattr(document, "document_id", None):
            reasons.add(ValidationReasonCode.CHUNK_DOCUMENT_MISMATCH)
        if chunk.revision_id != getattr(document, "revision_id", None):
            reasons.add(ValidationReasonCode.CHUNK_REVISION_MISMATCH)

        raw_content = chunk.raw_content
        if not isinstance(raw_content, str):
            reasons.add(ValidationReasonCode.CHUNK_SCHEMA_INVALID)
            reasons.add(ValidationReasonCode.CHUNK_CONTENT_HASH_INVALID)
        else:
            if raw_content == "":
                reasons.add(ValidationReasonCode.CHUNK_CONTENT_EMPTY)
            try:
                if (
                    normalize_canonical_text(raw_content, allow_empty=True)
                    != raw_content
                ):
                    raise ValueError("chunk content is not canonical")
                expected_hash = derive_chunk_content_hash(raw_content)
                if chunk.content_hash != expected_hash:
                    reasons.add(ValidationReasonCode.CHUNK_CONTENT_HASH_INVALID)
            except (TypeError, ValueError):
                reasons.add(ValidationReasonCode.CHUNK_CONTENT_HASH_INVALID)

        try:
            expected_chunk_id = derive_chunk_id(
                document_id=chunk.document_id,
                revision_id=chunk.revision_id,
                chunk_type=chunk.chunk_type,
                chunking_strategy=chunk.chunking_strategy,
                provenance=chunk.provenance,
                content_hash=chunk.content_hash,
            )
            if chunk.chunk_id != expected_chunk_id:
                raise ValueError("chunk identity mismatch")
        except (TypeError, ValueError, ValidationError, RecursionError):
            reasons.add(ValidationReasonCode.CHUNK_ID_INVALID)
        try:
            if (
                not isinstance(chunk.section_path, tuple)
                or normalize_section_path(chunk.section_path) != chunk.section_path
            ):
                raise ValueError("chunk section path is not canonical")
        except (TypeError, ValueError, ValidationError):
            reasons.add(ValidationReasonCode.CHUNK_SHAPE_INVALID)

    if len(canonical_chunks) != len(ordered_chunks):
        reasons.add(ValidationReasonCode.CHUNK_SCHEMA_INVALID)
    orders = [chunk.order for chunk in canonical_chunks]
    if orders != list(range(len(ordered_chunks))):
        reasons.add(ValidationReasonCode.CHUNK_ORDER_INVALID)
    chunk_ids = [
        chunk.chunk_id for chunk in canonical_chunks if isinstance(chunk.chunk_id, str)
    ]
    if len(chunk_ids) != len(set(chunk_ids)):
        reasons.add(ValidationReasonCode.CHUNK_ID_DUPLICATE)

    elements = _element_lookup(document)
    parents = [
        chunk for chunk in canonical_chunks if chunk.chunk_type is ChunkType.TEXT_PARENT
    ]
    children = [
        chunk for chunk in canonical_chunks if chunk.chunk_type is ChunkType.TEXT_CHILD
    ]
    parent_by_id: dict[str, ChunkRecord] = {}
    for parent in parents:
        if isinstance(parent.chunk_id, str):
            parent_by_id.setdefault(parent.chunk_id, parent)

    immediate_children: dict[int, list[ChunkRecord]] = {
        id(parent): [] for parent in parents
    }
    active_parent: ChunkRecord | None = None
    for chunk in canonical_chunks:
        if chunk.chunk_type is ChunkType.TEXT_PARENT:
            if active_parent is not None and not immediate_children[id(active_parent)]:
                reasons.add(ValidationReasonCode.PARENT_CHILD_ORDER_INVALID)
            active_parent = chunk
            continue
        if chunk.chunk_type is ChunkType.TEXT_CHILD:
            if active_parent is None or chunk.parent_id != active_parent.chunk_id:
                reasons.add(ValidationReasonCode.PARENT_CHILD_ORDER_INVALID)
            else:
                immediate_children[id(active_parent)].append(chunk)
            continue
        if active_parent is not None and not immediate_children[id(active_parent)]:
            reasons.add(ValidationReasonCode.PARENT_CHILD_ORDER_INVALID)
        active_parent = None
    if active_parent is not None and not immediate_children[id(active_parent)]:
        reasons.add(ValidationReasonCode.PARENT_CHILD_ORDER_INVALID)

    referenced_parents: dict[int, ChunkRecord] = {}
    for child in children:
        candidate_parent = (
            parent_by_id.get(child.parent_id)
            if isinstance(child.parent_id, str)
            else None
        )
        if (
            candidate_parent is not None
            and candidate_parent.chunk_type is ChunkType.TEXT_PARENT
        ):
            referenced_parents[id(child)] = candidate_parent
        if (
            candidate_parent is None
            or candidate_parent.chunk_type is not ChunkType.TEXT_PARENT
            or child.document_id != candidate_parent.document_id
            or child.revision_id != candidate_parent.revision_id
        ):
            reasons.add(ValidationReasonCode.PARENT_REFERENCE_INVALID)

    layouts: dict[int, _ParentLayout] = {}
    for parent in parents:
        provenance = parent.provenance
        spans_valid = (
            isinstance(provenance, TextParentProvenance)
            and _has_all_model_fields(provenance)
            and _spans_are_valid(
                provenance.source_spans,
                elements,
                section_path=parent.section_path,
            )
        )
        if not spans_valid:
            reasons.add(ValidationReasonCode.ELEMENT_SPAN_INVALID)
        layout = _parent_layout(parent, elements)
        if layout is None:
            reasons.add(ValidationReasonCode.PARENT_CONTENT_INVALID)
            continue
        layouts[id(parent)] = layout
        if parent.raw_content != layout.expected_content:
            reasons.add(ValidationReasonCode.PARENT_CONTENT_INVALID)
    used_separators = {
        layout.join_separator
        for layout in layouts.values()
        if layout.join_separator is not None
    }
    if len(used_separators) > 1:
        reasons.add(ValidationReasonCode.PARENT_CONTENT_INVALID)
    if not _parent_coverage_is_complete(document, parents):
        reasons.add(ValidationReasonCode.PARENT_COVERAGE_INVALID)

    for child in children:
        provenance = child.provenance
        if not isinstance(provenance, TextChildProvenance):
            reasons.add(ValidationReasonCode.CHILD_RANGE_INVALID)
            reasons.add(ValidationReasonCode.CHILD_LINEAGE_INVALID)
            continue

        provenance_values = vars(provenance)
        source_spans: object = provenance_values.get("source_spans")
        spans_valid = _spans_are_valid(
            source_spans,
            elements,
            section_path=child.section_path,
        )
        if not spans_valid:
            reasons.add(ValidationReasonCode.CHILD_LINEAGE_INVALID)

        start_value = provenance_values.get("parent_char_start")
        end_value = provenance_values.get("parent_char_end")
        start = start_value if type(start_value) is int else None
        end = end_value if type(end_value) is int else None
        range_shape_valid = (
            start is not None
            and end is not None
            and isinstance(child.raw_content, str)
            and 0 <= start < end
            and end - start == len(child.raw_content)
        )
        if not range_shape_valid:
            reasons.add(ValidationReasonCode.CHILD_RANGE_INVALID)
            reasons.add(ValidationReasonCode.CHILD_LINEAGE_INVALID)

        referenced_parent = referenced_parents.get(id(child))
        if referenced_parent is None:
            continue
        range_valid = (
            range_shape_valid
            and start is not None
            and end is not None
            and isinstance(referenced_parent.raw_content, str)
            and end <= len(referenced_parent.raw_content)
        )
        if not range_valid:
            reasons.add(ValidationReasonCode.CHILD_RANGE_INVALID)
        if (
            not range_valid
            or start is None
            or end is None
            or child.raw_content != referenced_parent.raw_content[start:end]
        ):
            reasons.add(ValidationReasonCode.CHILD_CONTENT_INVALID)
        layout = layouts.get(id(referenced_parent))
        expected_spans = (
            _expected_child_spans(layout, start, end)
            if layout is not None
            and range_valid
            and start is not None
            and end is not None
            else ()
        )
        actual_spans = (
            tuple(_span_signature(span) for span in source_spans)
            if isinstance(source_spans, tuple)
            and all(
                isinstance(span, ElementSpan) and _has_all_model_fields(span)
                for span in source_spans
            )
            else ()
        )
        if (
            not spans_valid
            or not expected_spans
            or actual_spans != expected_spans
            or child.section_path != referenced_parent.section_path
        ):
            reasons.add(ValidationReasonCode.CHILD_LINEAGE_INVALID)

    for parent in parents:
        parent_children = immediate_children[id(parent)]
        coverage_cursor = 0
        coverage_valid = bool(parent_children)
        for child in parent_children:
            provenance = child.provenance
            if not isinstance(
                provenance, TextChildProvenance
            ) or not _has_all_model_fields(provenance):
                reasons.add(ValidationReasonCode.CHILD_RANGE_INVALID)
                reasons.add(ValidationReasonCode.CHILD_LINEAGE_INVALID)
                coverage_valid = False
                continue
            start = provenance.parent_char_start
            end = provenance.parent_char_end
            coverage_range_valid = (
                type(start) is int
                and type(end) is int
                and isinstance(parent.raw_content, str)
                and 0 <= start < end <= len(parent.raw_content)
            )
            if not coverage_range_valid or start != coverage_cursor:
                coverage_valid = False
            if type(end) is int:
                coverage_cursor = end
        if not isinstance(parent.raw_content, str) or coverage_cursor != len(
            parent.raw_content
        ):
            coverage_valid = False
        if not coverage_valid:
            reasons.add(ValidationReasonCode.CHILD_COVERAGE_INVALID)

    previous_source_order = -1
    source_order_valid = True
    for chunk in canonical_chunks:
        marker_start: int | None = None
        marker_end: int | None = None
        if chunk.chunk_type is ChunkType.TEXT_PARENT:
            layout = layouts.get(id(chunk))
            if layout is not None and layout.source_maps:
                marker_start = layout.source_maps[0].span.element_order
                marker_end = layout.source_maps[-1].span.element_order
        elif chunk.chunk_type is ChunkType.TEXT_CHILD:
            referenced_parent = (
                parent_by_id.get(chunk.parent_id)
                if isinstance(chunk.parent_id, str)
                else None
            )
            layout = (
                layouts.get(id(referenced_parent))
                if referenced_parent is not None
                else None
            )
            if layout is not None and layout.source_maps:
                marker_start = layout.source_maps[-1].span.element_order
                marker_end = marker_start
        elif (
            chunk.chunk_type is ChunkType.TABLE
            and isinstance(chunk.provenance, TableProvenance)
            and _has_all_model_fields(chunk.provenance)
        ):
            if type(chunk.provenance.source_element_order) is int:
                marker_start = chunk.provenance.source_element_order
                marker_end = marker_start
            else:
                source_order_valid = False
        if marker_start is None or marker_end is None:
            continue
        if marker_start < previous_source_order or marker_end < marker_start:
            source_order_valid = False
        previous_source_order = max(previous_source_order, marker_end)
    if not source_order_valid:
        reasons.add(ValidationReasonCode.CHUNK_SOURCE_ORDER_INVALID)

    _validate_table_chunks(document, canonical_chunks, reasons)
    return _result(reasons)


validate_document = validate_parsed_document
validate_chunks = validate_chunk_set
