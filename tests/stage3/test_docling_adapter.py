"""Lazy Docling Office adapter and provider-neutral normalization behavior."""

from __future__ import annotations

import builtins
import importlib
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from zipfile import ZipFile

import pytest

from atlasrag.domain.enums import SourceType
from atlasrag.domain.errors import (
    DependencyTimeoutError,
    DependencyUnavailableError,
    InvariantViolationError,
    ProviderResponseError,
)
from atlasrag.domain.ingestion.enums import ElementType, ListKind, ParseOutcome
from atlasrag.domain.ingestion.source import CandidateBuildContext, SourceFile
from atlasrag.providers.parsing import docling as docling_adapter
from atlasrag.services.ingestion.identity import build_candidate_context

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stage3"
PIPELINE_FINGERPRINT = "e" * 64


class FakeDocument:
    def __init__(self, items: list[tuple[object, int]]) -> None:
        self.items = items
        self.calls: list[bool] = []
        self.layer_calls: list[frozenset[object] | None] = []
        self.iteration_threads: list[int] = []
        self.name = "provider-document"
        self.origin = SimpleNamespace(
            filename="provider-input",
            mimetype="provider-mimetype",
            binary_hash=123456,
        )

    def iterate_items(
        self,
        *,
        with_groups: bool = False,
        included_content_layers: frozenset[object] | None = None,
    ) -> list[tuple[object, int]]:
        self.calls.append(with_groups)
        self.layer_calls.append(included_content_layers)
        self.iteration_threads.append(threading.get_ident())
        allowed = (
            {"body"} if included_content_layers is None else included_content_layers
        )
        return [
            (item, depth)
            for item, depth in self.items
            if getattr(item, "content_layer", "body") in allowed
        ]


def ref(value: str) -> SimpleNamespace:
    return SimpleNamespace(cref=value)


def node(
    self_ref: str,
    label: str,
    *,
    parent: str | None,
    content_layer: str = "body",
    **values: object,
) -> SimpleNamespace:
    return SimpleNamespace(
        self_ref=self_ref,
        label=label,
        parent=None if parent is None else ref(parent),
        content_layer=content_layer,
        **values,
    )


def cell(
    text: str,
    row: int,
    column: int,
    *,
    row_span: int = 1,
    column_span: int = 1,
    column_header: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        bbox=None,
        row_span=row_span,
        col_span=column_span,
        start_row_offset_idx=row,
        end_row_offset_idx=row + row_span,
        start_col_offset_idx=column,
        end_col_offset_idx=column + column_span,
        text=text,
        column_header=column_header,
        row_header=False,
        row_section=False,
        fillable=False,
    )


def table_item(
    self_ref: str,
    parent: str,
    *,
    rows: int = 3,
    columns: int = 2,
    cells: list[object] | None = None,
    provenance: list[object] | None = None,
) -> SimpleNamespace:
    if cells is None:
        cells = [
            cell("Name", 0, 0, column_header=True),
            cell("Value", 0, 1, column_header=True),
            cell("alpha", 1, 0),
            cell("1", 1, 1),
            cell("beta", 2, 0),
            cell("2", 2, 1),
        ]
    return node(
        self_ref,
        "table",
        parent=parent,
        captions=[],
        references=[],
        footnotes=[],
        image=None,
        meta=None,
        prov=[] if provenance is None else provenance,
        data=SimpleNamespace(
            num_rows=rows,
            num_cols=columns,
            orientation="rot_0",
            table_cells=cells,
        ),
    )


