"""Pure lossless construction of canonical Parent, Child, and Table chunks."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import (
    BeforeValidator,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from atlasrag._canonical import canonical_sha256
from atlasrag.domain.base import FrozenModel
from atlasrag.domain.ingestion.chunks import (
    ChunkRecord,
    ChunkType,
    ElementSpan,
    RetrievalRole,
    TableProvenance,
    TableSlice,
    TableSliceMode,
    TextChildProvenance,
    TextParentProvenance,
    render_table_slice,
)
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.elements import (
    CanonicalElement,
    TableData,
    normalize_canonical_text,
    normalize_single_line,
)
from atlasrag.services.ingestion.analysis import (
    ChunkRouter,
    DocumentAnalyzer,
    TablePosition,
    TextRun,
)
from atlasrag.services.ingestion.identity import build_chunk

_CHUNKER_ALGORITHM_VERSION = "lossless-structure-recursive-table-v7"
DEFAULT_CHUNKER_BEHAVIOR_VERSION = "chunker-v7"
PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT = (
    "46aba26b91b69ccd6ba414561b594b88f49df674077cf0ef8344b04039737fcc"
)
_ASCII_SENTENCE_PUNCTUATION = frozenset(".!?")
_CJK_SENTENCE_PUNCTUATION = frozenset("。！？")
_SENTENCE_CLOSERS = frozenset("\"'”’)]")
_PositiveStrictInt = Annotated[StrictInt, Field(gt=0)]


def _strict_profile_schema_version(value: object) -> object:
    if type(value) is not int or value != 1:
        raise ValueError("profile_schema_version must be the integer 1")
    return value


def _strict_zero_overlap(value: object) -> object:
    if type(value) is not int or value != 0:
        raise ValueError("child_overlap_chars must be the integer 0")
    return value


_ProfileSchemaVersion = Annotated[
    Literal[1], BeforeValidator(_strict_profile_schema_version)
]
_ZeroOverlap = Annotated[Literal[0], BeforeValidator(_strict_zero_overlap)]


class ChunkingProfile(FrozenModel):
    """Internal immutable collection of all output-affecting chunk policies."""

    profile_schema_version: _ProfileSchemaVersion = 1
    parent_max_chars: _PositiveStrictInt = 8_000
    child_max_chars: _PositiveStrictInt = 1_500
    table_slice_max_chars: _PositiveStrictInt = 8_000
    child_overlap_chars: _ZeroOverlap = 0
    inter_element_separator: StrictStr = "\n\n"

    @field_validator("inter_element_separator")
    @classmethod
    def _canonical_separator(cls, value: str) -> str:
        normalized = normalize_canonical_text(value, allow_empty=True)
        if not normalized or not normalized.isspace():
            raise ValueError("inter_element_separator must be nonempty whitespace")
        return normalized

    @model_validator(mode="after")
    def _partitionable_separator_gap(self) -> Self:
        separator_length = len(self.inter_element_separator)
        maximum_minimal_atoms = (self.parent_max_chars + separator_length) // (
            separator_length + 1
        )
        minimal_rendered_length = (
            maximum_minimal_atoms + (maximum_minimal_atoms - 1) * separator_length
        )
        source_backed_capacity = maximum_minimal_atoms * self.child_max_chars
        if (
            maximum_minimal_atoms >= 2
            and minimal_rendered_length > source_backed_capacity
        ):
            raise ValueError(
                "inter_element_separator gap cannot retain typed Child lineage"
            )
        return self

    @property
    def profile_fingerprint(self) -> str:
        """Identify the complete profile plus the implemented algorithm family."""
        return canonical_sha256(
            {
                "algorithm_version": _CHUNKER_ALGORITHM_VERSION,
                "profile": self.model_dump(mode="json"),
            }
        )


def require_chunker_behavior_version_change(
    *,
    previous_profile: ChunkingProfile,
    previous_version: str,
    current_profile: ChunkingProfile,
    current_version: str,
) -> None:
    """Reject profile drift hidden behind one PartI chunker behavior version."""
    previous = ChunkingProfile.model_validate(previous_profile)
    current = ChunkingProfile.model_validate(current_profile)
    canonical_previous_version = normalize_single_line(previous_version)
    canonical_current_version = normalize_single_line(current_version)
    for profile, version in (
        (previous, canonical_previous_version),
        (current, canonical_current_version),
    ):
        if (
            version == DEFAULT_CHUNKER_BEHAVIOR_VERSION
            and profile.profile_fingerprint
            != PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT
        ):
            raise ValueError(
                "chunker_behavior_version must change when ChunkingProfile changes"
            )
    if (
        previous.profile_fingerprint != current.profile_fingerprint
        and canonical_previous_version == canonical_current_version
    ):
        raise ValueError(
            "chunker_behavior_version must change when ChunkingProfile changes"
        )


@dataclass(frozen=True, slots=True)
class _Atom:
    text: str
    span: ElementSpan


@dataclass(frozen=True, slots=True)
class _SourceMap:
    parent_start: int
    parent_end: int
    span: ElementSpan


@dataclass(frozen=True, slots=True)
class _ParentDraft:
    raw_content: str
    section_path: tuple[str, ...]
    source_spans: tuple[ElementSpan, ...]
    source_maps: tuple[_SourceMap, ...]


@dataclass(frozen=True, slots=True)
class _EndpointCategory:
    flags: tuple[bool, ...]
    latest_valid_starts: tuple[int, ...]


_EndpointCategories = tuple[_EndpointCategory, ...]


def _sentence_endpoint_latest_starts(text: str) -> list[int]:
    """Index sentence endpoints in two forward-only O(n) passes."""
    text_length = len(text)
    latest_starts = [-1] * (text_length + 1)
    position = 0
    while position < text_length:
        character = text[position]
        if character in _ASCII_SENTENCE_PUNCTUATION:
            punctuation = _ASCII_SENTENCE_PUNCTUATION
            requires_whitespace = True
        elif character in _CJK_SENTENCE_PUNCTUATION:
            punctuation = _CJK_SENTENCE_PUNCTUATION
            requires_whitespace = False
        else:
            position += 1
            continue

        while position < text_length and text[position] in punctuation:
            position += 1
        punctuation_end = position
        while position < text_length and text[position] in _SENTENCE_CLOSERS:
            position += 1
        if not requires_whitespace or (
            position < text_length and text[position].isspace()
        ):
            # A chunk starting at the last punctuation scalar still sees the
            # same sentence endpoint; one starting in the closers does not.
            latest_starts[position] = punctuation_end - 1

    for end in range(1, text_length + 1):
        if latest_starts[end] >= 0:
            continue
        if text[end - 1].isspace() and latest_starts[end - 1] >= 0:
            latest_starts[end] = latest_starts[end - 1]
    return latest_starts


def _split_endpoint_categories(text: str) -> _EndpointCategories:
    """Precompute ordered paragraph-to-scalar endpoint categories in O(n)."""
    endpoint_count = len(text) + 1
    paragraph_flags = [False] * endpoint_count
    paragraph_starts = [-1] * endpoint_count
    newline_flags = [False] * endpoint_count
    newline_starts = [-1] * endpoint_count
    sentence_starts = _sentence_endpoint_latest_starts(text)
    whitespace_flags = [False] * endpoint_count
    whitespace_starts = [-1] * endpoint_count
    scalar_flags = [False] * endpoint_count
    scalar_starts = [-1] * endpoint_count

    for end in range(1, endpoint_count):
        character = text[end - 1]
        scalar_flags[end] = True
        scalar_starts[end] = end - 1
        if character.isspace():
            whitespace_flags[end] = True
            whitespace_starts[end] = end - 1
        if character == "\n":
            newline_flags[end] = True
            newline_starts[end] = end - 1
            if end >= 2 and text[end - 2] == "\n":
                paragraph_flags[end] = True
                paragraph_starts[end] = end - 2

    sentence_flags = tuple(latest_start >= 0 for latest_start in sentence_starts)
    return (
        _EndpointCategory(tuple(paragraph_flags), tuple(paragraph_starts)),
        _EndpointCategory(tuple(newline_flags), tuple(newline_starts)),
        _EndpointCategory(sentence_flags, tuple(sentence_starts)),
        _EndpointCategory(tuple(whitespace_flags), tuple(whitespace_starts)),
        _EndpointCategory(tuple(scalar_flags), tuple(scalar_starts)),
    )


def _next_matching_positions(matches: list[bool]) -> list[int]:
    text_length = len(matches)
    positions = [text_length] * (text_length + 1)
    next_position = text_length
    for index in range(text_length - 1, -1, -1):
        if matches[index]:
            next_position = index
        positions[index] = next_position
    return positions


def _previous_matching_positions(matches: Sequence[bool]) -> list[int]:
    positions = [-1] * len(matches)
    previous_position = -1
    for index, matches_at_index in enumerate(matches):
        if matches_at_index:
            previous_position = index
        positions[index] = previous_position
    return positions


def _previous_feasible_category_endpoints(
    categories: _EndpointCategories,
    feasible: Sequence[bool],
) -> tuple[list[int], ...]:
    return tuple(
        _previous_matching_positions(
            [
                category.flags[end] and feasible_at_end
                for end, feasible_at_end in enumerate(feasible)
            ]
        )
        for category in categories
    )


def _priority_feasible_end(
    categories: _EndpointCategories,
    previous_feasible: tuple[list[int], ...],
    *,
    start: int,
    minimum_end: int,
    hard_end: int,
) -> int | None:
    """Choose the farthest feasible endpoint in the first available category."""
    for category, previous_positions in zip(categories, previous_feasible, strict=True):
        end = previous_positions[hard_end]
        # Valid-start bounds increase with endpoints, so an invalid farthest
        # candidate means every earlier candidate in this category is invalid.
        if end >= minimum_end and category.latest_valid_starts[end] >= start:
            return end
    return None


def _source_position_flags(
    text_length: int, source_maps: tuple[_SourceMap, ...]
) -> list[bool]:
    events = [0] * (text_length + 1)
    for source in source_maps:
        if not 0 <= source.parent_start < source.parent_end <= text_length:
            raise ValueError("internal source map is outside Parent content")
        events[source.parent_start] += 1
        events[source.parent_end] -= 1
    flags: list[bool] = []
    active = 0
    for index in range(text_length):
        active += events[index]
        flags.append(active > 0)
    return flags


def _plan_split_ranges(
    text: str,
    maximum: int,
    *,
    next_source: list[int] | None,
    require_nonwhitespace: bool,
    endpoint_categories: _EndpointCategories | None = None,
) -> list[tuple[int, int]] | None:
    text_length = len(text)
    categories = (
        _split_endpoint_categories(text)
        if endpoint_categories is None
        else endpoint_categories
    )
    next_nonwhitespace = (
        _next_matching_positions([not character.isspace() for character in text])
        if require_nonwhitespace
        else None
    )

    feasible = [False] * (text_length + 1)
    feasible[text_length] = True
    no_position = text_length + 1
    next_feasible = [no_position] * (text_length + 2)
    next_feasible[text_length] = text_length
    for start in range(text_length - 1, -1, -1):
        required_position = start
        if next_nonwhitespace is not None:
            required_position = max(required_position, next_nonwhitespace[start])
        if next_source is not None:
            required_position = max(required_position, next_source[start])
        minimum_end = required_position + 1
        hard_end = min(start + maximum, text_length)
        if minimum_end <= hard_end and next_feasible[minimum_end] <= hard_end:
            feasible[start] = True
            next_feasible[start] = start
        else:
            next_feasible[start] = next_feasible[start + 1]

    if not feasible[0]:
        return None
    previous_feasible = _previous_feasible_category_endpoints(categories, feasible)

    ranges: list[tuple[int, int]] = []
    start = 0
    while start < text_length:
        required_position = start
        if next_nonwhitespace is not None:
            required_position = max(required_position, next_nonwhitespace[start])
        if next_source is not None:
            required_position = max(required_position, next_source[start])
        minimum_end = required_position + 1
        hard_end = min(start + maximum, text_length)
        if hard_end == text_length:
            end = hard_end
        else:
            selected_end = _priority_feasible_end(
                categories,
                previous_feasible,
                start=start,
                minimum_end=minimum_end,
                hard_end=hard_end,
            )
            if selected_end is None:
                raise ValueError("internal lossless split feasibility mismatch")
            end = selected_end
        ranges.append((start, end))
        start = end
    return ranges


def _split_ranges(
    text: str,
    maximum: int,
    *,
    source_maps: tuple[_SourceMap, ...] | None = None,
) -> list[tuple[int, int]]:
    """Split losslessly, using blank source-backed ranges only when unavoidable."""
    text_length = len(text)
    endpoint_categories = _split_endpoint_categories(text)
    next_source = (
        _next_matching_positions(_source_position_flags(text_length, source_maps))
        if source_maps is not None
        else None
    )
    nonblank_ranges = _plan_split_ranges(
        text,
        maximum,
        next_source=next_source,
        require_nonwhitespace=True,
        endpoint_categories=endpoint_categories,
    )
    if nonblank_ranges is not None:
        return nonblank_ranges
    source_backed_ranges = _plan_split_ranges(
        text,
        maximum,
        next_source=next_source,
        require_nonwhitespace=False,
        endpoint_categories=endpoint_categories,
    )
    if source_backed_ranges is not None:
        return source_backed_ranges
    if source_maps is None:
        raise ValueError("parent_max_chars cannot preserve lossless structural atoms")
    raise ValueError("child_max_chars cannot preserve nonempty typed source lineage")


def _element_atoms(element: CanonicalElement, maximum: int) -> tuple[_Atom, ...]:
    atoms: list[_Atom] = []
    for start, end in _split_ranges(element.content, maximum):
        atoms.append(
            _Atom(
                text=element.content[start:end],
                span=ElementSpan(
                    element_id=element.element_id,
                    element_order=element.order,
                    char_start=start,
                    char_end=end,
                ),
            )
        )
    return tuple(atoms)


def _parent_drafts(run: TextRun, profile: ChunkingProfile) -> tuple[_ParentDraft, ...]:
    drafts: list[_ParentDraft] = []
    parts: list[str] = []
    spans: list[ElementSpan] = []
    maps: list[_SourceMap] = []
    rendered_length = 0

    def flush() -> None:
        nonlocal parts, spans, maps, rendered_length
        if not parts:
            return
        raw_content = "".join(parts)
        if len(raw_content) != rendered_length:
            raise ValueError("internal Parent rendering length mismatch")
        drafts.append(
            _ParentDraft(
                raw_content=raw_content,
                section_path=run.section_path,
                source_spans=tuple(spans),
                source_maps=tuple(maps),
            )
        )
        parts = []
        spans = []
        maps = []
        rendered_length = 0

    for element in run.elements:
        for atom in _element_atoms(element, profile.parent_max_chars):
            previous_span = spans[-1] if spans else None
            needs_separator = (
                previous_span is not None
                and previous_span.element_id != atom.span.element_id
            )
            separator = profile.inter_element_separator if needs_separator else ""
            candidate_length = rendered_length + len(separator) + len(atom.text)
            if parts and candidate_length > profile.parent_max_chars:
                flush()
                separator = ""
                candidate_length = len(atom.text)
            if candidate_length > profile.parent_max_chars:
                raise ValueError("structural atom exceeds parent_max_chars")
            if separator:
                parts.append(separator)
                rendered_length += len(separator)
            source_start = rendered_length
            parts.append(atom.text)
            rendered_length += len(atom.text)
            spans.append(atom.span)
            maps.append(
                _SourceMap(
                    parent_start=source_start,
                    parent_end=rendered_length,
                    span=atom.span,
                )
            )
    flush()
    return tuple(drafts)


def _child_ranges(
    text: str,
    maximum: int,
    source_maps: tuple[_SourceMap, ...],
) -> tuple[tuple[int, int], ...]:
    return tuple(_split_ranges(text, maximum, source_maps=source_maps))


def _child_source_span_groups(
    child_ranges: tuple[tuple[int, int], ...],
    source_maps: Sequence[_SourceMap],
) -> tuple[tuple[ElementSpan, ...], ...]:
    """Map ordered Child ranges to source spans with one forward-only sweep."""
    groups: list[tuple[ElementSpan, ...]] = []
    source_index = 0
    source_count = len(source_maps)
    previous_end = 0
    for start, end in child_ranges:
        if start != previous_end or end <= start:
            raise ValueError("internal Child ranges must be contiguous and nonempty")
        spans: list[ElementSpan] = []
        while source_index < source_count:
            source = source_maps[source_index]
            if source.parent_start >= end:
                break
            overlap_start = max(start, source.parent_start)
            overlap_end = min(end, source.parent_end)
            if overlap_start < overlap_end:
                offset = overlap_start - source.parent_start
                spans.append(
                    ElementSpan(
                        element_id=source.span.element_id,
                        element_order=source.span.element_order,
                        char_start=source.span.char_start + offset,
                        char_end=source.span.char_start
                        + offset
                        + (overlap_end - overlap_start),
                    )
                )
            if source.parent_end <= end:
                source_index += 1
                continue
            break
        if not spans:
            raise ValueError("Child must retain nonempty typed source lineage")
        groups.append(tuple(spans))
        previous_end = end
    return tuple(groups)


def _escape_table_cell(cell: str) -> str:
    return (
        cell.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
    )


def _table_rendering_parts(table: TableData) -> tuple[str, tuple[str, ...]]:
    prefix_lines: list[str] = []
    if table.caption is not None:
        prefix_lines.append(table.caption)
    prefix_lines.append(
        "| " + " | ".join(_escape_table_cell(cell) for cell in table.headers) + " |"
    )
    prefix_lines.append("| " + " | ".join("---" for _ in table.headers) + " |")
    row_lines = tuple(
        "| " + " | ".join(_escape_table_cell(cell) for cell in row) + " |"
        for row in table.rows
    )
    return "\n".join(prefix_lines), row_lines


def _table_slices(
    element: CanonicalElement, profile: ChunkingProfile
) -> tuple[TableSlice, ...]:
    table = element.table_data
    if table is None:
        raise ValueError("TABLE element requires table_data")
    prefix, row_lines = _table_rendering_parts(table)
    limit = profile.table_slice_max_chars
    escaped_headers = tuple(_escape_table_cell(cell) for cell in table.headers)
    escaped_cells = tuple(
        _escape_table_cell(cell) for row in table.rows for cell in row
    )
    if any(len(cell) > limit for cell in escaped_headers):
        raise ValueError("table header cannot fit without data loss")
    if any(len(cell) > limit for cell in escaped_cells):
        raise ValueError("table cell cannot fit without data loss")
    if len(prefix) >= limit:
        raise ValueError("table header block leaves no room for a complete row")
    row_costs = tuple(1 + len(line) for line in row_lines)
    if any(len(prefix) + cost > limit for cost in row_costs):
        raise ValueError("table row cannot fit without data loss")

    full_length = len(prefix) + sum(row_costs)
    if full_length <= limit:
        return (
            TableSlice(
                mode=TableSliceMode.FULL,
                source_element_id=element.element_id,
                headers=table.headers,
                rows=table.rows,
                row_start=0,
                row_end=len(table.rows),
                caption=table.caption,
                sheet_name=table.sheet_name,
                source_anchor=table.source_anchor,
            ),
        )

    slices: list[TableSlice] = []
    group_rows: list[tuple[str, ...]] = []
    group_start = 0
    rendered_length = len(prefix)
    for row_index, (row, cost) in enumerate(zip(table.rows, row_costs, strict=True)):
        if group_rows and rendered_length + cost > limit:
            slices.append(
                TableSlice(
                    mode=TableSliceMode.ROW_GROUP,
                    source_element_id=element.element_id,
                    headers=table.headers,
                    rows=tuple(group_rows),
                    row_start=group_start,
                    row_end=row_index,
                    caption=table.caption,
                    sheet_name=table.sheet_name,
                    source_anchor=table.source_anchor,
                )
            )
            group_rows = []
            group_start = row_index
            rendered_length = len(prefix)
        group_rows.append(row)
        rendered_length += cost
    if group_rows:
        slices.append(
            TableSlice(
                mode=TableSliceMode.ROW_GROUP,
                source_element_id=element.element_id,
                headers=table.headers,
                rows=tuple(group_rows),
                row_start=group_start,
                row_end=len(table.rows),
                caption=table.caption,
                sheet_name=table.sheet_name,
                source_anchor=table.source_anchor,
            )
        )
    return tuple(slices)


class CanonicalChunker:
    """Pure deterministic executor for one immutable chunking profile."""

    def __init__(
        self,
        profile: ChunkingProfile | None = None,
        *,
        analyzer: DocumentAnalyzer | None = None,
        router: ChunkRouter | None = None,
    ) -> None:
        self._profile = ChunkingProfile.model_validate(
            ChunkingProfile() if profile is None else profile
        )
        self._analyzer = DocumentAnalyzer() if analyzer is None else analyzer
        self._router = ChunkRouter() if router is None else router

    @property
    def profile(self) -> ChunkingProfile:
        return self._profile

    def chunk(self, document: DocumentContainer) -> tuple[ChunkRecord, ...]:
        """Build canonical chunks in deterministic contiguous output order."""
        analysis = self._analyzer.analyze(document)
        chunks: list[ChunkRecord] = []
        for item in analysis.items:
            if isinstance(item, TextRun):
                for draft in _parent_drafts(item, self._profile):
                    parent = build_chunk(
                        document_id=analysis.document_id,
                        revision_id=analysis.revision_id,
                        chunk_type=ChunkType.TEXT_PARENT,
                        retrieval_role=RetrievalRole.CONTEXT_ONLY,
                        chunking_strategy=self._router.parent_strategy,
                        raw_content=draft.raw_content,
                        parent_id=None,
                        order=len(chunks),
                        section_path=draft.section_path,
                        provenance=TextParentProvenance(
                            source_spans=draft.source_spans
                        ),
                    )
                    chunks.append(parent)
                    child_ranges = _child_ranges(
                        draft.raw_content,
                        self._profile.child_max_chars,
                        draft.source_maps,
                    )
                    child_span_groups = _child_source_span_groups(
                        child_ranges, draft.source_maps
                    )
                    for (start, end), source_spans in zip(
                        child_ranges, child_span_groups, strict=True
                    ):
                        chunks.append(
                            build_chunk(
                                document_id=analysis.document_id,
                                revision_id=analysis.revision_id,
                                chunk_type=ChunkType.TEXT_CHILD,
                                retrieval_role=RetrievalRole.RETRIEVABLE,
                                chunking_strategy=self._router.child_strategy,
                                raw_content=draft.raw_content[start:end],
                                parent_id=parent.chunk_id,
                                order=len(chunks),
                                section_path=draft.section_path,
                                provenance=TextChildProvenance(
                                    source_spans=source_spans,
                                    parent_char_start=start,
                                    parent_char_end=end,
                                ),
                            )
                        )
                continue

            if not isinstance(item, TablePosition):
                raise TypeError("unsupported analysis item")
            for table_slice in _table_slices(item.element, self._profile):
                chunks.append(
                    build_chunk(
                        document_id=analysis.document_id,
                        revision_id=analysis.revision_id,
                        chunk_type=ChunkType.TABLE,
                        retrieval_role=RetrievalRole.RETRIEVABLE,
                        chunking_strategy=self._router.table_strategy,
                        raw_content=render_table_slice(table_slice),
                        parent_id=None,
                        order=len(chunks),
                        section_path=item.element.section_path,
                        provenance=TableProvenance(
                            source_element_id=item.element.element_id,
                            source_element_order=item.element.order,
                            row_start=table_slice.row_start,
                            row_end=table_slice.row_end,
                            sheet_name=table_slice.sheet_name,
                        ),
                        table_slice=table_slice,
                    )
                )
        return tuple(chunks)


def build_chunks(
    document: DocumentContainer,
    profile: ChunkingProfile | None = None,
) -> tuple[ChunkRecord, ...]:
    """Build chunks using one explicit profile or the frozen Stage 3 defaults."""
    return CanonicalChunker(profile).chunk(document)


chunk_document = build_chunks
