"""Native deterministic UTF-8 text parser."""

from __future__ import annotations

from atlasrag.domain.enums import SourceType
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.elements import (
    build_canonical_element,
    normalize_canonical_text,
)
from atlasrag.domain.ingestion.enums import ElementType
from atlasrag.domain.ingestion.source import CandidateBuildContext, SourceFile
from atlasrag.providers.parsing.base import (
    failed_document,
    read_source_bytes,
    successful_document,
    validate_parse_request,
)

_PARSER_NAME = "atlasrag-text"
_PARSER_VERSION = "1"


class TextParser:
    """Parse UTF-8 text into deterministic paragraph elements."""

    async def parse(
        self,
        source: SourceFile,
        context: CandidateBuildContext,
    ) -> DocumentContainer:
        source, context = validate_parse_request(
            source,
            context,
            allowed_source_types=(SourceType.TXT,),
        )
        payload = await read_source_bytes(source)
        try:
            decoded = payload.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError:
            return self._failed(source, context, "INVALID_UTF8")
        if not decoded.strip():
            return self._failed(source, context, "EMPTY_SOURCE")
        try:
            normalized = normalize_canonical_text(decoded)
        except ValueError:
            return self._failed(source, context, "INVALID_TEXT_CONTROL")

        blocks: list[str] = []
        current: list[str] = []
        for line in normalized.split("\n"):
            if line.strip():
                current.append(line)
            elif current:
                blocks.append("\n".join(current))
                current = []
        if current:
            blocks.append("\n".join(current))

        elements = tuple(
            build_canonical_element(
                document_id=context.document_id,
                revision_id=context.revision_id,
                element_type=ElementType.PARAGRAPH,
                order=order,
                content=block,
            )
            for order, block in enumerate(blocks)
        )
        return successful_document(
            source,
            context,
            parser_name=_PARSER_NAME,
            parser_version=_PARSER_VERSION,
            elements=elements,
        )

    @staticmethod
    def _failed(
        source: SourceFile,
        context: CandidateBuildContext,
        failure_code: str,
    ) -> DocumentContainer:
        return failed_document(
            source,
            context,
            parser_name=_PARSER_NAME,
            parser_version=_PARSER_VERSION,
            failure_code=failure_code,
        )
