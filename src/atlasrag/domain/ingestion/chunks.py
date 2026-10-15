"""Immutable canonical chunks and typed structural-lineage contracts."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Self, TypeAlias

from pydantic import (
    BeforeValidator,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from atlasrag._canonical import canonical_sha256
from atlasrag.domain.base import (
    FrozenModel,
    validate_ordered_collection_input,
    validate_string_enum_input,
)
from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.ingestion.elements import (
    TableData,
    normalize_canonical_text,
    normalize_section_path,
    normalize_single_line,
    normalize_source_anchor,
    render_table_data,
)
from atlasrag.domain.ingestion.source import (
    validate_document_id,
    validate_revision_id,
    validate_sha256_hex,
)

_ELEMENT_ID = re.compile(r"^elem_[0-9a-f]{64}$")
_CHUNK_ID = re.compile(r"^chunk_[0-9a-f]{64}$")


class ChunkType(StrEnum):
    """Canonical chunk kinds, including nonretrievable text Parents."""

    TEXT_PARENT = "TEXT_PARENT"
    TEXT_CHILD = "TEXT_CHILD"
    TABLE = "TABLE"


class RetrievalRole(StrEnum):
    """Whether a canonical chunk is searchable or context-only."""

    CONTEXT_ONLY = "CONTEXT_ONLY"
    RETRIEVABLE = "RETRIEVABLE"


class ChunkingStrategy(StrEnum):
    """The only strategies selected by the Stage 3 Part I chunker."""

    STRUCTURE = "STRUCTURE"
    RECURSIVE = "RECURSIVE"
    TABLE = "TABLE"


class TableSliceMode(StrEnum):
    """Lossless table slicing modes implemented by Stage 3."""

    FULL = "FULL"
    ROW_GROUP = "ROW_GROUP"


_ChunkType = Annotated[ChunkType, BeforeValidator(validate_string_enum_input)]
_RetrievalRole = Annotated[RetrievalRole, BeforeValidator(validate_string_enum_input)]
_ChunkingStrategy = Annotated[
    ChunkingStrategy, BeforeValidator(validate_string_enum_input)
]
_TableSliceMode = Annotated[TableSliceMode, BeforeValidator(validate_string_enum_input)]
_SectionPath = Annotated[
    tuple[StrictStr, ...], BeforeValidator(validate_ordered_collection_input)
]


def _validate_element_id(value: str) -> str:
    if _ELEMENT_ID.fullmatch(value) is None:
        raise ValueError("element_id must be a canonical element identity")
    return value


def _validate_chunk_id(value: str) -> str:
    if _CHUNK_ID.fullmatch(value) is None:
        raise ValueError("chunk_id must be a canonical chunk identity")
    return value


class ElementSpan(FrozenModel):
    """One nonempty half-open range in canonical element content."""

    element_id: StrictStr
    element_order: Annotated[StrictInt, Field(ge=0)]
    char_start: Annotated[StrictInt, Field(ge=0)]
    char_end: Annotated[StrictInt, Field(ge=1)]

    _canonical_element_id = field_validator("element_id")(_validate_element_id)

    @model_validator(mode="after")
    def _nonempty_range(self) -> Self:
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        return self


_ElementSpans = Annotated[
    tuple[ElementSpan, ...], BeforeValidator(validate_ordered_collection_input)
]


def _validate_source_spans(
    source_spans: tuple[ElementSpan, ...],
) -> tuple[ElementSpan, ...]:
    if not source_spans:
        raise ValueError("source_spans must be nonempty")
    seen_ids: dict[int, str] = {}
    seen_orders: dict[str, int] = {}
    previous: ElementSpan | None = None
    for span in source_spans:
        existing_id = seen_ids.setdefault(span.element_order, span.element_id)
        if existing_id != span.element_id:
            raise ValueError("one element_order must identify exactly one element_id")
        existing_order = seen_orders.setdefault(span.element_id, span.element_order)
        if existing_order != span.element_order:
            raise ValueError("one element_id must identify exactly one element_order")
        if previous is not None:
            if span.element_order < previous.element_order:
                raise ValueError("source_spans must be ordered by element order")
            if span.element_order == previous.element_order:
                if span.element_id != previous.element_id:
                    raise ValueError("source_spans have contradictory element identity")
                if span.char_start < previous.char_end:
                    raise ValueError("source_spans must not overlap")
        previous = span
    return source_spans


class TextParentProvenance(FrozenModel):
    """Ordered canonical element ranges rendered into one text Parent."""

    source_spans: _ElementSpans

    _ordered_nonoverlapping_spans = field_validator("source_spans")(
        _validate_source_spans
    )


class TextChildProvenance(FrozenModel):
    """Element lineage plus the exact half-open range in one text Parent."""

    source_spans: _ElementSpans
    parent_char_start: Annotated[StrictInt, Field(ge=0)]
    parent_char_end: Annotated[StrictInt, Field(ge=1)]

    _ordered_nonoverlapping_spans = field_validator("source_spans")(
        _validate_source_spans
    )

    @model_validator(mode="after")
    def _nonempty_parent_range(self) -> Self:
        if self.parent_char_end <= self.parent_char_start:
            raise ValueError("parent_char_end must be greater than parent_char_start")
        return self


class TableProvenance(FrozenModel):
    """One exact data-row range from a canonical TABLE element."""

    source_element_id: StrictStr
    source_element_order: Annotated[StrictInt, Field(ge=0)]
    row_start: Annotated[StrictInt, Field(ge=0)]
    row_end: Annotated[StrictInt, Field(ge=1)]
    sheet_name: StrictStr | None = None

    _canonical_source_element_id = field_validator("source_element_id")(
        _validate_element_id
    )

    @field_validator("sheet_name")
    @classmethod
    def _canonical_sheet_name(cls, value: str | None) -> str | None:
        return None if value is None else normalize_single_line(value)

    @model_validator(mode="after")
    def _nonempty_row_range(self) -> Self:
        if self.row_end <= self.row_start:
            raise ValueError("row_end must be greater than row_start")
        return self


def _ordered_rows_input(value: object) -> object:
    ordered = validate_ordered_collection_input(value)
    assert isinstance(ordered, (list, tuple))
    for row in ordered:
        validate_ordered_collection_input(row)
    return ordered


_Headers = Annotated[
    tuple[StrictStr, ...], BeforeValidator(validate_ordered_collection_input)
]
_Rows = Annotated[
    tuple[tuple[StrictStr, ...], ...], BeforeValidator(_ordered_rows_input)
]


class TableSlice(FrozenModel):
    """A complete-header, complete-row subtable with exact source lineage."""

    mode: _TableSliceMode
    source_element_id: StrictStr
    headers: _Headers
    rows: _Rows
    row_start: Annotated[StrictInt, Field(ge=0)]
    row_end: Annotated[StrictInt, Field(ge=1)]
    caption: StrictStr | None = None
    sheet_name: StrictStr | None = None
    source_anchor: SourceAnchor | None = None

    _canonical_source_element_id = field_validator("source_element_id")(
        _validate_element_id
    )

    @field_validator("headers")
    @classmethod
    def _canonical_headers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(normalize_canonical_text(cell, allow_empty=True) for cell in value)

    @field_validator("rows")
    @classmethod
    def _canonical_rows(
        cls, value: tuple[tuple[str, ...], ...]
    ) -> tuple[tuple[str, ...], ...]:
        return tuple(
            tuple(normalize_canonical_text(cell, allow_empty=True) for cell in row)
            for row in value
        )

    @field_validator("caption")
    @classmethod
    def _canonical_caption(cls, value: str | None) -> str | None:
        return None if value is None else normalize_canonical_text(value)

    @field_validator("sheet_name")
    @classmethod
    def _canonical_sheet_name(cls, value: str | None) -> str | None:
        return None if value is None else normalize_single_line(value)

    @field_validator("source_anchor")
    @classmethod
    def _canonical_source_anchor(
        cls, value: SourceAnchor | None
    ) -> SourceAnchor | None:
        return normalize_source_anchor(value)

    @model_validator(mode="after")
    def _grid_range_and_source_consistency(self) -> Self:
        if not self.headers:
            raise ValueError("table slice headers must be nonempty")
        if not self.rows:
            raise ValueError("table slice rows must be nonempty")
        width = len(self.headers)
        if any(len(row) != width for row in self.rows):
            raise ValueError("table slice rows must form a rectangular grid")
        if self.row_end - self.row_start != len(self.rows):
            raise ValueError("table slice row range must match its rows")
        if self.mode is TableSliceMode.FULL and self.row_start != 0:
            raise ValueError("FULL table slice must start at row zero")
        anchor_sheet = (
            self.source_anchor.sheet_name if self.source_anchor is not None else None
        )
        if (
            self.sheet_name is not None
            and anchor_sheet is not None
            and self.sheet_name != anchor_sheet
        ):
            raise ValueError(
                "table slice sheet_name must match source_anchor sheet_name"
            )
        return self


def render_table_slice(table_slice: TableSlice) -> str:
    """Render one validated table slice with the canonical table renderer."""
    validated = TableSlice.model_validate(table_slice)
    return render_table_data(
        TableData(
            headers=validated.headers,
            rows=validated.rows,
            caption=validated.caption,
            sheet_name=validated.sheet_name,
            source_anchor=validated.source_anchor,
        )
    )


ChunkProvenance: TypeAlias = (
    TextParentProvenance | TextChildProvenance | TableProvenance
)


def _normalize_chunk_content(value: str) -> str:
    canonical_content = normalize_canonical_text(value, allow_empty=True)
    if not canonical_content:
        raise ValueError("chunk raw_content must be nonempty")
    return canonical_content


def derive_chunk_content_hash(raw_content: str) -> str:
    """Hash canonical chunk content with the shared canonical JSON primitive."""
    canonical_content = _normalize_chunk_content(raw_content)
    return canonical_sha256(canonical_content)


def derive_chunk_id(
    *,
    document_id: str,
    revision_id: str,
    chunk_type: ChunkType,
    chunking_strategy: ChunkingStrategy,
    provenance: ChunkProvenance,
    content_hash: str,
) -> str:
    """Derive a canonical chunk identity from content and typed lineage."""
    validate_document_id(document_id)
    validate_revision_id(revision_id)
    if not isinstance(chunk_type, ChunkType):
        raise TypeError("chunk_type must be a ChunkType")
    if not isinstance(chunking_strategy, ChunkingStrategy):
        raise TypeError("chunking_strategy must be a ChunkingStrategy")
    if isinstance(provenance, TextParentProvenance):
        validated_provenance: ChunkProvenance = TextParentProvenance.model_validate(
            provenance
        )
    elif isinstance(provenance, TextChildProvenance):
        validated_provenance = TextChildProvenance.model_validate(provenance)
    elif isinstance(provenance, TableProvenance):
        validated_provenance = TableProvenance.model_validate(provenance)
    else:
        raise TypeError("provenance must be typed canonical chunk provenance")
    validate_sha256_hex(content_hash, "chunk content hash")
    return "chunk_" + canonical_sha256(
        {
            "document_id": document_id,
            "revision_id": revision_id,
            "chunk_type": chunk_type.value,
            "chunking_strategy": chunking_strategy.value,
            "provenance": validated_provenance.model_dump(mode="json"),
            "content_hash": content_hash,
        }
    )


class ChunkRecord(FrozenModel):
    """One deterministic canonical Parent, Child, or Table chunk."""

    chunk_id: StrictStr
    document_id: StrictStr
    revision_id: StrictStr
    chunk_type: _ChunkType
    retrieval_role: _RetrievalRole
    chunking_strategy: _ChunkingStrategy
    raw_content: StrictStr
    content_hash: StrictStr
    parent_id: StrictStr | None = None
    order: Annotated[StrictInt, Field(ge=0)]
    section_path: _SectionPath = ()
    provenance: ChunkProvenance
    table_slice: TableSlice | None = None

    _canonical_chunk_id = field_validator("chunk_id")(_validate_chunk_id)
    _canonical_document_id = field_validator("document_id")(validate_document_id)
    _canonical_revision_id = field_validator("revision_id")(validate_revision_id)

    @field_validator("raw_content")
    @classmethod
    def _canonical_raw_content(cls, value: str) -> str:
        return _normalize_chunk_content(value)

    @field_validator("content_hash")
    @classmethod
    def _canonical_content_hash(cls, value: str) -> str:
        return validate_sha256_hex(value, "chunk content hash")

    @field_validator("parent_id")
    @classmethod
    def _canonical_parent_id(cls, value: str | None) -> str | None:
        return None if value is None else _validate_chunk_id(value)

    @field_validator("section_path")
    @classmethod
    def _canonical_section_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return normalize_section_path(value)

    @model_validator(mode="after")
    def _type_role_strategy_lineage_and_identity(self) -> Self:
        if self.chunk_type is ChunkType.TEXT_PARENT:
            valid_shape = (
                self.retrieval_role is RetrievalRole.CONTEXT_ONLY
                and self.chunking_strategy is ChunkingStrategy.STRUCTURE
                and self.parent_id is None
                and isinstance(self.provenance, TextParentProvenance)
                and self.table_slice is None
            )
            if not valid_shape:
                raise ValueError(
                    "TEXT_PARENT requires CONTEXT_ONLY STRUCTURE Parent provenance, "
                    "no parent_id, and no table_slice"
                )
            assert isinstance(self.provenance, TextParentProvenance)
            if sum(
                span.char_end - span.char_start for span in self.provenance.source_spans
            ) > len(self.raw_content):
                raise ValueError("TEXT_PARENT source_spans exceed rendered raw_content")
        elif self.chunk_type is ChunkType.TEXT_CHILD:
            valid_shape = (
                self.retrieval_role is RetrievalRole.RETRIEVABLE
                and self.chunking_strategy is ChunkingStrategy.RECURSIVE
                and self.parent_id is not None
                and isinstance(self.provenance, TextChildProvenance)
                and self.table_slice is None
            )
            if not valid_shape:
                raise ValueError(
                    "TEXT_CHILD requires RETRIEVABLE RECURSIVE Child provenance, "
                    "parent_id, and no table_slice"
                )
            assert isinstance(self.provenance, TextChildProvenance)
            if (
                self.provenance.parent_char_end - self.provenance.parent_char_start
                != len(self.raw_content)
            ):
                raise ValueError(
                    "TEXT_CHILD parent character range must match raw_content"
                )
            if sum(
                span.char_end - span.char_start for span in self.provenance.source_spans
            ) > len(self.raw_content):
                raise ValueError("TEXT_CHILD source_spans exceed rendered raw_content")
            if self.parent_id == self.chunk_id:
                raise ValueError("a chunk cannot be its own parent")
        else:
            valid_shape = (
                self.retrieval_role is RetrievalRole.RETRIEVABLE
                and self.chunking_strategy is ChunkingStrategy.TABLE
                and self.parent_id is None
                and isinstance(self.provenance, TableProvenance)
                and self.table_slice is not None
            )
            if not valid_shape:
                raise ValueError(
                    "TABLE requires RETRIEVABLE TABLE provenance, no parent_id, "
                    "and a table_slice"
                )
            assert isinstance(self.provenance, TableProvenance)
            assert self.table_slice is not None
            slice_lineage = (
                self.table_slice.source_element_id,
                self.table_slice.row_start,
                self.table_slice.row_end,
                self.table_slice.sheet_name,
            )
            provenance_lineage = (
                self.provenance.source_element_id,
                self.provenance.row_start,
                self.provenance.row_end,
                self.provenance.sheet_name,
            )
            if slice_lineage != provenance_lineage:
                raise ValueError("TABLE provenance and table_slice lineage must match")
            if self.raw_content != render_table_slice(self.table_slice):
                raise ValueError("TABLE raw_content must match canonical table_slice")

        expected_content_hash = derive_chunk_content_hash(self.raw_content)
        if self.content_hash != expected_content_hash:
            raise ValueError("content_hash does not match canonical raw_content")
        expected_chunk_id = derive_chunk_id(
            document_id=self.document_id,
            revision_id=self.revision_id,
            chunk_type=self.chunk_type,
            chunking_strategy=self.chunking_strategy,
            provenance=self.provenance,
            content_hash=self.content_hash,
        )
        if self.chunk_id != expected_chunk_id:
            raise ValueError("chunk_id does not match canonical chunk data")
        return self


def build_canonical_chunk(
    *,
    document_id: str,
    revision_id: str,
    chunk_type: ChunkType,
    retrieval_role: RetrievalRole,
    chunking_strategy: ChunkingStrategy,
    raw_content: str,
    parent_id: str | None,
    order: int,
    section_path: tuple[str, ...],
    provenance: ChunkProvenance,
    table_slice: TableSlice | None = None,
) -> ChunkRecord:
    """Normalize content, derive its hashes, and build one canonical chunk."""
    canonical_content = _normalize_chunk_content(raw_content)
    canonical_section_path = normalize_section_path(section_path)
    content_hash = derive_chunk_content_hash(canonical_content)
    canonical_chunk_id = derive_chunk_id(
        document_id=document_id,
        revision_id=revision_id,
        chunk_type=chunk_type,
        chunking_strategy=chunking_strategy,
        provenance=provenance,
        content_hash=content_hash,
    )
    return ChunkRecord(
        chunk_id=canonical_chunk_id,
        document_id=document_id,
        revision_id=revision_id,
        chunk_type=chunk_type,
        retrieval_role=retrieval_role,
        chunking_strategy=chunking_strategy,
        raw_content=canonical_content,
        content_hash=content_hash,
        parent_id=parent_id,
        order=order,
        section_path=canonical_section_path,
        provenance=provenance,
        table_slice=table_slice,
    )
