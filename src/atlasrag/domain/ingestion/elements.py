"""Immutable canonical element and full-table contracts."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Self

from pydantic import (
    BeforeValidator,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from atlasrag._canonical import canonical_sha256, validate_unicode_scalar_text
from atlasrag.domain.base import (
    FrozenModel,
    validate_ordered_collection_input,
    validate_string_enum_input,
)
from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.ingestion.enums import ElementType, ListKind
from atlasrag.domain.ingestion.source import (
    validate_document_id,
    validate_revision_id,
)

_ELEMENT_ID = re.compile(r"^elem_[0-9a-f]{64}$")
_ElementType = Annotated[ElementType, BeforeValidator(validate_string_enum_input)]
_ListKind = Annotated[ListKind, BeforeValidator(validate_string_enum_input)]
_SectionPath = Annotated[
    tuple[StrictStr, ...], BeforeValidator(validate_ordered_collection_input)
]


def _ordered_rows_input(value: object) -> object:
    value = validate_ordered_collection_input(value)
    assert isinstance(value, (list, tuple))
    for row in value:
        validate_ordered_collection_input(row)
    return value


_Headers = Annotated[
    tuple[StrictStr, ...], BeforeValidator(validate_ordered_collection_input)
]
_Rows = Annotated[
    tuple[tuple[StrictStr, ...], ...], BeforeValidator(_ordered_rows_input)
]


def normalize_canonical_text(value: str, *, allow_empty: bool = False) -> str:
    """Apply lossless canonical Unicode/newline normalization to parsed text."""
    value = validate_unicode_scalar_text(value)
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    )
    if any(
        (ord(character) < 32 and character not in {"\n", "\t"}) or ord(character) == 127
        for character in normalized
    ):
        raise ValueError("canonical text contains a forbidden control character")
    if not allow_empty and not normalized.strip():
        raise ValueError("canonical text must not be empty or blank")
    return normalized


def normalize_single_line(value: str) -> str:
    """Normalize one required nonblank single-line metadata value."""
    normalized = normalize_canonical_text(value)
    if "\n" in normalized or "\t" in normalized:
        raise ValueError("value must be a single line")
    return normalized


def normalize_source_anchor(value: SourceAnchor | None) -> SourceAnchor | None:
    """Rebuild a frozen Stage 1 anchor with Stage 3 canonical text rules."""
    if value is None:
        return None
    value = SourceAnchor.model_validate(value)
    return SourceAnchor(
        page_number=value.page_number,
        heading=(
            None if value.heading is None else normalize_single_line(value.heading)
        ),
        sheet_name=(
            None
            if value.sheet_name is None
            else normalize_single_line(value.sheet_name)
        ),
        cell_range=(
            None
            if value.cell_range is None
            else normalize_single_line(value.cell_range)
        ),
    )


def normalize_section_path(value: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize every ordered section component without changing order."""
    try:
        return tuple(normalize_single_line(part) for part in value)
    except ValueError as error:
        raise ValueError("section_path contains an invalid component") from error


class TableData(FrozenModel):
    """Normalized complete logical table before chunk slicing."""

    headers: _Headers
    rows: _Rows
    caption: StrictStr | None = None
    sheet_name: StrictStr | None = None
    source_anchor: SourceAnchor | None = None

    @field_validator("headers")
    @classmethod
    def _normalize_headers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(normalize_canonical_text(cell, allow_empty=True) for cell in value)

    @field_validator("rows")
    @classmethod
    def _normalize_rows(
        cls, value: tuple[tuple[str, ...], ...]
    ) -> tuple[tuple[str, ...], ...]:
        return tuple(
            tuple(normalize_canonical_text(cell, allow_empty=True) for cell in row)
            for row in value
        )

    @field_validator("caption")
    @classmethod
    def _normalize_caption(cls, value: str | None) -> str | None:
        return None if value is None else normalize_canonical_text(value)

    @field_validator("sheet_name")
    @classmethod
    def _normalize_sheet_name(cls, value: str | None) -> str | None:
        return None if value is None else normalize_single_line(value)

    @field_validator("source_anchor")
    @classmethod
    def _normalize_source_anchor(
        cls, value: SourceAnchor | None
    ) -> SourceAnchor | None:
        return normalize_source_anchor(value)

    @model_validator(mode="after")
    def _rectangular_grid_and_provenance(self) -> Self:
        if not self.headers:
            raise ValueError("table headers must define at least one logical column")
        if not self.rows:
            raise ValueError("table must contain at least one data row")
        width = len(self.headers)
        if any(len(row) != width for row in self.rows):
            raise ValueError("table rows must form a rectangular logical grid")
        anchor_sheet = (
            self.source_anchor.sheet_name if self.source_anchor is not None else None
        )
        if (
            self.sheet_name is not None
            and anchor_sheet is not None
            and self.sheet_name != anchor_sheet
        ):
            raise ValueError("table sheet_name must match source_anchor sheet_name")
        return self