def fake_docx_document() -> FakeDocument:
    body = "#/body"
    section = "#/groups/0"
    heading = "#/texts/0"
    list_group = "#/groups/1"
    return FakeDocument(
        [
            (node(body, "unspecified", parent=None, name="_root_"), 0),
            (node(section, "section", parent=body, name="header-0"), 1),
            (
                node(
                    heading,
                    "section_header",
                    parent=section,
                    text="AtlasRAG Office Fixture",
                    orig="AtlasRAG Office Fixture",
                    level=1,
                ),
                2,
            ),
            (
                node(
                    "#/texts/1",
                    "text",
                    parent=heading,
                    text="Canonical paragraph from Docling.",
                    orig="Canonical paragraph from Docling.",
                ),
                3,
            ),
            (node(list_group, "list", parent=heading, name="list"), 3),
            (
                node(
                    "#/texts/2",
                    "list_item",
                    parent=list_group,
                    text="Alpha item",
                    orig="Alpha item",
                    enumerated=False,
                    marker="",
                ),
                4,
            ),
            (
                node(
                    "#/texts/3",
                    "list_item",
                    parent=list_group,
                    text="Beta item",
                    orig="Beta item",
                    enumerated=False,
                    marker="",
                ),
                4,
            ),
            (
                node(
                    "#/texts/4",
                    "code",
                    parent=heading,
                    text='def office_fixture():\n    return "docx"',
                    orig='def office_fixture():\n    return "docx"',
                    code_language="Python",
                    captions=[],
                    references=[],
                    footnotes=[],
                    image=None,
                ),
                3,
            ),
            (table_item("#/tables/0", heading), 3),
        ]
    )


def fake_xlsx_document() -> FakeDocument:
    body = "#/body"
    sheet = "#/groups/0"
    provenance = SimpleNamespace(
        page_no=1,
        bbox=SimpleNamespace(
            l=0.0,
            t=0.0,
            r=2.0,
            b=3.0,
            coord_origin="TOPLEFT",
        ),
        charspan=(0, 0),
    )
    return FakeDocument(
        [
            (node(body, "unspecified", parent=None, name="_root_"), 0),
            (node(sheet, "sheet", parent=body, name="Metrics"), 1),
            (
                table_item(
                    "#/tables/0",
                    sheet,
                    provenance=[provenance],
                ),
                2,
            ),
        ]
    )


def prepared_source(
    root: Path, fixture_name: str
) -> tuple[SourceFile, CandidateBuildContext]:
    path = root / fixture_name
    path.write_bytes((FIXTURES / fixture_name).read_bytes())
    source = SourceFile.from_knowledge_directory(root, fixture_name)
    return source, build_candidate_context(source, PIPELINE_FINGERPRINT)


def rewrite_xlsx_fixture(
    path: Path,
    *,
    replacements: dict[str, bytes] | None = None,
    additions: dict[str, bytes] | None = None,
) -> None:
    replacements = {} if replacements is None else replacements
    additions = {} if additions is None else additions
    with ZipFile(FIXTURES / "simple.xlsx") as source, ZipFile(path, "w") as target:
        source_names = set(source.namelist())
        assert not source_names.intersection(additions)
        for member in source.infolist():
            target.writestr(
                member,
                replacements.get(member.filename, source.read(member.filename)),
            )
        for name, payload in additions.items():
            target.writestr(name, payload)


def write_minimal_xlsx_package(path: Path, worksheets: dict[str, bytes]) -> None:
    content_type_overrides = [
        '<Override PartName="/xl/custom/workbook.data" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.sheet.main+xml"/>'
    ]
    sheet_elements: list[str] = []
    relationships: list[str] = []
    for index, member_name in enumerate(worksheets, start=1):
        content_type_overrides.append(
            f'<Override PartName="/{member_name}" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.worksheet+xml"/>'
        )
        sheet_elements.append(
            f'<sheet name="Sheet {index}" sheetId="{index}" r:id="rId{index}"/>'
        )
        relationships.append(
            '<Relationship Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/worksheet" '
            f'Target="/{member_name}" Id="rId{index}"/>'
        )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{''.join(content_type_overrides)}"
        "</Types>"
    ).encode()
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships">'
        f"<sheets>{''.join(sheet_elements)}</sheets>"
        "</workbook>"
    ).encode()
    workbook_relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/'
        'package/2006/relationships">'
        f"{''.join(relationships)}"
        "</Relationships>"
    ).encode()

    with ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/custom/workbook.data", workbook)
        archive.writestr(
            "xl/custom/_rels/workbook.data.rels",
            workbook_relationships,
        )
        for member_name, payload in worksheets.items():
            archive.writestr(member_name, payload)


