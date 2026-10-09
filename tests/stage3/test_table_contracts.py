"""Canonical first-class TableData contract tests."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.ingestion.elements import (
    CanonicalElement,
    CanonicalElementMetadata,
    TableData,
)
from atlasrag.domain.ingestion.enums import ElementType
from atlasrag.domain.ingestion.source import derive_document_id
from atlasrag.services.ingestion.identity import build_element

DOCUMENT_ID = derive_document_id("tables/report.xlsx")
REVISION_ID = "rev_" + "c" * 64


def table_data(**overrides: Any) -> TableData:
    values: dict[str, object] = {
        "headers": ("Name", "Value"),
        "rows": (("Alpha", "1"), ("Beta", "2")),
        "caption": "Summary",
        "sheet_name": "Data",
        "source_anchor": SourceAnchor(sheet_name="Data", cell_range="A1:B3"),
    }
    values.update(overrides)
    return TableData.model_validate(values)


def table_element(
    *, data: TableData | None = None, content: str = "Name\tValue"
) -> CanonicalElement:
    return build_element(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        element_type=ElementType.TABLE,
        order=0,
        content=content,
        source_anchor=SourceAnchor(sheet_name="Data", cell_range="A1:B3"),
        metadata=CanonicalElementMetadata(),
        table_data=data or table_data(),
    )


def test_table_data_preserves_rectangular_structure_and_provenance() -> None:
    table = table_data()

    assert table.headers == ("Name", "Value")
    assert table.rows == (("Alpha", "1"), ("Beta", "2"))
    assert table.caption == "Summary"
    assert table.sheet_name == "Data"
    assert table.source_anchor == SourceAnchor(sheet_name="Data", cell_range="A1:B3")


def test_table_data_normalizes_cells_individually_without_flattening() -> None:
    table = table_data(
        headers=("Cafe\u0301", "Notes"),
        rows=(("Merged", ""), ("", "Line 1\r\nLine 2")),
    )

    assert table.headers == ("Café", "Notes")
    assert table.rows == (("Merged", ""), ("", "Line 1\nLine 2"))
    assert table.model_dump(mode="json")["rows"] == [
        ["Merged", ""],
        ["", "Line 1\nLine 2"],
    ]


def test_table_without_declared_headers_uses_equal_width_empty_headers() -> None:
    table = table_data(
        headers=("", "", ""),
        rows=(("first", "second", "third"),),
    )

    assert table.headers == ("", "", "")
    assert table.rows[0] == ("first", "second", "third")


@pytest.mark.parametrize(
    "overrides",
    [
        {"headers": ()},
        {"rows": ()},
        {"rows": (("only one",),)},
        {"rows": (("too", "many", "cells"),)},
    ],
)
def test_table_data_rejects_empty_or_nonrectangular_grids(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="table"):
        table_data(**overrides)


def test_table_data_rejects_unordered_rows_and_non_string_cells() -> None:
    with pytest.raises(ValidationError, match="list or tuple"):
        table_data(rows=({"Alpha", "1"},))
    with pytest.raises(ValidationError):
        table_data(rows=(("Alpha", 1),))


def test_table_sheet_name_must_match_source_anchor() -> None:
    with pytest.raises(ValidationError, match="sheet_name"):
        table_data(source_anchor=SourceAnchor(sheet_name="Other"))


def test_table_element_rejects_contradictory_structural_anchor() -> None:
    data = table_data(source_anchor=SourceAnchor(sheet_name="Data", cell_range="A1:B3"))

    with pytest.raises(ValidationError, match="source_anchor"):
        build_element(
            document_id=DOCUMENT_ID,
            revision_id=REVISION_ID,
            element_type=ElementType.TABLE,
            order=0,
            content="table",
            source_anchor=SourceAnchor(sheet_name="Data", cell_range="D1:E3"),
            table_data=data,
        )


def test_table_element_requires_table_data_and_non_table_forbids_it() -> None:
    table = table_element()
    assert table.table_data == table_data()

    with pytest.raises(ValidationError, match="TABLE.*table_data"):
        build_element(
            document_id=DOCUMENT_ID,
            revision_id=REVISION_ID,
            element_type=ElementType.TABLE,
            order=0,
            content="table",
        )
    with pytest.raises(ValidationError, match="non-TABLE.*table_data"):
        build_element(
            document_id=DOCUMENT_ID,
            revision_id=REVISION_ID,
            element_type=ElementType.PARAGRAPH,
            order=0,
            content="paragraph",
            table_data=table_data(),
        )


def test_table_data_changes_element_identity_even_when_rendered_text_matches() -> None:
    baseline = table_element()
    changed = table_element(data=table_data(rows=(("Alpha", "1"), ("Beta", "changed"))))

    assert baseline.content == changed.content
    assert baseline.element_id != changed.element_id


def test_table_element_json_round_trip_is_semantically_identical() -> None:
    element = table_element()

    restored = type(element).model_validate_json(element.model_dump_json())

    assert restored == element


def test_table_contracts_reject_provider_native_payloads() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        TableData.model_validate(
            {
                **table_data().model_dump(),
                "docling_table": {"internal_id": "random"},
            }
        )
