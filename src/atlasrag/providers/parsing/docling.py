# mypy: disable-error-code="import-not-found, import-untyped, unused-ignore"
"""Lazy Docling Slim adapter for canonical DOCX and XLSX parsing."""

from __future__ import annotations

import asyncio
import math
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, Protocol
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from pydantic import ValidationError

from atlasrag.domain.enums import SourceType
from atlasrag.domain.errors import (
    DependencyTimeoutError,
    DependencyUnavailableError,
    InvariantViolationError,
    ProviderResponseError,
)
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
from atlasrag.domain.ingestion.source import (
    CandidateBuildContext,
    SourceFile,
    resolve_source_target,
)
from atlasrag.providers.parsing.base import (
    failed_document,
    successful_document,
    validate_parse_request,
)

_SUPPORTED_DOCLING_VERSION = "2.135.0"
_DOCLING_CONTENT_LAYERS = frozenset(
    {"body", "furniture", "background", "invisible", "notes"}
)
_GROUP_LABELS = frozenset(
    {"unspecified", "list", "ordered_list", "chapter", "section", "sheet", "inline"}
)
_LIST_GROUP_LABELS = frozenset({"list", "ordered_list"})
_TEXT_LABELS = frozenset({"text", "paragraph"})
_FLATTENABLE_TABLE_GROUP_LABELS = frozenset({"unspecified", "inline"})
_MAX_TABLE_ROWS = 4_096
_MAX_TABLE_COLUMNS = 1_024
_MAX_TABLE_LOGICAL_CELLS = 262_144
_MAX_XLSX_WORKBOOK_LOGICAL_WORK = 2 * _MAX_TABLE_LOGICAL_CELLS
_MAX_OFFICE_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_OFFICE_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_OFFICE_EXPANDED_BYTES = 256 * 1024 * 1024
_MAX_OFFICE_ARCHIVE_MEMBERS = 10_000
_MAX_PROVIDER_ITEMS = 200_000
_MAX_CANONICAL_ELEMENTS = 100_000
_MAX_CANONICAL_TEXT_CHARACTERS = 64 * 1024 * 1024
_CONTENT_TYPES_MEMBER = "[Content_Types].xml"
_CONTENT_TYPES_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)
_RELATIONSHIPS_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
_OFFICE_RELATIONSHIPS_NAMESPACE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_WORKSHEET_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
)
_WORKBOOK_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
)
_UNSUPPORTED_WORKBOOK_CONTENT_TYPES = frozenset(
    {
        "application/vnd.ms-excel.template.macroEnabled.main+xml",
        "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
    }
)
_DRAWING_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.drawing+xml"
_CHART_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
)
_WORKSHEET_RELATIONSHIP_TYPE = f"{_OFFICE_RELATIONSHIPS_NAMESPACE}/worksheet"
_XLSX_CELL_REFERENCE = re.compile(r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]*)")
_XLSX_MAX_ROWS = 1_048_576
_XLSX_MAX_COLUMNS = 16_384


class _InvalidDoclingOutput(ValueError):
    """Internal sentinel whose provider detail never crosses the boundary."""