def fake_sdk(convert: object) -> SimpleNamespace:
    return SimpleNamespace(
        convert=convert,
        version="2.135.0",
        content_layers=frozenset({"body", "furniture", "notes"}),
    )


def test_importing_adapter_does_not_import_optional_docling_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for module_name in tuple(sys.modules):
        if module_name == "docling" or module_name.startswith("docling."):
            sys.modules.pop(module_name)
    original_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "docling" or name.startswith("docling."):
            raise AssertionError("adapter imported Docling before parse")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    importlib.reload(docling_adapter)

    assert "docling" not in sys.modules


@pytest.mark.asyncio
async def test_missing_optional_sdk_is_dependency_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path, "simple.docx")

    def missing_sdk() -> object:
        raise ModuleNotFoundError("No module named 'docling'")

    monkeypatch.setattr(docling_adapter, "_load_docling_sdk", missing_sdk)

    with pytest.raises(DependencyUnavailableError):
        await docling_adapter.DoclingParser().parse(source, context)


@pytest.mark.asyncio
async def test_docx_is_normalized_in_reading_order_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path, "simple.docx")
    document = fake_docx_document()
    calls: list[tuple[Path, object]] = []
    parse_threads: list[int] = []
    calling_thread = threading.get_ident()

    def convert(path: Path, source_type: object) -> FakeDocument:
        calls.append((path, source_type))
        parse_threads.append(threading.get_ident())
        return document

    monkeypatch.setattr(
        docling_adapter,
        "_load_docling_sdk",
        lambda: fake_sdk(convert),
    )

    parsed = await docling_adapter.DoclingParser().parse(source, context)

    assert calls == [(source.absolute_path.resolve(), source.source_type)]
    assert parse_threads and parse_threads != [calling_thread]
    assert document.iteration_threads and document.iteration_threads != [calling_thread]
    assert document.calls == [True]
    assert [element.element_type for element in parsed.elements] == [
        ElementType.HEADING,
        ElementType.PARAGRAPH,
        ElementType.LIST,
        ElementType.LIST,
        ElementType.CODE,
        ElementType.TABLE,
    ]
    assert [element.order for element in parsed.elements] == list(range(6))
    assert parsed.metadata.title == "AtlasRAG Office Fixture"
    assert parsed.elements[0].metadata.heading_level == 1
    assert all(
        item.metadata.list_kind is ListKind.UNORDERED and item.metadata.list_depth == 0
        for item in parsed.elements[2:4]
    )
    assert parsed.elements[4].metadata.code_language == "python"
    table = parsed.elements[5].table_data
    assert table is not None
    assert table.headers == ("Name", "Value")
    assert table.rows == (("alpha", "1"), ("beta", "2"))
    assert table.sheet_name is None
    assert table.source_anchor == parsed.elements[5].source_anchor
    assert parsed.parse_info.outcome is ParseOutcome.SUCCESS
    assert parsed.parse_info.parser_name == "docling"
    assert parsed.parse_info.parser_version == "2.135.0"
    assert "provider-mimetype" not in parsed.model_dump_json()
    assert "123456" not in parsed.model_dump_json()


@pytest.mark.asyncio
async def test_docx_numbered_list_uses_item_enumerated_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path, "simple.docx")
    document = fake_docx_document()
    for item, _depth in document.items:
        if getattr(item, "label", None) == "list_item":
            cast(SimpleNamespace, item).enumerated = True
    monkeypatch.setattr(
        docling_adapter,
        "_load_docling_sdk",
        lambda: fake_sdk(lambda *_args: document),
    )

    parsed = await docling_adapter.DoclingParser().parse(source, context)

    assert all(
        item.metadata.list_kind is ListKind.ORDERED for item in parsed.elements[2:4]
    )


