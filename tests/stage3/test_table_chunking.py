"""Deterministic FULL and ROW_GROUP table chunking tests."""

from __future__ import annotations

from typing import Any

import pytest

from atlasrag.domain.enums import SourceType
from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.ingestion.chunks import (
    ChunkType,
    TableProvenance,
    TableSliceMode,
    render_table_slice,
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
from atlasrag.services.ingestion.chunking import ChunkingProfile, build_chunks
from atlasrag.services.ingestion.identity import build_element

DOCUMENT_ID = derive_document_id("sheets/report.xlsx")
REVISION_ID = "rev_" + "c" * 64
ANCHOR = SourceAnchor(sheet_name="Metrics", cell_range="A1:B5")


def table_data(**overrides: Any) -> TableData:
    values: dict[str, object] = {
        "headers": ("Name", "Value"),
        "rows": (("Alpha", "1"), ("Beta", "2"), ("Gamma", "3"), ("Delta", "4")),
        "caption": "Quarterly summary",
        "sheet_name": "Metrics",
        "source_anchor": ANCHOR,
    }
    values.update(overrides)
    return TableData.model_validate(values)


def table_element(data: TableData, *, order: int = 0) -> CanonicalElement:
    return build_element(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        element_type=ElementType.TABLE,
        order=order,
        content=render_table_data(data),
        section_path=("Metrics",),
        source_anchor=data.source_anchor,
        table_data=data,
    )


def text_element(order: int, content: str) -> CanonicalElement:
    return build_element(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        element_type=ElementType.PARAGRAPH,
        order=order,
        content=content,
        section_path=("Metrics",),
    )


def document(elements: tuple[CanonicalElement, ...]) -> DocumentContainer:
    return DocumentContainer(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        source=CanonicalSource(
            relative_source_path="sheets/report.xlsx",
            file_name="report.xlsx",
            source_type=SourceType.XLSX,
        ),
        metadata=DocumentMetadata(title="Report", sheet_names=("Metrics",)),
        parse_info=ParseInfo(
            outcome=ParseOutcome.SUCCESS,
            reliability=ParseReliability.RELIABLE,
            parser_name="test-office",
            parser_version="1",
        ),
        elements=elements,
    )


def profile(table_limit: int) -> ChunkingProfile:
    return ChunkingProfile(
        parent_max_chars=100,
        child_max_chars=40,
        table_slice_max_chars=table_limit,
    )


def test_small_table_emits_one_full_slice_in_source_order() -> None:
    data = table_data()
    parsed = document(
        (
            text_element(0, "Before table."),
            table_element(data, order=1),
            text_element(2, "After table."),
        )
    )

    chunks = build_chunks(parsed, profile(len(render_table_data(data))))
    tables = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TABLE]

    assert len(tables) == 1
    table = tables[0]
    assert table.table_slice is not None
    assert table.table_slice.mode is TableSliceMode.FULL
    assert table.table_slice.rows == data.rows
    assert table.raw_content == render_table_data(data)
    assert chunks.index(table) > 0
    assert any(
        chunk.chunk_type is ChunkType.TEXT_PARENT
        for chunk in chunks[: chunks.index(table)]
    )
    assert any(
        chunk.chunk_type is ChunkType.TEXT_PARENT
        for chunk in chunks[chunks.index(table) + 1 :]
    )


def test_large_table_emits_ordered_row_groups_with_repeated_headers() -> None:
    data = table_data()
    one_row = TableData(
        headers=data.headers,
        rows=(data.rows[0],),
        caption=data.caption,
        sheet_name=data.sheet_name,
        source_anchor=data.source_anchor,
    )
    limit = len(render_table_data(one_row))

    chunks = build_chunks(document((table_element(data),)), profile(limit))
    tables = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TABLE]

    assert len(tables) == len(data.rows)
    assert all(chunk.table_slice is not None for chunk in tables)
    assert all(
        chunk.table_slice.mode is TableSliceMode.ROW_GROUP  # type: ignore[union-attr]
        for chunk in tables
    )
    assert all(chunk.table_slice.headers == data.headers for chunk in tables)  # type: ignore[union-attr]
    assert (
        tuple(
            row
            for chunk in tables
            for row in chunk.table_slice.rows  # type: ignore[union-attr]
        )
        == data.rows
    )
    assert [
        (chunk.table_slice.row_start, chunk.table_slice.row_end)  # type: ignore[union-attr]
        for chunk in tables
    ] == [(0, 1), (1, 2), (2, 3), (3, 4)]
    assert all(len(chunk.raw_content) <= limit for chunk in tables)


def test_table_slices_preserve_exact_element_and_source_lineage() -> None:
    data = table_data()
    source = table_element(data, order=3)
    single_row_data = TableData(
        headers=data.headers,
        rows=(data.rows[0],),
        caption=data.caption,
        sheet_name=data.sheet_name,
        source_anchor=data.source_anchor,
    )

    chunks = build_chunks(
        document(
            (
                text_element(0, "one"),
                text_element(1, "two"),
                text_element(2, "three"),
                source,
            )
        ),
        profile(len(render_table_data(single_row_data))),
    )
    tables = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TABLE]

    for chunk in tables:
        assert isinstance(chunk.provenance, TableProvenance)
        assert chunk.provenance.source_element_id == source.element_id
        assert chunk.provenance.source_element_order == source.order
        assert chunk.table_slice is not None
        assert chunk.table_slice.source_element_id == source.element_id
        assert chunk.table_slice.caption == data.caption
        assert chunk.table_slice.sheet_name == data.sheet_name
        assert chunk.table_slice.source_anchor == data.source_anchor
        assert chunk.provenance.sheet_name == data.sheet_name
        assert chunk.raw_content == render_table_slice(chunk.table_slice)


@pytest.mark.parametrize(
    "data",
    [
        table_data(headers=("H" * 80, "Value"), rows=(("a", "1"),)),
        table_data(rows=(("cell" * 30, "1"),)),
        table_data(
            headers=("A", "B", "C", "D"),
            rows=(("123456", "123456", "123456", "123456"),),
            caption=None,
        ),
    ],
    ids=("oversized-header", "oversized-cell", "oversized-row"),
)
def test_table_chunking_fails_closed_when_a_lossless_row_cannot_fit(
    data: TableData,
) -> None:
    with pytest.raises(ValueError, match="table|header|cell|row"):
        build_chunks(document((table_element(data),)), profile(42))


def test_table_row_ranges_cover_every_source_row_exactly_once() -> None:
    rows = tuple((f"row-{index}", str(index)) for index in range(20))
    data = table_data(rows=rows, caption=None)
    one_row = TableData(
        headers=data.headers,
        rows=(data.rows[0],),
        sheet_name=data.sheet_name,
        source_anchor=data.source_anchor,
    )
    limit = len(render_table_data(one_row)) + 12

    chunks = build_chunks(document((table_element(data),)), profile(limit))
    tables = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TABLE]

    cursor = 0
    recovered: list[tuple[str, ...]] = []
    for chunk in tables:
        assert chunk.table_slice is not None
        assert chunk.table_slice.row_start == cursor
        cursor = chunk.table_slice.row_end
        recovered.extend(chunk.table_slice.rows)
    assert cursor == len(rows)
    assert tuple(recovered) == rows
