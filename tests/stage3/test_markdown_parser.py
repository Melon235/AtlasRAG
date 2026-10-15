"""Deterministic AtlasRAG native Markdown subset parser tests."""

from __future__ import annotations

import builtins
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.ingestion.enums import (
    ElementType,
    ListKind,
    ParseOutcome,
    ParseReliability,
)
from atlasrag.domain.ingestion.source import CandidateBuildContext, SourceFile
from atlasrag.providers.parsing.markdown import MarkdownParser
from atlasrag.services.ingestion.identity import build_candidate_context

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stage3"
PIPELINE_FINGERPRINT = "e" * 64


def prepared_markdown(
    root: Path, payload: bytes
) -> tuple[SourceFile, CandidateBuildContext]:
    (root / "document.md").write_bytes(payload)
    source = SourceFile.from_knowledge_directory(root, "document.md")
    return source, build_candidate_context(source, PIPELINE_FINGERPRINT)


@pytest.mark.asyncio
async def test_markdown_parser_covers_frozen_subset_in_source_order(
    tmp_path: Path,
) -> None:
    payload = (FIXTURES / "simple.md").read_bytes()
    source, context = prepared_markdown(tmp_path, payload)

    parsed = await MarkdownParser().parse(source, context)

    assert [element.element_type for element in parsed.elements] == [
        ElementType.HEADING,
        ElementType.PARAGRAPH,
        ElementType.HEADING,
        ElementType.LIST,
        ElementType.LIST,
        ElementType.LIST,
        ElementType.CODE,
        ElementType.TABLE,
        ElementType.PARAGRAPH,
    ]
    assert [element.order for element in parsed.elements] == list(range(9))
    assert parsed.elements[0].content == "AtlasRAG 指南"
    assert parsed.elements[0].metadata.heading_level == 1
    assert parsed.elements[0].section_path == ("AtlasRAG 指南",)
    assert parsed.elements[1].content == (
        "Intro keeps **inline syntax** and punctuation.\nSecond paragraph line."
    )
    assert parsed.elements[2].section_path == ("AtlasRAG 指南", "Lists")
    assert parsed.elements[3].metadata.list_kind is ListKind.UNORDERED
    assert parsed.elements[4].metadata.list_kind is ListKind.ORDERED
    assert parsed.elements[5].metadata.list_depth == 1
    assert parsed.elements[6].content == '  value = "保留  "\n'
    assert parsed.elements[6].metadata.code_language == "python"
    table = parsed.elements[7].table_data
    assert table is not None
    assert table.headers == ("Name", "Value")
    assert table.rows == (("Café", "1"), ("数据", "2"))
    assert parsed.elements[7].source_anchor == SourceAnchor(heading="Lists")
    assert parsed.elements[8].content == (
        "> Unsupported quote remains paragraph content."
    )
    assert parsed.metadata.title == "AtlasRAG 指南"
    assert parsed.parse_info.outcome is ParseOutcome.SUCCESS
    assert parsed.parse_info.reliability is ParseReliability.RELIABLE
    assert parsed.parse_info.parser_name == "atlasrag-markdown"


@pytest.mark.asyncio
async def test_markdown_heading_stack_is_deterministic_with_skipped_levels(
    tmp_path: Path,
) -> None:
    payload = b"# Top\n\n### Deep\n\ntext\n\n## Sibling\n"
    source, context = prepared_markdown(tmp_path, payload)

    parsed = await MarkdownParser().parse(source, context)

    assert [element.section_path for element in parsed.elements] == [
        ("Top",),
        ("Top", "Deep"),
        ("Top", "Deep"),
        ("Top", "Sibling"),
    ]


@pytest.mark.asyncio
async def test_markdown_pipe_cells_support_escaped_delimiter(tmp_path: Path) -> None:
    payload = b"| Key | Value |\n| --- | --- |\n| a\\|b | line |\n"
    source, context = prepared_markdown(tmp_path, payload)

    parsed = await MarkdownParser().parse(source, context)

    table = parsed.elements[0].table_data
    assert table is not None
    assert table.rows == (("a|b", "line"),)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "failure_code"),
    [
        (b"", "EMPTY_SOURCE"),
        (b"invalid-\xff", "INVALID_UTF8"),
        (b"```python\nprint('x')\n", "UNCLOSED_CODE_FENCE"),
        (b"```python\n```\n", "EMPTY_CODE_BLOCK"),
        (
            b"| A | B |\n| --- | --- |\n| only-one |\n",
            "INVALID_TABLE",
        ),
    ],
)
async def test_markdown_parser_returns_explicit_failed_result_for_bad_source(
    tmp_path: Path, payload: bytes, failure_code: str
) -> None:
    source, context = prepared_markdown(tmp_path, payload)

    parsed = await MarkdownParser().parse(source, context)

    assert parsed.elements == ()
    assert parsed.parse_info.outcome is ParseOutcome.FAILED
    assert parsed.parse_info.failure_code == failure_code


@pytest.mark.asyncio
async def test_native_parsers_do_not_import_optional_parser_sdks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_markdown(tmp_path, b"# Native\n\ntext")
    original_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> object:
        if name.partition(".")[0] in {"docling", "mineru"}:
            raise AssertionError(f"native parser imported optional SDK: {name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    parsed = await MarkdownParser().parse(source, context)

    assert parsed.elements
    assert "docling" not in sys.modules
    assert "mineru" not in sys.modules


@pytest.mark.asyncio
async def test_markdown_parse_is_semantically_deterministic(tmp_path: Path) -> None:
    payload = (FIXTURES / "simple.md").read_bytes()
    source, context = prepared_markdown(tmp_path, payload)
    parser = MarkdownParser()

    first = await parser.parse(source, context)
    second = await parser.parse(source, context)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