@pytest.mark.asyncio
async def test_xlsx_preserves_sheet_name_and_structural_cell_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path, "simple.xlsx")
    document = fake_xlsx_document()
    monkeypatch.setattr(
        docling_adapter,
        "_load_docling_sdk",
        lambda: fake_sdk(lambda *_args: document),
    )

    parsed = await docling_adapter.DoclingParser().parse(source, context)

    assert parsed.metadata.sheet_names == ("Metrics",)
    assert [element.element_type for element in parsed.elements] == [ElementType.TABLE]
    table = parsed.elements[0].table_data
    assert table is not None
    assert table.sheet_name == "Metrics"
    assert table.source_anchor is not None
    assert table.source_anchor.sheet_name == "Metrics"
    assert table.source_anchor.cell_range == "A1:B3"
    assert table.source_anchor.page_number is None


@pytest.mark.asyncio
async def test_xlsx_unrepresentable_anchor_number_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path, "simple.xlsx")
    document = fake_xlsx_document()
    table = cast(SimpleNamespace, document.items[-1][0])
    table.prov[0].bbox.r = 10**10_000
    monkeypatch.setattr(
        docling_adapter,
        "_load_docling_sdk",
        lambda: fake_sdk(lambda *_args: document),
    )

    with pytest.raises(ProviderResponseError):
        await docling_adapter.DoclingParser().parse(source, context)


@pytest.mark.asyncio
async def test_structural_merged_and_missing_cells_are_lossless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path, "simple.xlsx")
    document = fake_xlsx_document()
    table = cast(SimpleNamespace, document.items[-1][0])
    table.data = SimpleNamespace(
        num_rows=3,
        num_cols=3,
        orientation="rot_0",
        table_cells=[
            cell("Group", 0, 0, column_span=2, column_header=True),
            cell("Value", 0, 2, column_header=True),
            cell("alpha", 1, 0, row_span=2),
            cell("x", 1, 1),
            cell("1", 1, 2),
            cell("2", 2, 2),
        ],
    )
    table.prov[0].bbox.r = 3.0
    monkeypatch.setattr(
        docling_adapter,
        "_load_docling_sdk",
        lambda: fake_sdk(lambda *_args: document),
    )

    parsed = await docling_adapter.DoclingParser().parse(source, context)

    canonical = parsed.elements[0].table_data
    assert canonical is not None
    assert canonical.headers == ("Group", "", "Value")
    assert canonical.rows == (("alpha", "x", "1"), ("", "", "2"))
    assert canonical.source_anchor is not None
    assert canonical.source_anchor.cell_range == "A1:C3"


@pytest.mark.asyncio
async def test_rich_table_cell_is_flattened_without_duplicate_elements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path, "simple.docx")
    document = fake_docx_document()
    table = cast(SimpleNamespace, document.items[-1][0])
    rich_group = "#/groups/rich-cell"
    rich_cell = table.data.table_cells[2]
    rich_cell.text = "alpha\ncontinued"
    rich_cell.ref = ref(rich_group)
    document.items.extend(
        [
            (node(rich_group, "unspecified", parent=table.self_ref, name="rich"), 4),
            (
                node(
                    "#/texts/rich-0",
                    "text",
                    parent=rich_group,
                    text="alpha",
                    orig="alpha",
                ),
                5,
            ),
            (
                node(
                    "#/texts/rich-1",
                    "text",
                    parent=rich_group,
                    text="continued",
                    orig="continued",
                ),
                5,
            ),
        ]
    )
    monkeypatch.setattr(
        docling_adapter,
        "_load_docling_sdk",
        lambda: fake_sdk(lambda *_args: document),
    )

    parsed = await docling_adapter.DoclingParser().parse(source, context)

    assert [element.element_type for element in parsed.elements] == [
        ElementType.HEADING,
        ElementType.PARAGRAPH,
        ElementType.LIST,
        ElementType.LIST,
        ElementType.CODE,
        ElementType.TABLE,
    ]
    table_data = parsed.elements[-1].table_data
    assert table_data is not None
    assert table_data.rows[0][0] == "alpha\ncontinued"


