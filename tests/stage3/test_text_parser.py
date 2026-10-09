"""AtlasRAG native UTF-8 text parser tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from atlasrag.domain.errors import InvariantViolationError
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.enums import ElementType, ParseOutcome, ParseReliability
from atlasrag.domain.ingestion.source import CandidateBuildContext, SourceFile
from atlasrag.providers.parsing.text import TextParser
from atlasrag.services.ingestion.identity import build_candidate_context

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stage3"
PIPELINE_FINGERPRINT = "f" * 64


def prepared_source(
    root: Path, data: bytes, name: str = "document.txt"
) -> tuple[SourceFile, CandidateBuildContext]:
    (root / name).write_bytes(data)
    source = SourceFile.from_knowledge_directory(root, name)
    return source, build_candidate_context(source, PIPELINE_FINGERPRINT)


@pytest.mark.asyncio
async def test_text_parser_emits_deterministic_paragraph_elements(
    tmp_path: Path,
) -> None:
    payload = (FIXTURES / "simple.txt").read_bytes()
    source, context = prepared_source(tmp_path, payload)
    parser = TextParser()

    first = await parser.parse(source, context)
    second = await parser.parse(source, context)

    assert first == second
    assert [element.element_type for element in first.elements] == [
        ElementType.PARAGRAPH,
        ElementType.PARAGRAPH,
    ]
    assert first.elements[0].content == (
        "AtlasRAG keeps canonical text.\n第二行保留 Unicode。"
    )
    assert first.elements[1].content == "Second paragraph keeps punctuation: Yes!"
    assert [element.order for element in first.elements] == [0, 1]
    assert first.document_id == context.document_id
    assert first.revision_id == context.revision_id
    assert first.source.relative_source_path == "document.txt"
    assert first.parse_info.outcome is ParseOutcome.SUCCESS
    assert first.parse_info.reliability is ParseReliability.RELIABLE
    assert first.parse_info.parser_name == "atlasrag-text"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"\xef\xbb\xbfBOM text", "BOM text"),
        (b"first\r\nsecond\rthird", "first\nsecond\nthird"),
        ("Unicode 🧭".encode(), "Unicode 🧭"),
    ],
)
async def test_text_parser_handles_bom_unicode_and_newlines(
    tmp_path: Path, payload: bytes, expected: str
) -> None:
    source, context = prepared_source(tmp_path, payload)

    parsed = await TextParser().parse(source, context)

    assert parsed.elements[0].content == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "failure_code"),
    [
        (b"", "EMPTY_SOURCE"),
        (b" \r\n\t", "EMPTY_SOURCE"),
        (b"invalid-\xff", "INVALID_UTF8"),
        (b"unsafe-\x00-text", "INVALID_TEXT_CONTROL"),
    ],
)
async def test_text_parser_returns_explicit_failed_business_result(
    tmp_path: Path, payload: bytes, failure_code: str
) -> None:
    source, context = prepared_source(tmp_path, payload)

    parsed = await TextParser().parse(source, context)

    assert parsed.elements == ()
    assert parsed.parse_info.outcome is ParseOutcome.FAILED
    assert parsed.parse_info.reliability is ParseReliability.UNAVAILABLE
    assert parsed.parse_info.failure_code == failure_code


@pytest.mark.asyncio
async def test_text_parser_rejects_wrong_route_or_candidate_context(
    tmp_path: Path,
) -> None:
    text_source, text_context = prepared_source(tmp_path, b"text")
    (tmp_path / "document.md").write_text("markdown", encoding="utf-8")
    markdown_source = SourceFile.from_knowledge_directory(tmp_path, "document.md")
    markdown_context = build_candidate_context(markdown_source, PIPELINE_FINGERPRINT)
    parser = TextParser()

    with pytest.raises(InvariantViolationError):
        await parser.parse(markdown_source, markdown_context)
    with pytest.raises(InvariantViolationError):
        await parser.parse(text_source, markdown_context)


@pytest.mark.asyncio
async def test_text_parser_output_is_stable_json(tmp_path: Path) -> None:
    payload = b"first\n\nsecond"
    source, context = prepared_source(tmp_path, payload)

    parsed = await TextParser().parse(source, context)
    restored = DocumentContainer.model_validate_json(parsed.model_dump_json())

    assert restored == parsed
    assert context.content_hash == hashlib.sha256(payload).hexdigest()
