"""Native deterministic parser for the frozen AtlasRAG Markdown subset."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from atlasrag.domain.enums import SourceType
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
from atlasrag.providers.parsing.base import (
    failed_document,
    read_source_bytes,
    successful_document,
    validate_parse_request,
)

_PARSER_NAME = "atlasrag-markdown"
_PARSER_VERSION = "1"


class _MarkdownFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _Fence:
    character: str
    length: int
    language: str | None


@dataclass(frozen=True, slots=True)
class _ListItem:
    kind: ListKind
    depth: int
    content: str


def _heading(line: str) -> tuple[int, str] | None:
    indent = len(line) - len(line.lstrip(" "))
    if indent > 3:
        return None
    candidate = line[indent:]
    level = len(candidate) - len(candidate.lstrip("#"))
    if not 1 <= level <= 6:
        return None
    if len(candidate) > level and not candidate[level].isspace():
        return None
    content = candidate[level:].strip()
    return (level, content) if content else None


def _opening_fence(line: str) -> _Fence | None:
    indent = len(line) - len(line.lstrip(" "))
    if indent > 3:
        return None
    candidate = line[indent:]
    if not candidate or candidate[0] not in {"`", "~"}:
        return None
    character = candidate[0]
    length = len(candidate) - len(candidate.lstrip(character))
    if length < 3:
        return None
    info = candidate[length:].strip()
    language = info.split(maxsplit=1)[0] if info else None
    return _Fence(character=character, length=length, language=language)


def _closes_fence(line: str, fence: _Fence) -> bool:
    candidate = line.lstrip(" ")
    if len(line) - len(candidate) > 3 or not candidate.startswith(fence.character):
        return False
    length = len(candidate) - len(candidate.lstrip(fence.character))
    return length >= fence.length and not candidate[length:].strip()


def _list_item(line: str) -> _ListItem | None:
    indent = len(line) - len(line.lstrip(" "))
    candidate = line[indent:]
    kind: ListKind | None = None
    marker_end = 0
    if len(candidate) >= 2 and candidate[0] in {"-", "+", "*"}:
        if candidate[1].isspace():
            kind = ListKind.UNORDERED
            marker_end = 2
    else:
        digit_count = len(candidate) - len(candidate.lstrip("0123456789"))
        if (
            digit_count
            and len(candidate) > digit_count + 1
            and candidate[digit_count] in {".", ")"}
            and candidate[digit_count + 1].isspace()
        ):
            kind = ListKind.ORDERED
            marker_end = digit_count + 2
    if kind is None:
        return None
    content = candidate[marker_end:].strip()
    if not content:
        return None
    return _ListItem(kind=kind, depth=indent // 2, content=content)


def _split_pipe_row(line: str) -> tuple[str, ...] | None:
    candidate = line.strip()
    if "|" not in candidate:
        return None
    if candidate.startswith("|"):
        candidate = candidate[1:]
    if candidate.endswith("|") and not candidate.endswith("\\|"):
        candidate = candidate[:-1]

    cells: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(candidate):
        character = candidate[index]
        if character == "\\" and index + 1 < len(candidate):
            following = candidate[index + 1]
            if following in {"|", "\\"}:
                current.append(following)
                index += 2
                continue
        if character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
        index += 1
    cells.append("".join(current).strip())
    return tuple(cells)


def _is_delimiter_row(cells: tuple[str, ...]) -> bool:
    if not cells:
        return False
    for cell in cells:
        candidate = cell.strip()
        if candidate.startswith(":"):
            candidate = candidate[1:]
        if candidate.endswith(":"):
            candidate = candidate[:-1]
        if len(candidate) < 3 or set(candidate) != {"-"}:
            return False
    return True


def _table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    headers = _split_pipe_row(lines[index])
    delimiter = _split_pipe_row(lines[index + 1])
    return (
        headers is not None
        and delimiter is not None
        and len(headers) == len(delimiter)
        and _is_delimiter_row(delimiter)
    )


def _anchor(section_path: tuple[str, ...]) -> SourceAnchor | None:
    return SourceAnchor(heading=section_path[-1]) if section_path else None


class MarkdownParser:
    """Parse a conservative Markdown subset without optional SDK imports."""

    async def parse(
        self,
        source: SourceFile,
        context: CandidateBuildContext,
    ) -> DocumentContainer:
        source, context = validate_parse_request(
            source,
            context,
            allowed_source_types=(SourceType.MD,),
        )
        payload = await read_source_bytes(source)
        try:
            decoded = payload.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError:
            return self._failed(source, context, "INVALID_UTF8")
        if not decoded.strip():
            return self._failed(source, context, "EMPTY_SOURCE")
        try:
            normalized = normalize_canonical_text(decoded)
        except ValueError:
            return self._failed(source, context, "INVALID_TEXT_CONTROL")

        try:
            elements, title = self._parse_lines(normalized, context)
        except _MarkdownFailure as failure:
            return self._failed(source, context, failure.code)
        return successful_document(
            source,
            context,
            parser_name=_PARSER_NAME,
            parser_version=_PARSER_VERSION,
            elements=elements,
            metadata=DocumentMetadata(title=title),
        )

    def _parse_lines(
        self,
        normalized: str,
        context: CandidateBuildContext,
    ) -> tuple[tuple[CanonicalElement, ...], str | None]:
        lines = normalized.split("\n")
        elements: list[CanonicalElement] = []
        headings: list[tuple[int, str]] = []
        title: str | None = None
        index = 0

        def section_path() -> tuple[str, ...]:
            return tuple(text for _, text in headings)

        def append(
            element_type: ElementType,
            content: str,
            *,
            metadata: CanonicalElementMetadata | None = None,
            source_anchor: SourceAnchor | None = None,
            table_data: TableData | None = None,
            explicit_section: tuple[str, ...] | None = None,
        ) -> None:
            elements.append(
                build_canonical_element(
                    document_id=context.document_id,
                    revision_id=context.revision_id,
                    element_type=element_type,
                    order=len(elements),
                    content=content,
                    section_path=(
                        section_path() if explicit_section is None else explicit_section
                    ),
                    source_anchor=source_anchor,
                    metadata=metadata,
                    table_data=table_data,
                )
            )

        while index < len(lines):
            line = lines[index]
            if not line.strip():
                index += 1
                continue

            parsed_heading = _heading(line)
            if parsed_heading is not None:
                level, content = parsed_heading
                while headings and headings[-1][0] >= level:
                    headings.pop()
                headings.append((level, content))
                current_section = section_path()
                if title is None and level == 1:
                    title = content
                append(
                    ElementType.HEADING,
                    content,
                    metadata=CanonicalElementMetadata(heading_level=level),
                    source_anchor=SourceAnchor(heading=content),
                    explicit_section=current_section,
                )
                index += 1
                continue

            fence = _opening_fence(line)
            if fence is not None:
                closing = index + 1
                while closing < len(lines) and not _closes_fence(lines[closing], fence):
                    closing += 1
                if closing >= len(lines):
                    raise _MarkdownFailure("UNCLOSED_CODE_FENCE")
                code_lines = lines[index + 1 : closing]
                if not code_lines or not "\n".join(code_lines).strip():
                    raise _MarkdownFailure("EMPTY_CODE_BLOCK")
                append(
                    ElementType.CODE,
                    "\n".join(code_lines) + "\n",
                    metadata=CanonicalElementMetadata(code_language=fence.language),
                    source_anchor=_anchor(section_path()),
                )
                index = closing + 1
                continue

            item = _list_item(line)
            if item is not None:
                append(
                    ElementType.LIST,
                    item.content,
                    metadata=CanonicalElementMetadata(
                        list_kind=item.kind,
                        list_depth=item.depth,
                    ),
                    source_anchor=_anchor(section_path()),
                )
                index += 1
                continue

            if _table_start(lines, index):
                headers = _split_pipe_row(lines[index])
                assert headers is not None
                row_index = index + 2
                rows: list[tuple[str, ...]] = []
                while row_index < len(lines) and lines[row_index].strip():
                    row = _split_pipe_row(lines[row_index])
                    if row is None:
                        break
                    rows.append(row)
                    row_index += 1
                table_anchor = _anchor(section_path())
                try:
                    table = TableData(
                        headers=headers,
                        rows=tuple(rows),
                        source_anchor=table_anchor,
                    )
                except ValidationError:
                    raise _MarkdownFailure("INVALID_TABLE") from None
                append(
                    ElementType.TABLE,
                    render_table_data(table),
                    source_anchor=table_anchor,
                    table_data=table,
                )
                index = row_index
                continue

            paragraph_lines = [line]
            index += 1
            while index < len(lines) and lines[index].strip():
                if (
                    _heading(lines[index]) is not None
                    or _opening_fence(lines[index]) is not None
                    or _list_item(lines[index]) is not None
                    or _table_start(lines, index)
                ):
                    break
                paragraph_lines.append(lines[index])
                index += 1
            append(
                ElementType.PARAGRAPH,
                "\n".join(paragraph_lines),
                source_anchor=_anchor(section_path()),
            )

        if not elements:
            raise _MarkdownFailure("EMPTY_SOURCE")
        return tuple(elements), title

    @staticmethod
    def _failed(
        source: SourceFile,
        context: CandidateBuildContext,
        failure_code: str,
    ) -> DocumentContainer:
        return failed_document(
            source,
            context,
            parser_name=_PARSER_NAME,
            parser_version=_PARSER_VERSION,
            failure_code=failure_code,
        )