@pytest.mark.asyncio
async def test_non_body_office_content_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path, "simple.docx")
    document = fake_docx_document()
    document.items.extend(
        [
            (
                node(
                    "#/groups/header",
                    "section",
                    parent="#/body",
                    content_layer="furniture",
                    name="header",
                ),
                1,
            ),
            (
                node(
                    "#/texts/header",
                    "text",
                    parent="#/groups/header",
                    content_layer="furniture",
                    text="Confidential",
                    orig="Confidential",
                ),
                2,
            ),
        ]
    )
    monkeypatch.setattr(
        docling_adapter,
        "_load_docling_sdk",
        lambda: fake_sdk(lambda *_args: document),
    )

    with pytest.raises(ProviderResponseError):
        await docling_adapter.DoclingParser().parse(source, context)


@pytest.mark.asyncio
async def test_table_without_provider_cells_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path, "simple.xlsx")
    document = fake_xlsx_document()
    table = cast(SimpleNamespace, document.items[-1][0])
    table.data.table_cells = []
    monkeypatch.setattr(
        docling_adapter,
        "_load_docling_sdk",
        lambda: fake_sdk(lambda *_args: document),
    )

    with pytest.raises(ProviderResponseError):
        await docling_adapter.DoclingParser().parse(source, context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit_name", "limit"),
    [
        ("_MAX_PROVIDER_ITEMS", 1),
        ("_MAX_CANONICAL_TEXT_CHARACTERS", 8),
    ],
)
async def test_document_resource_limits_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
) -> None:
    source, context = prepared_source(tmp_path, "simple.docx")
    monkeypatch.setattr(docling_adapter, limit_name, limit, raising=False)
    monkeypatch.setattr(
        docling_adapter,
        "_load_docling_sdk",
        lambda: fake_sdk(lambda *_args: fake_docx_document()),
    )

    with pytest.raises(ProviderResponseError):
        await docling_adapter.DoclingParser().parse(source, context)


def test_input_rejection_preserves_unknown_backend_failure() -> None:
    class KnownLoadError(Exception):
        pass

    class KnownSecurityError(Exception):
        pass

    defect = RuntimeError("unexpected InputDocument defect")

    with pytest.raises(RuntimeError, match="unexpected InputDocument defect"):
        docling_adapter._raise_input_rejection(
            defect,
            document_load_error_type=KnownLoadError,
            security_error_type=KnownSecurityError,
        )


@pytest.mark.parametrize(
    ("member_name", "member_bytes", "limit", "failure_code"),
    [
        ("../word/document.xml", b"safe size", None, "UNSAFE_OFFICE_SOURCE"),
        ("word/./document.xml", b"safe size", None, "UNSAFE_OFFICE_SOURCE"),
        (
            "word/document.xml",
            b"over the test limit",
            4,
            "OFFICE_SOURCE_LIMIT_EXCEEDED",
        ),
    ],
)
def test_office_archive_preflight_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    member_name: str,
    member_bytes: bytes,
    limit: int | None,
    failure_code: str,
) -> None:
    path = tmp_path / "unsafe.docx"
    with ZipFile(path, "w") as archive:
        archive.writestr(member_name, member_bytes)
    if limit is not None:
        monkeypatch.setattr(docling_adapter, "_MAX_OFFICE_MEMBER_BYTES", limit)

    with pytest.raises(docling_adapter._DoclingSourceFailure) as failure:
        docling_adapter._validate_office_package(path, SourceType.DOCX)

    assert failure.value.code == failure_code


def test_xlsx_formula_preflight_uses_opc_content_type(
    tmp_path: Path,
) -> None:
    path = tmp_path / "custom-sheet-path.xlsx"
    worksheet = b"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row r="1"><c r="A1"><f>1+2</f><v/></c></row></sheetData>
