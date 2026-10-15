"""Deterministic dependency-injected parser routing."""

from __future__ import annotations

from dataclasses import dataclass

from atlasrag.domain.enums import SourceType
from atlasrag.domain.errors import ConfigurationError
from atlasrag.domain.ingestion.source import SourceFile
from atlasrag.providers.parsing.base import DocumentParser


@dataclass(frozen=True, slots=True)
class ParserRouter:
    """Select exactly one injected provider for each frozen source type."""

    mineru: DocumentParser
    docling: DocumentParser
    markdown: DocumentParser
    text: DocumentParser

    def __post_init__(self) -> None:
        for parser in (self.mineru, self.docling, self.markdown, self.text):
            if not isinstance(parser, DocumentParser):
                raise TypeError("parser dependency must implement DocumentParser")

    def parser_for(self, source: SourceFile) -> DocumentParser:
        """Return the frozen provider route without fallback or guessing."""
        if not isinstance(source, SourceFile):
            raise TypeError("source must be a SourceFile")
        source = SourceFile.model_validate(source)
        if source.source_type is SourceType.PDF:
            return self.mineru
        if source.source_type in {SourceType.DOCX, SourceType.XLSX}:
            return self.docling
        if source.source_type is SourceType.MD:
            return self.markdown
        if source.source_type is SourceType.TXT:
            return self.text
        raise ConfigurationError