class _DoclingSourceFailure(Exception):
    """Known source-data failure reported by the pinned Docling backend."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _DoclingExecutionFailure(StrEnum):
    """Safe internal outcomes for provider exceptions raised in the worker."""

    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    INVARIANT_VIOLATION = "INVARIANT_VIOLATION"
    INVALID_RESPONSE = "INVALID_RESPONSE"


@dataclass(frozen=True, slots=True)
class _DoclingSourceFailureResult:
    """Carry only one stable business code out of the provider worker."""

    code: str


def _invalid() -> NoReturn:
    raise _InvalidDoclingOutput


def _attribute(value: object, name: str) -> object:
    try:
        return getattr(value, name)
    except AttributeError:
        _invalid()


def _string(value: object, *, allow_blank: bool = False) -> str:
    if not isinstance(value, str) or (not allow_blank and not value.strip()):
        _invalid()
    return value


def _canonical_text(value: object, *, allow_empty: bool = False) -> str:
    try:
        return normalize_canonical_text(
            _string(value, allow_blank=allow_empty),
            allow_empty=allow_empty,
        )
    except ValueError:
        _invalid()


def _integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _invalid()
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid()
    try:
        normalized = float(value)
    except OverflowError:
        _invalid()
    if not math.isfinite(normalized):
        _invalid()
    return normalized


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        _invalid()
    return value


def _sequence(value: object) -> list[Any] | tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        _invalid()
    return value


def _empty_sequence(value: object) -> None:
    if _sequence(value):
        _invalid()


def _label(value: object) -> str:
    if isinstance(value, str):
        return _string(value)
    return _string(_attribute(value, "value"))


def _reference(value: object) -> str | None:
    if value is None:
        return None
    return _string(_attribute(value, "cref"))


def _provider_text(item: object) -> str:
    text = _canonical_text(_attribute(item, "text"))
    original = _canonical_text(_attribute(item, "orig"))
    if text != original:
        _invalid()
    return text


class _ConvertFunction(Protocol):
    def __call__(self, path: Path, source_type: SourceType) -> object: ...


@dataclass(frozen=True, slots=True)
class _DoclingSDK:
    convert: _ConvertFunction
    version: str
    content_layers: frozenset[object]


@dataclass(frozen=True, slots=True)
class _PackageRelationship:
    relationship_type: str
    target: str
    target_mode: str | None


@dataclass(frozen=True, slots=True)
class _ContentTypeManifest:
    by_member: dict[str, str]
    defaults: dict[str, str]
    overrides: dict[str, str]


def _raise_input_rejection(
    cause: BaseException | None,
    *,
    document_load_error_type: type[BaseException],
    security_error_type: type[BaseException],
) -> NoReturn:
    if cause is None or isinstance(cause, document_load_error_type):
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
    if isinstance(cause, security_error_type):
        raise _DoclingSourceFailure("UNSAFE_OFFICE_SOURCE")
    if not isinstance(cause, Exception):
        raise cause
    raise ProviderResponseError


def _unsafe_archive_member(name: str) -> bool:
    path = PurePosixPath(name)
    raw_parts = name[:-1].split("/") if name.endswith("/") else name.split("/")
    return (
        not name
        or path.is_absolute()
        or "\\" in name
        or any(part in {"", ".", ".."} for part in raw_parts)
        or (bool(path.parts) and path.parts[0].endswith(":"))
    )


def _xml_root(payload: bytes) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE") from None


def _xlsx_cell_position(reference: str) -> tuple[int, int]:
    match = _XLSX_CELL_REFERENCE.fullmatch(reference)
    if match is None:
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
    letters, row_text = match.groups()
    column = 0
    for character in letters.upper():
        column = column * 26 + ord(character) - ord("A") + 1
    row = int(row_text)
    if row > _XLSX_MAX_ROWS or column > _XLSX_MAX_COLUMNS:
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
    return row, column


def _xlsx_range_bounds(reference: str) -> tuple[int, int, int, int]:
    parts = reference.split(":")
    if len(parts) == 1:
        start_row, start_column = _xlsx_cell_position(parts[0])
        return start_row, start_column, start_row, start_column
    if len(parts) != 2:
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
    start_row, start_column = _xlsx_cell_position(parts[0])
    end_row, end_column = _xlsx_cell_position(parts[1])
    if start_row > end_row or start_column > end_column:
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
    return start_row, start_column, end_row, end_column


def _validate_xlsx_worksheet(payload: bytes) -> int:
    root = _xml_root(payload)
    if any(element.tag.rsplit("}", 1)[-1] == "f" for element in root.iter()):
        raise _DoclingSourceFailure("UNSUPPORTED_XLSX_FORMULA")

    min_row: int | None = None
    min_column: int | None = None
    max_row = 0
    max_column = 0

    def include(bounds: tuple[int, int, int, int]) -> None:
        nonlocal min_row, min_column, max_row, max_column
        start_row, start_column, end_row, end_column = bounds
        min_row = start_row if min_row is None else min(min_row, start_row)
        min_column = (
            start_column if min_column is None else min(min_column, start_column)
        )
        max_row = max(max_row, end_row)
        max_column = max(max_column, end_column)
        row_span = max_row - min_row + 1
        column_span = max_column - min_column + 1
        if (
            row_span > _MAX_TABLE_ROWS
            or column_span > _MAX_TABLE_COLUMNS
            or row_span * column_span > _MAX_TABLE_LOGICAL_CELLS
        ):
            raise _DoclingSourceFailure("OFFICE_SOURCE_LIMIT_EXCEEDED")

    sheet_data = [
        element for element in root if element.tag.rsplit("}", 1)[-1] == "sheetData"
    ]
    if len(sheet_data) > 1:
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")

    cell_count = 0
    if sheet_data:
        row_counter = 0
        for row_element in sheet_data[0]:
            if row_element.tag.rsplit("}", 1)[-1] != "row":
                continue
            row_reference = row_element.attrib.get("r")
            if row_reference is None:
                row_counter += 1
            elif not row_reference.isascii() or not row_reference.isdecimal():
                raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
            else:
                row_counter = int(row_reference)
            if not 1 <= row_counter <= _XLSX_MAX_ROWS:
                raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")

            column_counter = 0
            for cell_element in row_element:
                if cell_element.tag.rsplit("}", 1)[-1] != "c":
                    continue
                cell_count += 1
                if cell_count > _MAX_TABLE_LOGICAL_CELLS:
                    raise _DoclingSourceFailure("OFFICE_SOURCE_LIMIT_EXCEEDED")
                cell_reference = cell_element.attrib.get("r")
                if cell_reference is None:
                    column_counter += 1
                    if column_counter > _XLSX_MAX_COLUMNS:
                        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
                    position = (row_counter, column_counter)
                else:
                    position = _xlsx_cell_position(cell_reference)
                    column_counter = position[1]
                include((*position, *position))

    merged_logical_cells = 0
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "mergeCell":
            continue
        reference = element.attrib.get("ref")
        if reference is None:
            raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
        bounds = _xlsx_range_bounds(reference)
        start_row, start_column, end_row, end_column = bounds
        rows = end_row - start_row + 1
        columns = end_column - start_column + 1
        if (
            rows > _MAX_TABLE_ROWS
            or columns > _MAX_TABLE_COLUMNS
            or rows * columns > _MAX_TABLE_LOGICAL_CELLS
        ):
            raise _DoclingSourceFailure("OFFICE_SOURCE_LIMIT_EXCEEDED")
        merged_logical_cells += rows * columns
        if merged_logical_cells > _MAX_TABLE_LOGICAL_CELLS:
            raise _DoclingSourceFailure("OFFICE_SOURCE_LIMIT_EXCEEDED")
        include(bounds)

    scanned_logical_cells = (
        0
        if min_row is None or min_column is None
        else (max_row - min_row + 1) * (max_column - min_column + 1)
    )
    return max(scanned_logical_cells, cell_count) + merged_logical_cells


def _xlsx_part_content_types(
    archive: ZipFile,
    names: set[str],
) -> _ContentTypeManifest:
    if _CONTENT_TYPES_MEMBER not in names:
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
    root = _xml_root(archive.read(_CONTENT_TYPES_MEMBER))
    namespace = f"{{{_CONTENT_TYPES_NAMESPACE}}}"
    if root.tag != f"{namespace}Types":
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")

    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    for child in root:
        content_type = child.attrib.get("ContentType")
        if not content_type or content_type != content_type.strip():
            raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
        if child.tag == f"{namespace}Default":
            extension = child.attrib.get("Extension")
            if (
                not extension
                or extension != extension.strip()
                or "." in extension
                or "/" in extension
                or "\\" in extension
                or extension.casefold() in defaults
            ):
                raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
            defaults[extension.casefold()] = content_type
        elif child.tag == f"{namespace}Override":
            part_name = child.attrib.get("PartName")
            if not part_name or not part_name.startswith("/"):
                raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
            member_name = part_name[1:]
            if _unsafe_archive_member(member_name) or member_name in overrides:
                raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
            overrides[member_name] = content_type
        else:
            raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")

    if any(name not in names for name in overrides):
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")

    content_types: dict[str, str] = {}
    for name in names:
        if name.endswith("/"):
            continue
        content_type = overrides.get(name)
        if content_type is None and "." in name:
            content_type = defaults.get(name.rsplit(".", 1)[1].casefold())
        if content_type is None:
            raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
        content_types[name] = content_type
    return _ContentTypeManifest(
        by_member=content_types,
        defaults=defaults,
        overrides=overrides,
    )


def _relationship_part(source_part: str) -> str:
    source = PurePosixPath(source_part)
    return str(source.parent / "_rels" / f"{source.name}.rels")


def _package_relationships(payload: bytes) -> dict[str, _PackageRelationship]:
    root = _xml_root(payload)
    namespace = f"{{{_RELATIONSHIPS_NAMESPACE}}}"
    if root.tag != f"{namespace}Relationships":
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
    relationships: dict[str, _PackageRelationship] = {}
    for child in root:
        if child.tag != f"{namespace}Relationship":
            raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
        relationship_id = child.attrib.get("Id")
        relationship_type = child.attrib.get("Type")
        target = child.attrib.get("Target")
        target_mode = child.attrib.get("TargetMode")
        if (
            not relationship_id
            or relationship_id != relationship_id.strip()
            or relationship_id in relationships
            or not relationship_type
            or relationship_type != relationship_type.strip()
            or not target
            or target != target.strip()
            or target_mode not in {None, "External"}
        ):
            raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
        relationships[relationship_id] = _PackageRelationship(
            relationship_type=relationship_type,
            target=target,
            target_mode=target_mode,
        )
    return relationships


def _resolve_package_target(source_part: str, target: str) -> str:
    if "\\" in target or "?" in target or "#" in target:
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
    if target.startswith("/"):
        parts = target[1:].split("/")
    else:
        parts = [*PurePosixPath(source_part).parent.parts, *target.split("/")]
    resolved: list[str] = []
    for part in parts:
        if not part or part == ".":
            raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
        if part == "..":
            if not resolved:
                raise _DoclingSourceFailure("UNSAFE_OFFICE_SOURCE")
            resolved.pop()
        else:
            resolved.append(part)
    member_name = "/".join(resolved)
    if _unsafe_archive_member(member_name):
        raise _DoclingSourceFailure("UNSAFE_OFFICE_SOURCE")
    return member_name


def _reject_unsupported_xlsx_relationships(
    archive: ZipFile,
    names: set[str],
    manifest: _ContentTypeManifest,
) -> None:
    relationship_types = {
        relationship.relationship_type
        for name in sorted(names)
        if name.endswith(".rels")
        for relationship in _package_relationships(archive.read(name)).values()
    }
    if _CHART_CONTENT_TYPE in manifest.by_member.values() or any(
        relationship_type.endswith(("/chart", "/chartsheet"))
        for relationship_type in relationship_types
    ):
        raise _DoclingSourceFailure("UNSUPPORTED_XLSX_CHART")
    if _DRAWING_CONTENT_TYPE in manifest.by_member.values() or any(
        relationship_type.endswith("/drawing")
        for relationship_type in relationship_types
    ):
        raise _DoclingSourceFailure("UNSUPPORTED_XLSX_DRAWING")


def _xlsx_loaded_worksheet_members(
    archive: ZipFile,
    names: set[str],
    manifest: _ContentTypeManifest,
) -> tuple[str, ...]:
    declared_types = {*manifest.defaults.values(), *manifest.overrides.values()}
    if _UNSUPPORTED_WORKBOOK_CONTENT_TYPES.intersection(declared_types):
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
    if _WORKBOOK_CONTENT_TYPE in manifest.defaults.values():
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
    workbook_parts = sorted(
        name
        for name, content_type in manifest.overrides.items()
        if content_type == _WORKBOOK_CONTENT_TYPE
    )
    if len(workbook_parts) != 1:
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
    workbook_part = workbook_parts[0]
    relationship_part = _relationship_part(workbook_part)
    if relationship_part not in names:
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
    relationships = _package_relationships(archive.read(relationship_part))

    workbook = _xml_root(archive.read(workbook_part))
    sheet_relationship_attribute = f"{{{_OFFICE_RELATIONSHIPS_NAMESPACE}}}id"
    sheets_elements = [
        element for element in workbook if element.tag.rsplit("}", 1)[-1] == "sheets"
    ]
    if len(sheets_elements) != 1:
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")

    relationship_ids: set[str] = set()
    worksheet_members: list[str] = []
    worksheet_member_set: set[str] = set()
    for sheet in sheets_elements[0]:
        if sheet.tag.rsplit("}", 1)[-1] != "sheet":
            raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
        relationship_id = sheet.attrib.get(sheet_relationship_attribute)
        if not relationship_id or relationship_id in relationship_ids:
            raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
        relationship_ids.add(relationship_id)
        relationship = relationships.get(relationship_id)
        if (
            relationship is None
            or relationship.relationship_type != _WORKSHEET_RELATIONSHIP_TYPE
            or relationship.target_mode is not None
        ):
            raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
        member_name = _resolve_package_target(workbook_part, relationship.target)
        if (
            member_name not in names
            or manifest.by_member.get(member_name) != _WORKSHEET_CONTENT_TYPE
            or member_name in worksheet_member_set
        ):
            raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
        worksheet_member_set.add(member_name)
        worksheet_members.append(member_name)

    typed_worksheets = {
        name
        for name, content_type in manifest.by_member.items()
        if content_type == _WORKSHEET_CONTENT_TYPE
    }
    if not worksheet_members or worksheet_member_set != typed_worksheets:
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
    return tuple(worksheet_members)


def _validate_office_package(path: Path, source_type: SourceType) -> None:
    if path.stat().st_size > _MAX_OFFICE_ARCHIVE_BYTES:
        raise _DoclingSourceFailure("OFFICE_SOURCE_LIMIT_EXCEEDED")
    try:
        with ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > _MAX_OFFICE_ARCHIVE_MEMBERS:
                raise _DoclingSourceFailure("OFFICE_SOURCE_LIMIT_EXCEEDED")
            names: set[str] = set()
            expanded_bytes = 0
            for member in members:
                name = member.filename
                file_type = stat.S_IFMT(member.external_attr >> 16)
                if (
                    _unsafe_archive_member(name)
                    or name in names
                    or member.flag_bits & 0x1
                    or file_type == stat.S_IFLNK
                ):
                    raise _DoclingSourceFailure("UNSAFE_OFFICE_SOURCE")
                names.add(name)
                if member.file_size > _MAX_OFFICE_MEMBER_BYTES:
                    raise _DoclingSourceFailure("OFFICE_SOURCE_LIMIT_EXCEEDED")
                expanded_bytes += member.file_size
                if expanded_bytes > _MAX_OFFICE_EXPANDED_BYTES:
                    raise _DoclingSourceFailure("OFFICE_SOURCE_LIMIT_EXCEEDED")
            if source_type is SourceType.XLSX:
                manifest = _xlsx_part_content_types(archive, names)
                _reject_unsupported_xlsx_relationships(
                    archive,
                    names,
                    manifest,
                )
                logical_work = 0
                for name in _xlsx_loaded_worksheet_members(
                    archive,
                    names,
                    manifest,
                ):
                    logical_work += _validate_xlsx_worksheet(archive.read(name))
                    if logical_work > _MAX_XLSX_WORKBOOK_LOGICAL_WORK:
                        raise _DoclingSourceFailure("OFFICE_SOURCE_LIMIT_EXCEEDED")
    except BadZipFile:
        raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE") from None


def _load_docling_sdk() -> _DoclingSDK:
    """Load only the pinned Office backends from the optional slim install."""
    from docling import (
        __version__ as docling_version,
    )
    from docling.backend.msexcel_backend import (
        MsExcelDocumentBackend,
    )
    from docling.backend.msword_backend import (
        MsWordDocumentBackend,
    )
    from docling.datamodel.base_models import (
        InputFormat,
    )
    from docling.datamodel.document import (
        InputDocument,
        get_input_rejection_cause,
    )
    from docling.datamodel.settings import (
        DocumentLimits,
    )
    from docling.exceptions import (
        DocumentLoadError,
        SecurityError,
    )

    version = _string(docling_version)
    if version != _SUPPORTED_DOCLING_VERSION:
        raise ImportError("unsupported Docling SDK version")

    def convert(path: Path, source_type: SourceType) -> object:
        _validate_office_package(path, source_type)
        input_format: Any
        backend_class: Any
        if source_type is SourceType.DOCX:
            input_format = InputFormat.DOCX
            backend_class = MsWordDocumentBackend
        elif source_type is SourceType.XLSX:
            input_format = InputFormat.XLSX
            backend_class = MsExcelDocumentBackend
        else:
            raise TypeError(
                "Docling Office boundary received an unsupported source type"
            )

        try:
            input_document = InputDocument(
                path,
                input_format,
                backend_class,
                limits=DocumentLimits(
                    max_file_size=_MAX_OFFICE_ARCHIVE_BYTES,
                    max_num_pages=10_000,
                ),
            )
        except SecurityError:
            raise _DoclingSourceFailure("UNSAFE_OFFICE_SOURCE") from None
        if _attribute(input_document, "valid") is not True:
            _raise_input_rejection(
                get_input_rejection_cause(input_document),
                document_load_error_type=DocumentLoadError,
                security_error_type=SecurityError,
            )

        backend = _attribute(input_document, "_backend")
        is_valid = _attribute(backend, "is_valid")
        convert_document = _attribute(backend, "convert")
        unload = _attribute(backend, "unload")
        if (
            not callable(is_valid)
            or not callable(convert_document)
            or not callable(unload)
        ):
            raise ImportError("unsupported Docling Office backend surface")
        if is_valid() is not True:
            raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE")
        try:
            try:
                return convert_document()
            except DocumentLoadError:
                raise _DoclingSourceFailure("INVALID_OFFICE_SOURCE") from None
            except SecurityError:
                raise _DoclingSourceFailure("UNSAFE_OFFICE_SOURCE") from None
        finally:
            unload()

    return _DoclingSDK(
        convert=convert,
        version=version,
        content_layers=_DOCLING_CONTENT_LAYERS,
    )


def _excel_column(number: int) -> str:
    if number < 1:
        _invalid()
    output: list[str] = []
    while number:
        number, remainder = divmod(number - 1, 26)
        output.append(chr(ord("A") + remainder))
    return "".join(reversed(output))


def _sheet_anchor(
    item: object,
    *,
    sheet_name: str,
    sheet_number: int,
    rows: int | None = None,
    columns: int | None = None,
) -> SourceAnchor:
    if (rows is None) != (columns is None):
        raise TypeError("row and column expectations must be supplied together")
    provenance = _sequence(_attribute(item, "prov"))
    if len(provenance) != 1:
        _invalid()
    location = provenance[0]
    if _integer(_attribute(location, "page_no"), minimum=1) != sheet_number:
        _invalid()
    charspan = _sequence(_attribute(location, "charspan"))
    if tuple(charspan) != (0, 0):
        _invalid()
    bbox = _attribute(location, "bbox")
    if _label(_attribute(bbox, "coord_origin")) != "TOPLEFT":
        _invalid()
    left = _number(_attribute(bbox, "l"))
    top = _number(_attribute(bbox, "t"))
    right = _number(_attribute(bbox, "r"))
    bottom = _number(_attribute(bbox, "b"))
    if any(not value.is_integer() or value < 0 for value in (left, top, right, bottom)):
        _invalid()
    if right <= left or bottom <= top:
        _invalid()
    if rows is not None and (right - left != columns or bottom - top != rows):
        _invalid()
    first_column = int(left) + 1
    first_row = int(top) + 1
    last_column = int(right)
    last_row = int(bottom)
    first_cell = f"{_excel_column(first_column)}{first_row}"
    last_cell = f"{_excel_column(last_column)}{last_row}"
    cell_range = first_cell if first_cell == last_cell else f"{first_cell}:{last_cell}"
    return SourceAnchor(sheet_name=sheet_name, cell_range=cell_range)


@dataclass(frozen=True, slots=True)
class _NormalizedTable:
    data: TableData
    rich_cells: tuple[tuple[str, str], ...]


def _table_data(
    item: object,
    *,
    heading: str | None,
    sheet: tuple[str, int] | None,
) -> _NormalizedTable:
    for name in ("captions", "references", "footnotes"):
        _empty_sequence(_attribute(item, name))
    if _attribute(item, "image") is not None or _attribute(item, "meta") is not None:
        _invalid()

    data = _attribute(item, "data")
    rows = _integer(_attribute(data, "num_rows"), minimum=1)
    columns = _integer(_attribute(data, "num_cols"), minimum=1)
    if (
        rows > _MAX_TABLE_ROWS
        or columns > _MAX_TABLE_COLUMNS
        or rows * columns > _MAX_TABLE_LOGICAL_CELLS
        or _label(_attribute(data, "orientation")) != "rot_0"
    ):
        _invalid()

    grid: list[list[str | None]] = [[None for _ in range(columns)] for _ in range(rows)]
    header_columns: set[int] = set()
    rich_cells: dict[str, str] = {}
    saw_header = False
    raw_cells = _sequence(_attribute(data, "table_cells"))
    if not raw_cells:
        _invalid()
    for raw_cell in raw_cells:
        if _attribute(raw_cell, "bbox") is not None:
            _invalid()
        start_row = _integer(_attribute(raw_cell, "start_row_offset_idx"))
        end_row = _integer(_attribute(raw_cell, "end_row_offset_idx"), minimum=1)
        start_column = _integer(_attribute(raw_cell, "start_col_offset_idx"))
        end_column = _integer(_attribute(raw_cell, "end_col_offset_idx"), minimum=1)
        row_span = _integer(_attribute(raw_cell, "row_span"), minimum=1)
        column_span = _integer(_attribute(raw_cell, "col_span"), minimum=1)
        if (
            end_row - start_row != row_span
            or end_column - start_column != column_span
            or end_row > rows
            or end_column > columns
        ):
            _invalid()
        text = _canonical_text(_attribute(raw_cell, "text"), allow_empty=True)
        rich_reference = getattr(raw_cell, "ref", None)
        if rich_reference is not None:
            rich_ref = _reference(rich_reference)
            if rich_ref is None or rich_ref in rich_cells:
                _invalid()
            rich_cells[rich_ref] = text
        is_header = _boolean(_attribute(raw_cell, "column_header"))
        if (
            _boolean(_attribute(raw_cell, "row_header"))
            or _boolean(_attribute(raw_cell, "row_section"))
            or _boolean(_attribute(raw_cell, "fillable"))
        ):
            _invalid()
        if is_header:
            if start_row != 0 or end_row != 1:
                _invalid()
            saw_header = True
            header_columns.update(range(start_column, end_column))
        for row in range(start_row, end_row):
            for column in range(start_column, end_column):
                if grid[row][column] is not None:
                    _invalid()
                grid[row][column] = (
                    text if row == start_row and column == start_column else ""
                )

    normalized_grid = tuple(
        tuple("" if value is None else value for value in row) for row in grid
    )
    if saw_header:
        if header_columns != set(range(columns)):
            _invalid()
        headers = normalized_grid[0]
        body = normalized_grid[1:]
    else:
        headers = ("",) * columns
        body = normalized_grid
    if not body:
        _invalid()

    if sheet is None:
        provenance = _sequence(_attribute(item, "prov"))
        if provenance:
            _invalid()
        anchor = None if heading is None else SourceAnchor(heading=heading)
        sheet_name = None
    else:
        sheet_name, sheet_number = sheet
        anchor = _sheet_anchor(
            item,
            sheet_name=sheet_name,
            sheet_number=sheet_number,
            rows=rows,
            columns=columns,
        )
    return _NormalizedTable(
        data=TableData(
            headers=headers,
            rows=body,
            sheet_name=sheet_name,
            source_anchor=anchor,
        ),
        rich_cells=tuple(rich_cells.items()),
    )


@dataclass(slots=True)
class _DoclingNormalizer:
    context: CandidateBuildContext
    source_type: SourceType
    elements: list[CanonicalElement] = field(default_factory=list)
    headings: list[tuple[int, str]] = field(default_factory=list)
    title: str | None = None
    parents: dict[str, str | None] = field(default_factory=dict)
    group_labels: dict[str, str] = field(default_factory=dict)
    sheets: dict[str, tuple[str, int]] = field(default_factory=dict)
    table_refs: set[str] = field(default_factory=set)
    rich_cell_tables: dict[str, str] = field(default_factory=dict)
    rich_cell_expected: dict[str, str] = field(default_factory=dict)
    rich_cell_parts: dict[str, list[str]] = field(default_factory=dict)
    seen_rich_cells: set[str] = field(default_factory=set)
    provider_item_count: int = 0
    canonical_text_characters: int = 0

    def _section_path(self) -> tuple[str, ...]:
        return tuple(text for _level, text in self.headings)

    def _heading_anchor(self) -> SourceAnchor | None:
        if not self.headings:
            return None
        return SourceAnchor(heading=self.headings[-1][1])

    def _append(
        self,
        element_type: ElementType,
        content: str,
        *,
        metadata: CanonicalElementMetadata | None = None,
        table_data: TableData | None = None,
        source_anchor: SourceAnchor | None = None,
    ) -> None:
        if (
            len(self.elements) >= _MAX_CANONICAL_ELEMENTS
            or self.canonical_text_characters + len(content)
            > _MAX_CANONICAL_TEXT_CHARACTERS
        ):
            _invalid()
        self.canonical_text_characters += len(content)
        self.elements.append(
            build_canonical_element(
                document_id=self.context.document_id,
                revision_id=self.context.revision_id,
                element_type=element_type,
                order=len(self.elements),
                content=content,
                section_path=self._section_path(),
                source_anchor=(
                    self._heading_anchor() if source_anchor is None else source_anchor
                ),
                metadata=metadata,
                table_data=table_data,
            )
        )

    def _ancestors(self, parent_ref: str | None) -> tuple[str, ...]:
        labels: list[str] = []
        seen: set[str] = set()
        current = parent_ref
        while current is not None:
            if current in seen or current not in self.parents:
                _invalid()
            seen.add(current)
            label = self.group_labels.get(current)
            if label is not None:
                labels.append(label)
            current = self.parents[current]
        return tuple(labels)

    def _sheet(self, parent_ref: str | None) -> tuple[str, int] | None:
        seen: set[str] = set()
        current = parent_ref
        found: tuple[str, int] | None = None
        while current is not None:
            if current in seen or current not in self.parents:
                _invalid()
            seen.add(current)
            sheet = self.sheets.get(current)
            if sheet is not None:
                if found is not None:
                    _invalid()
                found = sheet
            current = self.parents[current]
        return found

    def _rich_cell_root(self, parent_ref: str | None) -> str | None:
        seen: set[str] = set()
        current = parent_ref
        while current is not None:
            if current in seen or current not in self.parents:
                _invalid()
            seen.add(current)
            if current in self.rich_cell_tables:
                return current
            current = self.parents[current]
        return None

    def _consume_rich_cell_descendant(self, root_ref: str, item: object) -> None:
        label = _label(_attribute(item, "label"))
        if label in _FLATTENABLE_TABLE_GROUP_LABELS:
            _string(_attribute(item, "name"))
            return
        if label in _TEXT_LABELS:
            self.rich_cell_parts[root_ref].append(_provider_text(item))
            return
        _invalid()

    def validate_rich_cells(self) -> None:
        if self.seen_rich_cells != set(self.rich_cell_tables):
            _invalid()
        for root_ref, parts in self.rich_cell_parts.items():
            flattened = _canonical_text("\n".join(parts), allow_empty=True)
            if flattened != self.rich_cell_expected[root_ref]:
                _invalid()

    def consume(self, item: object, depth: object) -> None:
        self.provider_item_count += 1
        if self.provider_item_count > _MAX_PROVIDER_ITEMS:
            _invalid()
        _integer(depth)
        if _label(_attribute(item, "content_layer")) != "body":
            _invalid()
        self_ref = _string(_attribute(item, "self_ref"))
        if self_ref in self.parents:
            _invalid()
        parent_ref = _reference(_attribute(item, "parent"))
        if parent_ref is not None and parent_ref not in self.parents:
            _invalid()
        self.parents[self_ref] = parent_ref
        label = _label(_attribute(item, "label"))

        if self_ref in self.rich_cell_tables:
            if (
                parent_ref != self.rich_cell_tables[self_ref]
                or label not in _FLATTENABLE_TABLE_GROUP_LABELS
            ):
                _invalid()
            _string(_attribute(item, "name"))
            self.seen_rich_cells.add(self_ref)
            return
        rich_cell_root = self._rich_cell_root(parent_ref)
        if rich_cell_root is not None:
            self._consume_rich_cell_descendant(rich_cell_root, item)
            return
        if parent_ref in self.table_refs:
            _invalid()

        if label in _GROUP_LABELS:
            name = _string(_attribute(item, "name"))
            if label == "sheet":
                if self.source_type is not SourceType.XLSX:
                    _invalid()
                if name in (value[0] for value in self.sheets.values()):
                    _invalid()
                self.sheets[self_ref] = (name, len(self.sheets) + 1)
            elif self.source_type is SourceType.XLSX and label != "unspecified":
                _invalid()
            self.group_labels[self_ref] = label
            return

        sheet = self._sheet(parent_ref)
        if self.source_type is SourceType.XLSX:
            if sheet is None or (label != "table" and label not in _TEXT_LABELS):
                _invalid()
        if self.source_type is SourceType.DOCX and sheet is not None:
            _invalid()

        if label in {"title", "section_header"}:
            content = _provider_text(item)
            level = (
                1
                if label == "title"
                else _integer(_attribute(item, "level"), minimum=1)
            )
            if level > 6:
                _invalid()
            while self.headings and self.headings[-1][0] >= level:
                self.headings.pop()
            self.headings.append((level, content))
            if self.title is None and level == 1:
                self.title = content
            self._append(
                ElementType.HEADING,
                content,
                metadata=CanonicalElementMetadata(heading_level=level),
            )
        elif label in _TEXT_LABELS:
            source_anchor = None
            if sheet is not None:
                sheet_name, sheet_number = sheet
                source_anchor = _sheet_anchor(
                    item,
                    sheet_name=sheet_name,
                    sheet_number=sheet_number,
                )
            self._append(
                ElementType.PARAGRAPH,
                _provider_text(item),
                source_anchor=source_anchor,
            )
        elif label == "list_item":
            content = _provider_text(item)
            enumerated = _boolean(_attribute(item, "enumerated"))
            _string(_attribute(item, "marker"), allow_blank=True)
            ancestors = self._ancestors(parent_ref)
            list_groups = [value for value in ancestors if value in _LIST_GROUP_LABELS]
            if not list_groups:
                _invalid()
            if list_groups[0] == "ordered_list" and not enumerated:
                _invalid()
            self._append(
                ElementType.LIST,
                content,
                metadata=CanonicalElementMetadata(
                    list_kind=(ListKind.ORDERED if enumerated else ListKind.UNORDERED),
                    list_depth=len(list_groups) - 1,
                ),
            )
        elif label == "code":
            for name in ("captions", "references", "footnotes"):
                _empty_sequence(_attribute(item, name))
            if _attribute(item, "image") is not None:
                _invalid()
            language_value = _attribute(item, "code_language")
            language = None
            if language_value is not None:
                language = _label(language_value).casefold()
            self._append(
                ElementType.CODE,
                _provider_text(item),
                metadata=CanonicalElementMetadata(code_language=language),
            )
        elif label == "table":
            normalized_table = _table_data(
                item,
                heading=self.headings[-1][1] if self.headings else None,
                sheet=sheet,
            )
            table = normalized_table.data
            self.table_refs.add(self_ref)
            for rich_ref, expected_text in normalized_table.rich_cells:
                if rich_ref in self.parents or rich_ref in self.rich_cell_tables:
                    _invalid()
                self.rich_cell_tables[rich_ref] = self_ref
                self.rich_cell_expected[rich_ref] = expected_text
                self.rich_cell_parts[rich_ref] = []
            self._append(
                ElementType.TABLE,
                render_table_data(table),
                table_data=table,
                source_anchor=table.source_anchor,
            )
        else:
            _invalid()


def _normalize_docling_document(
    document: object,
    source: SourceFile,
    context: CandidateBuildContext,
    *,
    parser_version: str,
    included_content_layers: frozenset[object],
) -> DocumentContainer:
    try:
        iterate_items = _attribute(document, "iterate_items")
        if not callable(iterate_items):
            _invalid()
        records = iterate_items(
            with_groups=True,
            included_content_layers=included_content_layers,
        )
        if not isinstance(records, Iterable):
            _invalid()
        normalizer = _DoclingNormalizer(
            context=context,
            source_type=source.source_type,
        )
        for record in records:
            if not isinstance(record, tuple) or len(record) != 2:
                _invalid()
            item, depth = record
            normalizer.consume(item, depth)
        normalizer.validate_rich_cells()
        if not normalizer.elements:
            _invalid()
        sheet_names = tuple(value[0] for value in normalizer.sheets.values())
        if source.source_type is SourceType.XLSX and not sheet_names:
            _invalid()
        metadata = DocumentMetadata(
            title=normalizer.title,
            sheet_names=sheet_names,
        )
    except (_InvalidDoclingOutput, ValidationError):
        raise ProviderResponseError from None
    return successful_document(
        source,
        context,
        parser_name="docling",
        parser_version=parser_version,
        elements=tuple(normalizer.elements),
        metadata=metadata,
    )


class DoclingParser:
    """Parse DOCX/XLSX through the pinned Docling Slim Office backends."""

    async def parse(
        self,
        source: SourceFile,
        context: CandidateBuildContext,
    ) -> DocumentContainer:
        source, context = validate_parse_request(
            source,
            context,
            allowed_source_types=(SourceType.DOCX, SourceType.XLSX),
        )
        path = resolve_source_target(source)
        load_failure: DependencyUnavailableError | ProviderResponseError | None = None
        try:
            sdk = _load_docling_sdk()
        except ImportError:
            load_failure = DependencyUnavailableError()
        except Exception:
            load_failure = ProviderResponseError()
        if load_failure is not None:
            raise load_failure

        def convert_and_normalize() -> (
            DocumentContainer | _DoclingExecutionFailure | _DoclingSourceFailureResult
        ):
            try:
                document = sdk.convert(path, source.source_type)
                return _normalize_docling_document(
                    document,
                    source,
                    context,
                    parser_version=sdk.version,
                    included_content_layers=sdk.content_layers,
                )
            except TimeoutError:
                return _DoclingExecutionFailure.TIMEOUT
            except ImportError:
                return _DoclingExecutionFailure.UNAVAILABLE
            except _DoclingSourceFailure as failure:
                return _DoclingSourceFailureResult(failure.code)
            except InvariantViolationError:
                return _DoclingExecutionFailure.INVARIANT_VIOLATION
            except Exception:
                return _DoclingExecutionFailure.INVALID_RESPONSE

        outcome = await asyncio.to_thread(convert_and_normalize)
        if isinstance(outcome, DocumentContainer):
            return outcome
        if outcome is _DoclingExecutionFailure.TIMEOUT:
            raise DependencyTimeoutError
        if outcome is _DoclingExecutionFailure.UNAVAILABLE:
            raise DependencyUnavailableError
        if outcome is _DoclingExecutionFailure.INVARIANT_VIOLATION:
            raise InvariantViolationError
        if outcome is _DoclingExecutionFailure.INVALID_RESPONSE:
            raise ProviderResponseError
        if not isinstance(outcome, _DoclingSourceFailureResult):
            raise ProviderResponseError
        return failed_document(
            source,
            context,
            parser_name="docling",
            parser_version=sdk.version,
            failure_code=outcome.code,
        )