</worksheet>
"""
    write_minimal_xlsx_package(
        path,
        {"xl/custom/sheet1.data": worksheet},
    )

    with pytest.raises(docling_adapter._DoclingSourceFailure) as failure:
        docling_adapter._validate_office_package(path, SourceType.XLSX)

    assert failure.value.code == "UNSUPPORTED_XLSX_FORMULA"


@pytest.mark.parametrize(
    "worksheet_body",
    [
        '<sheetData><row r="1"><c r="A1"/></row>'
        '<row r="1048576"><c r="XFD1048576"/></row></sheetData>',
        '<sheetData><row r="1"><c r="A1"/></row></sheetData>'
        '<mergeCells count="1"><mergeCell ref="A1:XFD1048576"/></mergeCells>',
    ],
)
def test_xlsx_preflight_rejects_oversized_logical_extent_before_docling(
    tmp_path: Path,
    worksheet_body: str,
) -> None:
    path = tmp_path / "oversized-logical-extent.xlsx"
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{worksheet_body}"
        "</worksheet>"
    ).encode()
    write_minimal_xlsx_package(
        path,
        {"xl/custom/sheet1.data": worksheet},
    )

    with pytest.raises(docling_adapter._DoclingSourceFailure) as failure:
        docling_adapter._validate_office_package(path, SourceType.XLSX)

    assert failure.value.code == "OFFICE_SOURCE_LIMIT_EXCEEDED"


def test_xlsx_preflight_applies_logical_work_budget_across_worksheets(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cumulative-logical-work.xlsx"
    worksheet = b"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row r="1"><c r="A1"/></row></sheetData>
  <mergeCells count="1"><mergeCell ref="A1:BL4096"/></mergeCells>
</worksheet>
"""
    write_minimal_xlsx_package(
        path,
        {
            "xl/custom/sheet1.data": worksheet,
            "xl/custom/sheet2.data": worksheet,
        },
    )

    with pytest.raises(docling_adapter._DoclingSourceFailure) as failure:
        docling_adapter._validate_office_package(path, SourceType.XLSX)

    assert failure.value.code == "OFFICE_SOURCE_LIMIT_EXCEEDED"