class CanonicalElementMetadata(FrozenModel):
    """Stable AtlasRAG element facts, excluding provider-native payloads."""

    heading_level: Annotated[StrictInt, Field(ge=1, le=6)] | None = None
    list_kind: _ListKind | None = None
    list_depth: Annotated[StrictInt, Field(ge=0)] | None = None
    code_language: StrictStr | None = None

    @field_validator("code_language")
    @classmethod
    def _normalize_code_language(cls, value: str | None) -> str | None:
        return None if value is None else normalize_single_line(value)


def derive_element_id(
    *,
    revision_id: str,
    order: int,
    element_type: ElementType,
    content: str,
    section_path: tuple[str, ...],
    source_anchor: SourceAnchor | None,
    table_data: TableData | None,
) -> str:
    """Derive a deterministic canonical identity for one parsed element."""
    validate_revision_id(revision_id)
    if type(order) is not int or order < 0:
        raise ValueError("element order must be a nonnegative integer")
    if not isinstance(element_type, ElementType):
        raise TypeError("element_type must be an ElementType")
    normalized_content = normalize_canonical_text(content)
    normalized_section = normalize_section_path(section_path)
    validated_anchor = normalize_source_anchor(source_anchor)
    validated_table = (
        None if table_data is None else TableData.model_validate(table_data)
    )
    return "elem_" + canonical_sha256(
        {
            "revision_id": revision_id,
            "order": order,
            "element_type": element_type.value,
            "content": normalized_content,
            "section_path": normalized_section,
            "source_anchor": (
                None
                if validated_anchor is None
                else validated_anchor.model_dump(mode="json")
            ),
            "table_data": (
                None
                if validated_table is None
                else validated_table.model_dump(mode="json")
            ),
        }
    )


class CanonicalElement(FrozenModel):
    """One ordered, provider-neutral canonical document element."""

    element_id: StrictStr
    document_id: StrictStr
    revision_id: StrictStr
    element_type: _ElementType
    order: Annotated[StrictInt, Field(ge=0)]
    content: StrictStr
    section_path: _SectionPath = ()
    source_anchor: SourceAnchor | None = None
    metadata: CanonicalElementMetadata = Field(default_factory=CanonicalElementMetadata)
    table_data: TableData | None = None

    @field_validator("element_id")
    @classmethod
    def _canonical_element_id(cls, value: str) -> str:
        if _ELEMENT_ID.fullmatch(value) is None:
            raise ValueError("element_id must be a canonical element identity")
        return value

    _canonical_document_id = field_validator("document_id")(validate_document_id)
    _canonical_revision_id = field_validator("revision_id")(validate_revision_id)

    @field_validator("content")
    @classmethod
    def _normalize_content(cls, value: str) -> str:
        return normalize_canonical_text(value)

    @field_validator("section_path")
    @classmethod
    def _normalize_section_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return normalize_section_path(value)

    @field_validator("source_anchor")
    @classmethod
    def _normalize_source_anchor(
        cls, value: SourceAnchor | None
    ) -> SourceAnchor | None:
        return normalize_source_anchor(value)

    @model_validator(mode="after")
    def _type_metadata_table_and_identity_consistency(self) -> Self:
        metadata = self.metadata
        if self.element_type is ElementType.HEADING:
            metadata_valid = (
                metadata.heading_level is not None
                and metadata.list_kind is None
                and metadata.list_depth is None
                and metadata.code_language is None
            )
        elif self.element_type is ElementType.LIST:
            metadata_valid = (
                metadata.heading_level is None
                and metadata.list_kind is not None
                and metadata.list_depth is not None
                and metadata.code_language is None
            )
        elif self.element_type is ElementType.CODE:
            metadata_valid = (
                metadata.heading_level is None
                and metadata.list_kind is None
                and metadata.list_depth is None
            )
        else:
            metadata_valid = all(
                value is None
                for value in (
                    metadata.heading_level,
                    metadata.list_kind,
                    metadata.list_depth,
                    metadata.code_language,
                )
            )
        if not metadata_valid:
            raise ValueError("element metadata contradicts element_type")

        if self.element_type is ElementType.TABLE and self.table_data is None:
            raise ValueError("TABLE element requires table_data")
        if self.element_type is not ElementType.TABLE and self.table_data is not None:
            raise ValueError("non-TABLE element must not contain table_data")
        if (
            self.element_type is ElementType.TABLE
            and self.table_data is not None
            and self.source_anchor != self.table_data.source_anchor
        ):
            raise ValueError(
                "TABLE element source_anchor must match table_data source_anchor"
            )

        expected_id = derive_element_id(
            revision_id=self.revision_id,
            order=self.order,
            element_type=self.element_type,
            content=self.content,
            section_path=self.section_path,
            source_anchor=self.source_anchor,
            table_data=self.table_data,
        )
        if self.element_id != expected_id:
            raise ValueError("element_id does not match canonical element data")
        return self
