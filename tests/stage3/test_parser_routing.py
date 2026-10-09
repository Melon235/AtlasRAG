"""Exact Stage 3 parser protocol and routing boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.source import CandidateBuildContext, SourceFile
from atlasrag.providers.parsing.base import DocumentParser
from atlasrag.providers.parsing.routing import ParserRouter


class StubParser:
    async def parse(
        self, source: SourceFile, context: CandidateBuildContext
    ) -> DocumentContainer:
        raise AssertionError("routing tests must not invoke parsers")


def source_file(root: Path, relative_path: str) -> SourceFile:
    path = root / relative_path
    path.write_bytes(b"fixture")
    return SourceFile.from_knowledge_directory(root, relative_path)


def test_document_parser_is_a_structural_async_protocol() -> None:
    assert isinstance(StubParser(), DocumentParser)


@pytest.mark.parametrize(
    ("relative_path", "selected"),
    [
        ("file.PDF", "mineru"),
        ("file.DocX", "docling"),
        ("file.XLSX", "docling"),
        ("file.Md", "markdown"),
        ("file.TxT", "text"),
    ],
)
def test_router_selects_only_the_frozen_parser_for_each_source_type(
    tmp_path: Path, relative_path: str, selected: str
) -> None:
    parsers = {
        "mineru": StubParser(),
        "docling": StubParser(),
        "markdown": StubParser(),
        "text": StubParser(),
    }
    router = ParserRouter(
        mineru=parsers["mineru"],
        docling=parsers["docling"],
        markdown=parsers["markdown"],
        text=parsers["text"],
    )

    actual = router.parser_for(source_file(tmp_path, relative_path))

    assert actual is parsers[selected]


def test_router_revalidates_constructed_source_instead_of_falling_back(
    tmp_path: Path,
) -> None:
    valid = source_file(tmp_path, "file.txt")
    forged = SourceFile.model_construct(**{**valid.model_dump(), "source_type": "HTML"})
    fallback = StubParser()
    router = ParserRouter(
        mineru=StubParser(),
        docling=StubParser(),
        markdown=StubParser(),
        text=fallback,
    )

    with pytest.raises(ValueError):
        router.parser_for(forged)


def test_router_is_immutable_and_rejects_non_parser_dependencies() -> None:
    with pytest.raises(TypeError, match="DocumentParser"):
        ParserRouter(
            mineru=object(),  # type: ignore[arg-type]
            docling=StubParser(),
            markdown=StubParser(),
            text=StubParser(),
        )