def test_xlsx_preflight_rejects_loaded_sheet_with_wrong_content_type(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wrong-sheet-content-type.xlsx"
    with ZipFile(FIXTURES / "simple.xlsx") as source:
        content_types = source.read("[Content_Types].xml").replace(
            b"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
            b"application/xml",
        )
        worksheet = source.read("xl/worksheets/sheet1.xml").replace(
            b'<c r="B2" t="n"><v>1</v></c>',
            b'<c r="B2"><f>1+2</f><v/></c>',
        )
    rewrite_xlsx_fixture(
        path,
        replacements={
            "[Content_Types].xml": content_types,
            "xl/worksheets/sheet1.xml": worksheet,
        },
    )

    with pytest.raises(docling_adapter._DoclingSourceFailure) as failure:
        docling_adapter._validate_office_package(path, SourceType.XLSX)

    assert failure.value.code == "INVALID_OFFICE_SOURCE"


def test_xlsx_preflight_rejects_aliased_workbook_sheet_relationship(
    tmp_path: Path,
) -> None:
    path = tmp_path / "aliased-sheet.xlsx"
    with ZipFile(FIXTURES / "simple.xlsx") as source:
        workbook = source.read("xl/workbook.xml").replace(
            b"</sheets>",
            b'<sheet xmlns:r="http://schemas.openxmlformats.org/officeDocument/'
            b'2006/relationships" name="Alias" sheetId="2" state="visible" '
            b'r:id="rId1"/></sheets>',
        )
    rewrite_xlsx_fixture(
        path,
        replacements={"xl/workbook.xml": workbook},
    )

    with pytest.raises(docling_adapter._DoclingSourceFailure) as failure:
        docling_adapter._validate_office_package(path, SourceType.XLSX)

    assert failure.value.code == "INVALID_OFFICE_SOURCE"


def test_xlsx_preflight_rejects_competing_backend_workbook_type(
    tmp_path: Path,
) -> None:
    path = tmp_path / "competing-workbook-types.xlsx"
    with ZipFile(FIXTURES / "simple.xlsx") as source:
        content_types = source.read("[Content_Types].xml").replace(
            b"</Types>",
            b'<Override PartName="/xl/alternate/workbook.xml" '
            b'ContentType="application/vnd.ms-excel.sheet.macroEnabled.main+xml"/>'
            b"</Types>",
        )
    rewrite_xlsx_fixture(
        path,
        replacements={"[Content_Types].xml": content_types},
        additions={
            "xl/alternate/workbook.xml": b"""
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Alternate" sheetId="1" r:id="rId1"/></sheets>
</workbook>
""",
            "xl/alternate/_rels/workbook.xml.rels": b"""
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
    Target="/xl/alternate/formula.xml" Id="rId1"/>
</Relationships>
""",
            "xl/alternate/formula.xml": b"""
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row r="1"><c r="A1"><f>1+2</f><v/></c></row></sheetData>
</worksheet>
""",
        },
    )

    with pytest.raises(docling_adapter._DoclingSourceFailure) as failure:
        docling_adapter._validate_office_package(path, SourceType.XLSX)

    assert failure.value.code == "INVALID_OFFICE_SOURCE"


def test_xlsx_preflight_rejects_workbook_type_inherited_from_default(
    tmp_path: Path,
) -> None:
    path = tmp_path / "default-workbook-type.xlsx"
    workbook_content_type = (
        b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    )
    with ZipFile(FIXTURES / "simple.xlsx") as source:
        content_types = source.read("[Content_Types].xml").replace(
            b"application/vnd.openxmlformats-officedocument.spreadsheetml."
            b"worksheet+xml",
            b"application/xml",
        )
        content_types = content_types.replace(
            b'<Override PartName="/xl/workbook.xml" ContentType="'
            + workbook_content_type
            + b'"/>',
            b'<Default Extension="data" ContentType="'
            + workbook_content_type
            + b'"/><Override PartName="/xl/alternate-sheet.xml" '
            b'ContentType="application/vnd.openxmlformats-officedocument.'
            b'spreadsheetml.worksheet+xml"/>',
        )
        worksheet = source.read("xl/worksheets/sheet1.xml").replace(
            b'<c r="B2" t="n"><v>1</v></c>',
            b'<c r="B2"><f>1+2</f><v/></c>',
        )
    rewrite_xlsx_fixture(
        path,
        replacements={
            "[Content_Types].xml": content_types,
            "xl/worksheets/sheet1.xml": worksheet,
        },
        additions={
            "xl/alternate.data": b"""
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Alternate" sheetId="1" r:id="rId1"/></sheets>
</workbook>
""",
            "xl/_rels/alternate.data.rels": b"""
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
    Target="/xl/alternate-sheet.xml" Id="rId1"/>
</Relationships>
""",
            "xl/alternate-sheet.xml": b"""
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>safe</t></is></c></row></sheetData>
</worksheet>
""",
        },
    )

    with pytest.raises(docling_adapter._DoclingSourceFailure) as failure:
        docling_adapter._validate_office_package(path, SourceType.XLSX)

    assert failure.value.code == "INVALID_OFFICE_SOURCE"


def test_xlsx_preflight_rejects_chart_relationship_before_docling(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chart-reference.xlsx"
    with ZipFile(FIXTURES / "simple.xlsx") as source:
        worksheet = source.read("xl/worksheets/sheet1.xml").replace(
            b"</worksheet>",
            b'<drawing xmlns:r="http://schemas.openxmlformats.org/officeDocument/'
            b'2006/relationships" r:id="rId1"/></worksheet>',
        )
    rewrite_xlsx_fixture(
        path,
        replacements={"xl/worksheets/sheet1.xml": worksheet},
        additions={
            "xl/worksheets/_rels/sheet1.xml.rels": b"""
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing"
    Target="/xl/drawings/drawing1.xml" Id="rId1"/>
</Relationships>
""",
            "xl/drawings/drawing1.xml": b"""
<wsDr xmlns="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"/>
""",
            "xl/drawings/_rels/drawing1.xml.rels": b"""
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"
    Target="/xl/charts/chart1.xml" Id="rId1"/>
</Relationships>
""",
            "xl/charts/chart1.xml": b"""
<chartSpace xmlns="http://schemas.openxmlformats.org/drawingml/2006/chart">
  <chart><plotArea><barChart><ser><val><numRef>
    <f>'Metrics'!$A$1:$XFD$1048576</f>
  </numRef></val></ser></barChart></plotArea></chart>
</chartSpace>
""",
        },
    )

    with pytest.raises(docling_adapter._DoclingSourceFailure) as failure:
        docling_adapter._validate_office_package(path, SourceType.XLSX)

    assert failure.value.code == "UNSUPPORTED_XLSX_CHART"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("provider detail"), DependencyTimeoutError),
        (ImportError("late optional dependency"), DependencyUnavailableError),
    ],
)
async def test_documented_runtime_failures_are_normalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: type[Exception],
) -> None:
    source, context = prepared_source(tmp_path, "simple.docx")

    def convert(*_args: object) -> object:
        raise error

    monkeypatch.setattr(
        docling_adapter,
        "_load_docling_sdk",
        lambda: fake_sdk(convert),
    )

    with pytest.raises(expected):
        await docling_adapter.DoclingParser().parse(source, context)


