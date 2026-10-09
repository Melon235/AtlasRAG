"""Fail-closed normalization of the frozen real MinerU 4 output fixture."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest

from atlasrag.domain.errors import ProviderResponseError
from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.ingestion.enums import ElementType, ListKind, ParseOutcome
from atlasrag.domain.ingestion.source import CandidateBuildContext, SourceFile
from atlasrag.providers.parsing.mineru_normalizer import normalize_mineru_output
from atlasrag.services.ingestion.identity import build_candidate_context

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MINERU_FIXTURE = FIXTURES / "parser_outputs" / "mineru" / "v4" / "simple.json"
PDF_FIXTURE = FIXTURES / "stage3" / "simple.pdf"
PIPELINE_FINGERPRINT = "d" * 64


def output_payload() -> dict[str, Any]:
    payload = json.loads(MINERU_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def prepared_pdf(root: Path) -> tuple[SourceFile, CandidateBuildContext]:
    path = root / "document.pdf"
    path.write_bytes(PDF_FIXTURE.read_bytes())
    source = SourceFile.from_knowledge_directory(root, path.name)
    return source, build_candidate_context(source, PIPELINE_FINGERPRINT)


def table_body(payload: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], payload["pages"][0]["blocks"][-1]["content"][0])


def test_real_mineru_v4_fixture_normalizes_in_stable_source_order(
    tmp_path: Path,
) -> None:
    source, context = prepared_pdf(tmp_path)

    parsed = normalize_mineru_output(output_payload(), source, context)

    assert [element.element_type for element in parsed.elements] == [
        ElementType.HEADING,
        ElementType.PARAGRAPH,
        ElementType.LIST,
        ElementType.LIST,
        ElementType.LIST,
        ElementType.CODE,
        ElementType.TABLE,
    ]
    assert [element.order for element in parsed.elements] == list(range(7))
    assert parsed.elements[0].content == "AtlasRAG PDF Fixture"
    assert parsed.elements[0].metadata.heading_level == 1
    assert parsed.elements[1].content == (
        "Canonical paragraph from MinerU flash. "
        "This fixture preserves stable source order."
    )
    assert [element.content for element in parsed.elements[2:5]] == [
        "a. Alpha item",
        "b. Beta item",
        "c. Gamma item",
    ]
    assert all(
        element.metadata.list_kind is ListKind.ORDERED
        and element.metadata.list_depth == 0
        for element in parsed.elements[2:5]
    )
    assert parsed.elements[5].content == (
        "def parse_pdf():\n"
        '    items = ["alpha", "beta"]\n'
        "    for item in items:\n"
        "        print(item)\n"
        "    return items"
    )
    assert parsed.elements[5].metadata.code_language == "python"
    table = parsed.elements[6].table_data
    assert table is not None
    assert table.headers == ("", "")
    assert table.rows == (
        ("Name", "Value"),
        ("alpha", "1"),
        ("beta", "2"),
    )
    assert parsed.metadata.title == "AtlasRAG PDF Fixture"
    assert parsed.metadata.page_count == 1
    assert parsed.parse_info.outcome is ParseOutcome.SUCCESS
    assert parsed.parse_info.parser_name == "mineru"
    assert parsed.parse_info.parser_version == "4.0.11"
    assert all(
        element.section_path == ("AtlasRAG PDF Fixture",) for element in parsed.elements
    )
    assert all(
        element.source_anchor
        == SourceAnchor(page_number=1, heading="AtlasRAG PDF Fixture")
        for element in parsed.elements
    )


def test_provider_declared_headers_and_merged_cells_are_preserved(
    tmp_path: Path,
) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    table_body(payload)["content"] = (
        "<table><thead><tr><th>Name</th><th>Value</th></tr></thead>"
        "<tbody><tr><td rowspan='2'>alpha</td><td>1</td></tr>"
        "<tr><td>2</td></tr></tbody></table>"
    )

    parsed = normalize_mineru_output(payload, source, context)

    table = parsed.elements[-1].table_data
    assert table is not None
    assert table.headers == ("Name", "Value")
    assert table.rows == (("alpha", "1"), ("", "2"))


def test_fully_rowspan_covered_logical_row_is_preserved(
    tmp_path: Path,
) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    table_body(payload)["content"] = (
        "<table><tbody>"
        "<tr><td rowspan='2'>alpha</td><td rowspan='2'>1</td></tr>"
        "<tr></tr>"
        "<tr><td>beta</td><td>2</td></tr>"
        "</tbody></table>"
    )

    parsed = normalize_mineru_output(payload, source, context)

    table = parsed.elements[-1].table_data
    assert table is not None
    assert table.headers == ("", "")
    assert table.rows == (("alpha", "1"), ("", ""), ("beta", "2"))


def test_fully_covered_multi_row_header_fails_closed(tmp_path: Path) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    table_body(payload)["content"] = (
        "<table><thead>"
        "<tr><th rowspan='2'>Name</th><th rowspan='2'>Value</th></tr>"
        "<tr></tr>"
        "</thead><tbody><tr><td>alpha</td><td>1</td></tr></tbody></table>"
    )

    with pytest.raises(ProviderResponseError):
        normalize_mineru_output(payload, source, context)


def test_incidental_provider_facts_do_not_change_canonical_output_or_ids(
    tmp_path: Path,
) -> None:
    source, context = prepared_pdf(tmp_path)
    first = output_payload()
    second = copy.deepcopy(first)
    first["extensions"]["runtime"] = {
        "random_id": "run-one",
        "temporary_path": "/tmp/one/input.pdf",
    }
    second["extensions"]["runtime"] = {
        "random_id": "run-two",
        "temporary_path": "/tmp/two/input.pdf",
    }
    first["metadata"]["document"]["created_at"] = "2026-01-01T00:00:00Z"
    second["metadata"]["document"]["created_at"] = "2027-02-02T00:00:00Z"
    first["metadata"]["document"]["creator_application"] = "/tmp/random-one"
    second["metadata"]["document"]["creator_application"] = "/tmp/random-two"

    first_parsed = normalize_mineru_output(first, source, context)
    second_parsed = normalize_mineru_output(second, source, context)

    assert first_parsed == second_parsed
    assert [element.element_id for element in first_parsed.elements] == [
        element.element_id for element in second_parsed.elements
    ]
    serialized = first_parsed.model_dump_json()
    assert "random_id" not in serialized
    assert "temporary_path" not in serialized
    assert "docvortex_layout" not in serialized
    assert '"bbox"' not in serialized


@pytest.mark.parametrize(
    ("block_index", "anchor"),
    [
        (0, "provider-heading-anchor"),
        (1, {"target": "provider-text-anchor"}),
    ],
)
def test_non_null_provider_anchor_is_rejected_instead_of_discarded(
    tmp_path: Path, block_index: int, anchor: object
) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    payload["pages"][0]["blocks"][block_index]["anchor"] = anchor

    with pytest.raises(ProviderResponseError):
        normalize_mineru_output(payload, source, context)


def test_non_null_nested_provider_semantics_are_rejected(
    tmp_path: Path,
) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    list_block = payload["pages"][0]["blocks"][2]
    list_block["content"][0]["continues_prev"] = True

    with pytest.raises(ProviderResponseError):
        normalize_mineru_output(payload, source, context)


@pytest.mark.parametrize("level", [0, 1, 7])
def test_paragraph_title_requires_mineru_heading_level_two_through_six(
    tmp_path: Path, level: int
) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    heading = payload["pages"][0]["blocks"][0]
    heading["type"] = "paragraph_title"
    heading["level"] = level

    with pytest.raises(ProviderResponseError):
        normalize_mineru_output(payload, source, context)


@pytest.mark.parametrize(
    "indexes",
    [
        (4, 3, 5),
        (2, 4, 5),
        (3, 4, 6),
    ],
)
def test_nested_block_indexes_must_preserve_global_source_order(
    tmp_path: Path, indexes: tuple[int, int, int]
) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    children = payload["pages"][0]["blocks"][2]["content"]
    for child, index in zip(children, indexes, strict=True):
        child["index"] = index

    with pytest.raises(ProviderResponseError):
        normalize_mineru_output(payload, source, context)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cell_merge", [1]),
        ("image_path", "/tmp/provider-table.png"),
        ("image_url", "https://provider.invalid/table.png"),
    ],
)
def test_table_side_channels_are_not_silently_discarded(
    tmp_path: Path, field: str, value: object
) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    if field == "cell_merge":
        payload["pages"][0]["blocks"][-1][field] = value
    else:
        table_body(payload)[field] = value

    with pytest.raises(ProviderResponseError):
        normalize_mineru_output(payload, source, context)


@pytest.mark.parametrize(
    "missing_key",
    ["schema", "schema_version", "metadata", "extensions", "pages"],
)
def test_normalizer_rejects_missing_required_top_level_keys(
    tmp_path: Path, missing_key: str
) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    payload.pop(missing_key)

    with pytest.raises(ProviderResponseError):
        normalize_mineru_output(payload, source, context)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("schema", "docvortex.legacy"),
        ("schema_version", "1.0"),
        ("is_full_document", False),
    ],
)
def test_normalizer_rejects_unsupported_protocol_identity(
    tmp_path: Path, field: str, bad_value: object
) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    payload[field] = bad_value

    with pytest.raises(ProviderResponseError):
        normalize_mineru_output(payload, source, context)


@pytest.mark.parametrize(
    ("path", "bad_value"),
    [
        (("metadata", "producer", "version"), "4.0.12"),
        (("metadata", "producer", "name"), "other"),
        (("metadata", "file_suffix"), "docx"),
        (("extensions", "mineru", "tier"), "standard"),
        (("extensions", "mineru", "parse_mode"), "unknown"),
    ],
)
def test_normalizer_rejects_wrong_provider_contract(
    tmp_path: Path, path: tuple[str, ...], bad_value: object
) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    target: dict[str, Any] = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = bad_value

    with pytest.raises(ProviderResponseError):
        normalize_mineru_output(payload, source, context)


@pytest.mark.parametrize(
    "bad_html",
    [
        "not a table",
        "<table></table>",
        "<table><tr><th>A</th><td>B</td></tr><tr><td>1</td><td>2</td></tr></table>",
        "<table><tr><td rowspan='0'>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table>",
    ],
)
def test_normalizer_rejects_malformed_or_lossy_table_shapes(
    tmp_path: Path, bad_html: str
) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    table_body(payload)["content"] = bad_html

    with pytest.raises(ProviderResponseError):
        normalize_mineru_output(payload, source, context)


def test_table_html_has_a_hard_input_budget(tmp_path: Path) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    table_body(payload)["content"] = (
        "<table><tr><td>A</td></tr><tr><td>B</td></tr></table>" + " " * 1_048_577
    )

    with pytest.raises(ProviderResponseError):
        normalize_mineru_output(payload, source, context)


def test_table_row_count_has_a_hard_budget(tmp_path: Path) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    table_body(payload)["content"] = (
        "<table>" + "<tr><td>x</td></tr>" * 4_097 + "</table>"
    )

    with pytest.raises(ProviderResponseError):
        normalize_mineru_output(payload, source, context)


def test_table_column_span_has_a_hard_budget(tmp_path: Path) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    table_body(payload)["content"] = (
        "<table><tr><td colspan='1025'>a</td></tr>"
        "<tr><td colspan='1025'>b</td></tr></table>"
    )

    with pytest.raises(ProviderResponseError):
        normalize_mineru_output(payload, source, context)


def test_table_logical_cell_count_has_a_hard_budget(tmp_path: Path) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    table_body(payload)["content"] = (
        "<table>" + "<tr><td colspan='1024'>x</td></tr>" * 257 + "</table>"
    )

    with pytest.raises(ProviderResponseError):
        normalize_mineru_output(payload, source, context)


def test_table_span_integer_is_bounded_before_conversion(tmp_path: Path) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    huge_span = "9" * 5_000
    table_body(payload)["content"] = (
        f"<table><tr><td colspan='{huge_span}'>a</td></tr><tr><td>b</td></tr></table>"
    )

    with pytest.raises(ProviderResponseError):
        normalize_mineru_output(payload, source, context)


def test_normalizer_rejects_unobserved_block_shape_without_dropping_it(
    tmp_path: Path,
) -> None:
    source, context = prepared_pdf(tmp_path)
    payload = output_payload()
    payload["pages"][0]["blocks"].insert(
        1,
        {
            "type": "image",
            "index": 1,
            "bbox": [0.1, 0.1, 0.2, 0.2],
            "content": [],
        },
    )

    with pytest.raises(ProviderResponseError):
        normalize_mineru_output(payload, source, context)
