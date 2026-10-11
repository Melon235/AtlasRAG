"""Pure deterministic document analysis and Stage 3 chunk routing."""

from __future__ import annotations

from dataclasses import dataclass

from atlasrag.domain.ingestion.chunks import ChunkingStrategy, ChunkType
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.elements import CanonicalElement
from atlasrag.domain.ingestion.enums import ElementType


@dataclass(frozen=True, slots=True)
class TextRun:
    """One contiguous, same-section sequence of canonical text elements."""

    elements: tuple[CanonicalElement, ...]

    def __post_init__(self) -> None:
        if not self.elements:
            raise ValueError("text run must be nonempty")
        first = self.elements[0]
        if first.element_type is ElementType.TABLE:
            raise ValueError("text run must not contain TABLE elements")
        previous_order = -1
        for element in self.elements:
            if element.element_type is ElementType.TABLE:
                raise ValueError("text run must not contain TABLE elements")
            if element.document_id != first.document_id:
                raise ValueError("text run elements must share document_id")
            if element.revision_id != first.revision_id:
                raise ValueError("text run elements must share revision_id")
            if element.section_path != first.section_path:
                raise ValueError("text run elements must share section_path")
            if element.order <= previous_order:
                raise ValueError("text run elements must remain in source order")
            previous_order = element.order

    @property
    def section_path(self) -> tuple[str, ...]:
        """Return the common canonical section path."""
        return self.elements[0].section_path


@dataclass(frozen=True, slots=True)
class TablePosition:
    """One TABLE element retained at its exact source position."""

    element: CanonicalElement

    def __post_init__(self) -> None:
        if self.element.element_type is not ElementType.TABLE:
            raise ValueError("table position requires a TABLE element")


AnalysisItem = TextRun | TablePosition


@dataclass(frozen=True, slots=True)
class DocumentAnalysis:
    """Immutable ordered analysis of one validated canonical document."""

    document_id: str
    revision_id: str
    items: tuple[AnalysisItem, ...]

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("document analysis must be nonempty")
        previous_order = -1
        for item in self.items:
            elements = item.elements if isinstance(item, TextRun) else (item.element,)
            for element in elements:
                if element.document_id != self.document_id:
                    raise ValueError("analysis item document_id must match document")
                if element.revision_id != self.revision_id:
                    raise ValueError("analysis item revision_id must match document")
                if element.order <= previous_order:
                    raise ValueError("analysis items must remain in source order")
                previous_order = element.order


class DocumentAnalyzer:
    """Identify ordered same-section text runs and exact table positions."""

    def analyze(self, document: DocumentContainer) -> DocumentAnalysis:
        """Return a deterministic analysis without I/O or external dependencies."""
        if not isinstance(document, DocumentContainer):
            raise TypeError("document must be a DocumentContainer")
        validated = DocumentContainer.model_validate(document)
        items: list[AnalysisItem] = []
        pending: list[CanonicalElement] = []
        pending_section: tuple[str, ...] | None = None

        def flush_text_run() -> None:
            nonlocal pending, pending_section
            if pending:
                items.append(TextRun(elements=tuple(pending)))
                pending = []
                pending_section = None

        for element in validated.elements:
            if element.element_type is ElementType.TABLE:
                flush_text_run()
                items.append(TablePosition(element=element))
                continue
            if pending and element.section_path != pending_section:
                flush_text_run()
            if not pending:
                pending_section = element.section_path
            pending.append(element)
        flush_text_run()
        return DocumentAnalysis(
            document_id=validated.document_id,
            revision_id=validated.revision_id,
            items=tuple(items),
        )


class ChunkRouter:
    """Pure fixed Stage 3 mapping from canonical kind to chunking strategy."""

    @property
    def parent_strategy(self) -> ChunkingStrategy:
        return ChunkingStrategy.STRUCTURE

    @property
    def child_strategy(self) -> ChunkingStrategy:
        return ChunkingStrategy.RECURSIVE

    @property
    def table_strategy(self) -> ChunkingStrategy:
        return ChunkingStrategy.TABLE

    def route(self, target: CanonicalElement | ChunkType) -> ChunkingStrategy:
        """Select one of STRUCTURE, RECURSIVE, or TABLE for a typed target."""
        if isinstance(target, CanonicalElement):
            return (
                self.table_strategy
                if target.element_type is ElementType.TABLE
                else self.parent_strategy
            )
        if not isinstance(target, ChunkType):
            raise TypeError("target must be a CanonicalElement or ChunkType")
        if target is ChunkType.TEXT_PARENT:
            return self.parent_strategy
        if target is ChunkType.TEXT_CHILD:
            return self.child_strategy
        return self.table_strategy


def analyze_document(document: DocumentContainer) -> DocumentAnalysis:
    """Functional convenience wrapper for deterministic document analysis."""
    return DocumentAnalyzer().analyze(document)