@pytest.mark.asyncio
async def test_invalid_office_source_returns_explicit_failed_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path, "simple.docx")

    def convert(*_args: object) -> object:
        raise docling_adapter._DoclingSourceFailure("INVALID_OFFICE_SOURCE")

    monkeypatch.setattr(
        docling_adapter,
        "_load_docling_sdk",
        lambda: fake_sdk(convert),
    )

    parsed = await docling_adapter.DoclingParser().parse(source, context)

    assert parsed.parse_info.outcome is ParseOutcome.FAILED
    assert parsed.parse_info.failure_code == "INVALID_OFFICE_SOURCE"
    assert parsed.elements == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        object(),
        FakeDocument(
            [
                (
                    node(
                        "#/body",
                        "picture",
                        parent=None,
                        text="silently dropping this would be lossy",
                    ),
                    0,
                )
            ]
        ),
    ],
)
async def test_malformed_or_unsupported_docling_shape_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document: object,
) -> None:
    source, context = prepared_source(tmp_path, "simple.docx")
    monkeypatch.setattr(
        docling_adapter,
        "_load_docling_sdk",
        lambda: fake_sdk(lambda *_args: document),
    )

    with pytest.raises(ProviderResponseError):
        await docling_adapter.DoclingParser().parse(source, context)


@pytest.mark.asyncio
async def test_unexpected_provider_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path, "simple.docx")

    def convert(*_args: object) -> object:
        raise RuntimeError("unexpected Docling defect")

    monkeypatch.setattr(
        docling_adapter,
        "_load_docling_sdk",
        lambda: fake_sdk(convert),
    )

    with pytest.raises(RuntimeError, match="unexpected Docling defect"):
        await docling_adapter.DoclingParser().parse(source, context)


@pytest.mark.asyncio
async def test_unexpected_iteration_value_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path, "simple.docx")

    class DefectiveDocument:
        def iterate_items(self, **_kwargs: object) -> object:
            raise ValueError("unexpected iterator defect")

    monkeypatch.setattr(
        docling_adapter,
        "_load_docling_sdk",
        lambda: fake_sdk(lambda *_args: DefectiveDocument()),
    )

    with pytest.raises(ValueError, match="unexpected iterator defect"):
        await docling_adapter.DoclingParser().parse(source, context)


@pytest.mark.asyncio
async def test_adapter_rejects_pdf_and_context_mismatch_before_loading_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docx_source, docx_context = prepared_source(tmp_path, "simple.docx")
    pdf_source, pdf_context = prepared_source(tmp_path, "simple.pdf")
    monkeypatch.setattr(
        docling_adapter,
        "_load_docling_sdk",
        lambda: pytest.fail("invalid route must fail before loading Docling"),
    )

    with pytest.raises(InvariantViolationError):
        await docling_adapter.DoclingParser().parse(pdf_source, pdf_context)
    with pytest.raises(InvariantViolationError):
        await docling_adapter.DoclingParser().parse(docx_source, pdf_context)
    assert docx_context.source == docx_source
