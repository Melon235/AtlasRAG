"""Fail-closed MinerU 4 Middle JSON to AtlasRAG canonical normalization."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, NoReturn

from pydantic import ValidationError

from atlasrag.domain.enums import SourceType
from atlasrag.domain.errors import ProviderResponseError
from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.ingestion.document import DocumentContainer, DocumentMetadata
from atlasrag.domain.ingestion.elements import (
    CanonicalElement,
    CanonicalElementMetadata,
    TableData,
    build_canonical_element,
    normalize_canonical_text,
    render_table_data,
)
from atlasrag.domain.ingestion.enums import ElementType, ListKind
from atlasrag.domain.ingestion.source import CandidateBuildContext, SourceFile
from atlasrag.providers.parsing.base import successful_document, validate_parse_request

_SUPPORTED_MINERU_VERSION = "4.0.11"
_SUPPORTED_SCHEMA = "docvortex.middle"
_SUPPORTED_SCHEMA_VERSION = "2.0"
# These are provider-response safety caps, not canonical truncation limits.
# Crossing any cap rejects the response before building a logical grid.
_MAX_TABLE_HTML_CHARACTERS = 1_048_576
_MAX_TABLE_ROWS = 4_096
_MAX_TABLE_COLUMNS = 1_024
_MAX_TABLE_LOGICAL_CELLS = 262_144
_MAX_TABLE_PHYSICAL_CELLS = 262_144
_ORDERED_ITEM = re.compile(r"^\s*(?:\d+|[A-Za-z]|[ivxlcdmIVXLCDM]+)[.)]\s+\S")
_INLINE_STYLES = frozenset(
    {
        "bold",
        "italic",
        "underline",
        "emphasis",
        "strikethrough",
        "superscript",
        "subscript",
    }
)


class _InvalidMinerUOutput(ValueError):
    """Internal sentinel whose detail never crosses the provider boundary."""


def _invalid() -> NoReturn:
    raise _InvalidMinerUOutput


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        _invalid()
    return value


def _items(value: object) -> list[Any]:
    if not isinstance(value, list):
        _invalid()
    return value


def _required(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping:
        _invalid()
    return mapping[key]


def _string(value: object, *, allow_blank: bool = False) -> str:
    if not isinstance(value, str) or (not allow_blank and not value.strip()):
        _invalid()
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _string(value)


def _integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _invalid()
    return value


def _optional_continuation(mapping: dict[str, Any]) -> None:
    value = _required(mapping, "continues_prev")
    if value is not None and value is not False:
        _invalid()


def _required_none(mapping: dict[str, Any], key: str) -> None:
    if _required(mapping, key) is not None:
        _invalid()


def _canonical_text(value: str, *, allow_empty: bool = False) -> str:
    try:
        return normalize_canonical_text(value, allow_empty=allow_empty)
    except ValueError:
        _invalid()


def _bbox(value: object) -> None:
    coordinates = _items(value)
    if len(coordinates) != 4:
        _invalid()
    if any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(float(item))
        for item in coordinates
    ):
        _invalid()
    x0, y0, x1, y1 = (float(item) for item in coordinates)
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        _invalid()


def _block_identity(block: dict[str, Any]) -> tuple[str, int]:
    block_type = _string(_required(block, "type"))
    index = _integer(_required(block, "index"))
    _bbox(_required(block, "bbox"))
    if block.get("anchor") is not None:
        _invalid()
    continuation = block.get("continues_prev")
    if continuation is not None and continuation is not False:
        _invalid()
    return block_type, index


def _inline_text(value: object) -> str:
    spans = _items(value)
    if not spans:
        _invalid()
    output: list[str] = []
    for raw_span in spans:
        span = _mapping(raw_span)
        if _required(span, "type") != "text":
            _invalid()
        content = _string(_required(span, "content"))
        styles = _items(_required(span, "styles"))
        if any(
            not isinstance(style, str) or style not in _INLINE_STYLES
            for style in styles
        ):
            _invalid()
        if len(styles) != len(set(styles)):
            _invalid()
        output.append(content)
    return _canonical_text("".join(output))


@dataclass(frozen=True, slots=True)
class _RawTableCell:
    content: str
    is_header: bool
    rowspan: int
    colspan: int


@dataclass(frozen=True, slots=True)
class _RawTableRow:
    cells: tuple[_RawTableCell, ...]
    header_section: bool


@dataclass(slots=True)
class _CellBuilder:
    tag: str
    rowspan: int
    colspan: int
    parts: list[str] = field(default_factory=list)


class _MinerUTableHTMLParser(HTMLParser):
    """Parse the structural HTML subset emitted by MinerU table bodies."""

    _INLINE_TAGS = frozenset(
        {"p", "span", "strong", "b", "em", "i", "code", "sup", "sub"}
    )
    _SECTIONS = frozenset({"thead", "tbody", "tfoot"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[_RawTableRow] = []
        self.caption_parts: list[str] | None = None
        self._seen_table = False
        self._inside_table = False
        self._section: str | None = None
        self._row_cells: list[_RawTableCell] | None = None
        self._row_header_section = False
        self._cell: _CellBuilder | None = None
        self._active_caption: list[str] | None = None
        self._open_tags: list[str] = []
        self._physical_cell_count = 0

    @staticmethod
    def _span_value(attrs: list[tuple[str, str | None]], name: str) -> int:
        values = [value for key, value in attrs if key == name]
        if len(values) > 1:
            _invalid()
        if not values:
            return 1
        value = values[0]
        if value is None or not value.isascii() or not value.isdigit():
            _invalid()
        maximum = _MAX_TABLE_ROWS if name == "rowspan" else _MAX_TABLE_COLUMNS
        if len(value) > len(str(maximum)):
            _invalid()
        span = _integer(int(value), minimum=1)
        if span > maximum:
            _invalid()
        return span

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "br":
            if attrs or (self._cell is None and self._active_caption is None):
                _invalid()
            self._append_text("\n")
            return
        if tag == "table":
            if self._seen_table or self._inside_table or attrs:
                _invalid()
            self._seen_table = self._inside_table = True
        elif tag in self._SECTIONS:
            if (
                not self._inside_table
                or self._section is not None
                or self._row_cells is not None
                or attrs
            ):
                _invalid()
            self._section = tag
        elif tag == "tr":
            if not self._inside_table or self._row_cells is not None or attrs:
                _invalid()
            self._row_cells = []
            self._row_header_section = self._section == "thead"
        elif tag in {"td", "th"}:
            if self._row_cells is None or self._cell is not None:
                _invalid()
            if any(key not in {"rowspan", "colspan"} for key, _value in attrs):
                _invalid()
            self._physical_cell_count += 1
            if self._physical_cell_count > _MAX_TABLE_PHYSICAL_CELLS:
                _invalid()
            self._cell = _CellBuilder(
                tag=tag,
                rowspan=self._span_value(attrs, "rowspan"),
                colspan=self._span_value(attrs, "colspan"),
            )
        elif tag == "caption":
            if (
                not self._inside_table
                or self._active_caption is not None
                or self._row_cells is not None
                or attrs
            ):
                _invalid()
            self._active_caption = []
        elif tag in self._INLINE_TAGS:
            if attrs or (self._cell is None and self._active_caption is None):
                _invalid()
        else:
            _invalid()
        self._open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "br":
            _invalid()
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if not self._open_tags or self._open_tags[-1] != tag:
            _invalid()
        self._open_tags.pop()
        if tag in self._INLINE_TAGS:
            if tag == "p":
                self._append_text("\n")
            return
        if tag in {"td", "th"}:
            if self._cell is None or self._row_cells is None:
                _invalid()
            text = "".join(self._cell.parts).strip()
            self._row_cells.append(
                _RawTableCell(
                    content=_canonical_text(text, allow_empty=True),
                    is_header=tag == "th" or self._row_header_section,
                    rowspan=self._cell.rowspan,
                    colspan=self._cell.colspan,
                )
            )
            self._cell = None
        elif tag == "tr":
            if self._row_cells is None or self._cell is not None:
                _invalid()
            if len(self.rows) >= _MAX_TABLE_ROWS:
                _invalid()
            self.rows.append(
                _RawTableRow(
                    cells=tuple(self._row_cells),
                    header_section=self._row_header_section,
                )
            )
            self._row_cells = None
            self._row_header_section = False
        elif tag in self._SECTIONS:
            if self._section != tag or self._row_cells is not None:
                _invalid()
            self._section = None
        elif tag == "caption":
            if self._active_caption is None:
                _invalid()
            if self.caption_parts is not None:
                _invalid()
            self.caption_parts = self._active_caption
            self._active_caption = None
        elif tag == "table":
            if (
                not self._inside_table
                or self._row_cells is not None
                or self._section is not None
                or self._active_caption is not None
            ):
                _invalid()
            self._inside_table = False
        else:
            _invalid()

    def handle_data(self, data: str) -> None:
        if self._cell is not None or self._active_caption is not None:
            self._append_text(data)
        elif data.strip():
            _invalid()

    def _append_text(self, value: str) -> None:
        if self._cell is not None:
            self._cell.parts.append(value)
        elif self._active_caption is not None:
            self._active_caption.append(value)
        else:
            _invalid()

    def handle_comment(self, data: str) -> None:
        del data
        _invalid()

    def handle_decl(self, decl: str) -> None:
        del decl
        _invalid()

    def handle_pi(self, data: str) -> None:
        del data
        _invalid()

    def finish(self) -> tuple[tuple[_RawTableRow, ...], str | None]:
        self.close()
        if (
            not self._seen_table
            or self._inside_table
            or self._open_tags
            or self._cell is not None
            or self._row_cells is not None
            or not self.rows
        ):
            _invalid()
        caption = None
        if self.caption_parts is not None:
            caption = _canonical_text(
                "".join(self.caption_parts).strip(), allow_empty=True
            )
            if not caption:
                caption = None
        return tuple(self.rows), caption


def _materialize_table(html: str, anchor: SourceAnchor) -> TableData:
    if len(html) > _MAX_TABLE_HTML_CHARACTERS:
        _invalid()
    parser = _MinerUTableHTMLParser()
    parser.feed(html)
    raw_rows, caption = parser.finish()
    occupied_until: dict[int, int] = {}
    materialized: list[tuple[str, ...]] = []
    header_rows: list[int] = []
    width = 0

    for row_index, raw_row in enumerate(raw_rows):
        if raw_row.header_section or (
            raw_row.cells and all(cell.is_header for cell in raw_row.cells)
        ):
            header_rows.append(row_index)
        elif any(cell.is_header for cell in raw_row.cells):
            _invalid()

        values: dict[int, str] = {
            column: ""
            for column, final_row in occupied_until.items()
            if final_row >= row_index
        }
        if not raw_row.cells and (width == 0 or set(values) != set(range(width))):
            _invalid()
        column = 0
        for cell in raw_row.cells:
            while True:
                if column + cell.colspan > _MAX_TABLE_COLUMNS:
                    _invalid()
                if not any(
                    occupied_until.get(candidate, -1) >= row_index
                    for candidate in range(column, column + cell.colspan)
                ):
                    break
                values.setdefault(column, "")
                column += 1
            values[column] = cell.content
            for offset in range(1, cell.colspan):
                values[column + offset] = ""
            if cell.rowspan > 1:
                for offset in range(cell.colspan):
                    occupied_until[column + offset] = row_index + cell.rowspan - 1
            column += cell.colspan
        if not values:
            _invalid()
        candidate_width = max(width, max(values) + 1)
        if (
            candidate_width > _MAX_TABLE_COLUMNS
            or candidate_width * len(raw_rows) > _MAX_TABLE_LOGICAL_CELLS
        ):
            _invalid()
        width = candidate_width
        materialized.append(tuple(values.get(index, "") for index in range(width)))

    if any(final_row >= len(raw_rows) for final_row in occupied_until.values()):
        _invalid()
    materialized = [row + ("",) * (width - len(row)) for row in materialized]
    if header_rows:
        if header_rows != [0]:
            _invalid()
        headers = materialized[0]
        rows = materialized[1:]
    else:
        headers = ("",) * width
        rows = materialized
    if not rows:
        _invalid()
    return TableData(
        headers=headers,
        rows=tuple(rows),
        caption=caption,
        source_anchor=anchor,
    )


@dataclass(slots=True)
class _MinerUNormalizer:
    document_id: str
    revision_id: str
    elements: list[CanonicalElement] = field(default_factory=list)
    headings: list[tuple[int, str]] = field(default_factory=list)
    document_title: str | None = None

    def _section_path(self) -> tuple[str, ...]:
        return tuple(text for _level, text in self.headings)

    def _anchor(self, page_number: int) -> SourceAnchor:
        return SourceAnchor(
            page_number=page_number,
            heading=self.headings[-1][1] if self.headings else None,
        )

    def _append(
        self,
        element_type: ElementType,
        content: str,
        *,
        page_number: int,
        metadata: CanonicalElementMetadata | None = None,
        table_data: TableData | None = None,
    ) -> None:
        self.elements.append(
            build_canonical_element(
                document_id=self.document_id,
                revision_id=self.revision_id,
                element_type=element_type,
                order=len(self.elements),
                content=content,
                section_path=self._section_path(),
                source_anchor=self._anchor(page_number),
                metadata=metadata,
                table_data=table_data,
            )
        )

    def normalize_block(self, raw_block: object, page_number: int) -> int:
        block = _mapping(raw_block)
        block_type, index = _block_identity(block)
        if block_type in {"doc_title", "paragraph_title"}:
            self._heading(block, block_type, page_number)
        elif block_type in {"text", "ref_text"}:
            _required_none(block, "anchor")
            _optional_continuation(block)
            self._append(
                ElementType.PARAGRAPH,
                _inline_text(_required(block, "content")),
                page_number=page_number,
            )
        elif block_type == "list":
            return self._list(block, page_number, depth=0, index=index)
        elif block_type == "code":
            self._code(block, index, page_number)
        elif block_type == "table":
            _optional_continuation(block)
            self._table(block, index, page_number)
        else:
            _invalid()
        return index

    def _heading(
        self, block: dict[str, Any], block_type: str, page_number: int
    ) -> None:
        _required_none(block, "anchor")
        level = _integer(_required(block, "level"), minimum=1)
        if (block_type == "doc_title" and level != 1) or (
            block_type == "paragraph_title" and not 2 <= level <= 6
        ):
            _invalid()
        content = _inline_text(_required(block, "content"))
        while self.headings and self.headings[-1][0] >= level:
            self.headings.pop()
        self.headings.append((level, content))
        if block_type == "doc_title" and self.document_title is None:
            self.document_title = content
        self._append(
            ElementType.HEADING,
            content,
            page_number=page_number,
            metadata=CanonicalElementMetadata(heading_level=level),
        )

    def _list(
        self,
        block: dict[str, Any],
        page_number: int,
        *,
        depth: int,
        index: int,
    ) -> int:
        _optional_continuation(block)
        if block.get("sub_type") not in {"text", "ref_text"}:
            _invalid()
        children = _items(_required(block, "content"))
        if not children:
            _invalid()
        direct_texts: list[str] = []
        prepared: list[tuple[dict[str, Any], str, int, str | None]] = []
        for raw_child in children:
            child = _mapping(raw_child)
            child_type, child_index = _block_identity(child)
            content: str | None = None
            if child_type in {"text", "ref_text"}:
                _required_none(child, "anchor")
                _optional_continuation(child)
                content = _inline_text(_required(child, "content"))
                direct_texts.append(content)
            elif child_type != "list":
                _invalid()
            prepared.append((child, child_type, child_index, content))
        if not direct_texts:
            _invalid()
        kind = (
            ListKind.ORDERED
            if all(_ORDERED_ITEM.match(text) for text in direct_texts)
            else ListKind.UNORDERED
        )
        previous_index = index
        for child, child_type, child_index, content in prepared:
            if child_index <= previous_index:
                _invalid()
            if child_type == "list":
                previous_index = self._list(
                    child,
                    page_number,
                    depth=depth + 1,
                    index=child_index,
                )
            else:
                if content is None:
                    _invalid()
                self._append(
                    ElementType.LIST,
                    content,
                    page_number=page_number,
                    metadata=CanonicalElementMetadata(
                        list_kind=kind,
                        list_depth=depth,
                    ),
                )
                previous_index = child_index
        return previous_index

    def _code(self, block: dict[str, Any], index: int, page_number: int) -> None:
        if block.get("sub_type") != "code":
            _invalid()
        language = _string(_required(block, "guess_lang"))
        children = _items(_required(block, "content"))
        if len(children) != 1:
            _invalid()
        body = _mapping(children[0])
        body_type, body_index = _block_identity(body)
        if body_type != "code_body" or body_index != index:
            _invalid()
        self._append(
            ElementType.CODE,
            _canonical_text(_string(_required(body, "content"))),
            page_number=page_number,
            metadata=CanonicalElementMetadata(code_language=language),
        )

    def _table(self, block: dict[str, Any], index: int, page_number: int) -> None:
        _required_none(block, "cell_merge")
        children = _items(_required(block, "content"))
        if len(children) != 1:
            _invalid()
        body = _mapping(children[0])
        body_type, body_index = _block_identity(body)
        if body_type != "table_body" or body_index != index:
            _invalid()
        _required_none(body, "image_path")
        _required_none(body, "image_url")
        anchor = self._anchor(page_number)
        table = _materialize_table(_string(_required(body, "content")), anchor)
        self._append(
            ElementType.TABLE,
            render_table_data(table),
            page_number=page_number,
            table_data=table,
        )


def _metadata(payload: dict[str, Any], page_count: int) -> tuple[DocumentMetadata, str]:
    metadata = _mapping(_required(payload, "metadata"))
    if _required(metadata, "file_suffix") != "pdf":
        _invalid()
    producer = _mapping(_required(metadata, "producer"))
    if _required(producer, "name") != "mineru":
        _invalid()
    version = _string(_required(producer, "version"))
    if version != _SUPPORTED_MINERU_VERSION:
        _invalid()
    document = _mapping(_required(metadata, "document"))
    declared_page_count = _integer(_required(document, "page_count"), minimum=1)
    if (
        declared_page_count != page_count
        or document.get("page_count_kind") != "physical"
    ):
        _invalid()
    authors = tuple(
        _string(author) for author in _items(_required(document, "authors"))
    )
    languages = tuple(
        _string(language) for language in _items(_required(document, "languages"))
    )
    if len(languages) > 1:
        _invalid()
    title = _optional_string(_required(document, "title"))
    return (
        DocumentMetadata(
            title=title,
            authors=authors,
            language=languages[0] if languages else None,
            page_count=declared_page_count,
        ),
        version,
    )


def _normalize_payload(
    payload: object,
    context: CandidateBuildContext,
) -> tuple[tuple[CanonicalElement, ...], DocumentMetadata, str]:
    root = _mapping(payload)
    if (
        _required(root, "schema") != _SUPPORTED_SCHEMA
        or _required(root, "schema_version") != _SUPPORTED_SCHEMA_VERSION
        or _required(root, "is_full_document") is not True
    ):
        _invalid()
    extensions = _mapping(_required(root, "extensions"))
    mineru = _mapping(_required(extensions, "mineru"))
    if mineru.get("tier") != "flash" or mineru.get("parse_mode") not in {
        "txt",
        "ocr",
    }:
        _invalid()

    pages = _items(_required(root, "pages"))
    if not pages:
        _invalid()
    metadata, parser_version = _metadata(root, len(pages))
    normalizer = _MinerUNormalizer(
        document_id=context.document_id,
        revision_id=context.revision_id,
    )
    for page_position, raw_page in enumerate(pages):
        page = _mapping(raw_page)
        if _integer(_required(page, "page_idx")) != page_position:
            _invalid()
        previous_index = -1
        for raw_block in _items(_required(page, "blocks")):
            current_index = normalizer.normalize_block(
                raw_block, page_number=page_position + 1
            )
            if current_index <= previous_index:
                _invalid()
            previous_index = current_index

    if not normalizer.elements:
        _invalid()
    if metadata.title is None and normalizer.document_title is not None:
        metadata = metadata.model_copy(update={"title": normalizer.document_title})
    return tuple(normalizer.elements), metadata, parser_version


def normalize_mineru_output(
    payload: object,
    source: SourceFile,
    context: CandidateBuildContext,
) -> DocumentContainer:
    """Validate one MinerU 4 result and discard every provider-native detail."""
    source, context = validate_parse_request(
        source,
        context,
        allowed_source_types=(SourceType.PDF,),
    )
    try:
        elements, metadata, parser_version = _normalize_payload(payload, context)
    except (_InvalidMinerUOutput, ValidationError):
        raise ProviderResponseError from None
    return successful_document(
        source,
        context,
        parser_name="mineru",
        parser_version=parser_version,
        elements=elements,
        metadata=metadata,
    )
