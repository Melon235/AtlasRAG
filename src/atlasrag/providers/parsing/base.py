"""Minimal parser protocol and shared provider-boundary helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from atlasrag.domain.enums import SourceType
from atlasrag.domain.errors import InvariantViolationError
from atlasrag.domain.ingestion.document import (
    DocumentContainer,
    DocumentMetadata,
    ParseInfo,
)
from atlasrag.domain.ingestion.elements import CanonicalElement
from atlasrag.domain.ingestion.enums import ParseOutcome, ParseReliability
from atlasrag.domain.ingestion.source import (
    CandidateBuildContext,
    CanonicalSource,
    SourceFile,
    resolve_source_target,
)


@runtime_checkable
class DocumentParser(Protocol):
    """Normalize one validated source directly into AtlasRAG canonical data."""

    async def parse(
        self,
        source: SourceFile,
        context: CandidateBuildContext,
    ) -> DocumentContainer: ...


def validate_parse_request(
    source: SourceFile,
    context: CandidateBuildContext,
    *,
    allowed_source_types: Iterable[SourceType],
) -> tuple[SourceFile, CandidateBuildContext]:
    """Revalidate parser inputs and enforce exact routing ownership."""
    if not isinstance(source, SourceFile) or not isinstance(
        context, CandidateBuildContext
    ):
        raise TypeError("parser inputs must be SourceFile and CandidateBuildContext")
    source = SourceFile.model_validate(source)
    context = CandidateBuildContext.model_validate(context)
    if source != context.source or source.source_type not in frozenset(
        allowed_source_types
    ):
        raise InvariantViolationError
    return source, context


async def read_source_bytes(source: SourceFile) -> bytes:
    """Read provider input outside database work and without blocking the loop."""

    def read() -> bytes:
        return resolve_source_target(source).read_bytes()

    return await asyncio.to_thread(read)


def failed_document(
    source: SourceFile,
    context: CandidateBuildContext,
    *,
    parser_name: str,
    parser_version: str,
    failure_code: str,
) -> DocumentContainer:
    """Build an explicit unusable parse result for corrupt user source data."""
    return DocumentContainer(
        document_id=context.document_id,
        revision_id=context.revision_id,
        source=CanonicalSource.from_source_file(source),
        metadata=DocumentMetadata(),
        parse_info=ParseInfo(
            outcome=ParseOutcome.FAILED,
            reliability=ParseReliability.UNAVAILABLE,
            parser_name=parser_name,
            parser_version=parser_version,
            failure_code=failure_code,
        ),
        elements=(),
    )


def successful_document(
    source: SourceFile,
    context: CandidateBuildContext,
    *,
    parser_name: str,
    parser_version: str,
    elements: tuple[CanonicalElement, ...],
    metadata: DocumentMetadata | None = None,
) -> DocumentContainer:
    """Build one reliable canonical parse result from normalized elements."""
    return DocumentContainer(
        document_id=context.document_id,
        revision_id=context.revision_id,
        source=CanonicalSource.from_source_file(source),
        metadata=metadata if metadata is not None else DocumentMetadata(),
        parse_info=ParseInfo(
            outcome=ParseOutcome.SUCCESS,
            reliability=ParseReliability.RELIABLE,
            parser_name=parser_name,
            parser_version=parser_version,
        ),
        elements=elements,
    )
