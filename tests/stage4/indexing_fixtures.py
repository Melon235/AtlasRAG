"""Canonical Stage 3 builders shared by Stage 4 contract tests."""

from atlasrag.domain.enums import SourceType
from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.indexing.models import RepresentationProfileV1, RetrievalRecord
from atlasrag.domain.ingestion.chunks import (
    ChunkingStrategy,
    ChunkRecord,
    ChunkType,
    ElementSpan,
    RetrievalRole,
    TableProvenance,
    TableSlice,
    TableSliceMode,
    TextChildProvenance,
    TextParentProvenance,
    build_canonical_chunk,
    render_table_slice,
)
from atlasrag.domain.ingestion.document import (
    DocumentContainer,
    DocumentMetadata,
    ParseInfo,
)
from atlasrag.domain.ingestion.enums import ElementType, ParseOutcome, ParseReliability
from atlasrag.domain.ingestion.source import CanonicalSource, derive_document_id
from atlasrag.services.indexing.representation import RepresentationBuilder
from atlasrag.services.ingestion.chunking import build_chunks
from atlasrag.services.ingestion.identity import build_element

DOCUMENT_ID = derive_document_id("docs/guide.md")
REVISION_ID = "rev_" + "a" * 64
ELEMENT_ID = "elem_" + "b" * 64


def canonical_whitespace_chunks(
    content: str = "A" + " " * 4000 + "B",
) -> tuple[ChunkRecord, ...]:
    """Exercise the real frozen Stage 3 default chunker over a source paragraph."""
    element = build_element(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        element_type=ElementType.PARAGRAPH,
        order=0,
        content=content,
        section_path=("Whitespace",),
    )
    document = DocumentContainer(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        source=CanonicalSource(
            relative_source_path="docs/guide.md",
            file_name="guide.md",
            source_type=SourceType.MD,
        ),
        metadata=DocumentMetadata(),
        parse_info=ParseInfo(
            outcome=ParseOutcome.SUCCESS,
            reliability=ParseReliability.RELIABLE,
            parser_name="test-canonical",
            parser_version="1",
        ),
        elements=(element,),
    )
    return build_chunks(document)


def retrieval_record(chunk: ChunkRecord) -> RetrievalRecord:
    """Build the transient record with an explicit test vector, without inference."""
    representation = RepresentationBuilder(
        RepresentationProfileV1(),
        representation_version="representation-v1",
    ).build(chunk)
    return RetrievalRecord(
        chunk_id=representation.chunk_id,
        document_id=representation.document_id,
        revision_id=representation.revision_id,
        file_name="guide.md",
        parent_id=chunk.parent_id,
        chunk_type=representation.chunk_type,
        source_type=SourceType.MD,
        content=representation.dense_text,
        sparse_text=representation.sparse_text,
        dense_vector=(1.0, 0.0, 0.0),
        structured_metadata=representation.structured_metadata,
    )


def text_chunks(
    content: str = "原文 exact\nsecond line",
) -> tuple[ChunkRecord, ChunkRecord]:
    spans = (
        ElementSpan(
            element_id=ELEMENT_ID, element_order=3, char_start=0, char_end=len(content)
        ),
    )
    parent = build_canonical_chunk(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        chunk_type=ChunkType.TEXT_PARENT,
        retrieval_role=RetrievalRole.CONTEXT_ONLY,
        chunking_strategy=ChunkingStrategy.STRUCTURE,
        raw_content=content,
        parent_id=None,
        order=0,
        section_path=("Overview",),
        provenance=TextParentProvenance(source_spans=spans),
    )
    child = build_canonical_chunk(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        chunk_type=ChunkType.TEXT_CHILD,
        retrieval_role=RetrievalRole.RETRIEVABLE,
        chunking_strategy=ChunkingStrategy.RECURSIVE,
        raw_content=content,
        parent_id=parent.chunk_id,
        order=1,
        section_path=parent.section_path,
        provenance=TextChildProvenance(
            source_spans=spans, parent_char_start=0, parent_char_end=len(content)
        ),
    )
    return parent, child


def table_chunk(mode: TableSliceMode = TableSliceMode.FULL) -> ChunkRecord:
    start = 0 if mode is TableSliceMode.FULL else 5
    table = TableSlice(
        mode=mode,
        source_element_id=ELEMENT_ID,
        headers=("Name", "值"),
        rows=(("Alpha", "1"), ("Beta", "2")),
        row_start=start,
        row_end=start + 2,
        caption="Quarterly report",
        sheet_name="Metrics",
        source_anchor=SourceAnchor(sheet_name="Metrics", cell_range="A1:B9"),
    )
    return build_canonical_chunk(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        chunk_type=ChunkType.TABLE,
        retrieval_role=RetrievalRole.RETRIEVABLE,
        chunking_strategy=ChunkingStrategy.TABLE,
        raw_content=render_table_slice(table),
        parent_id=None,
        order=2,
        section_path=("Metrics",),
        table_slice=table,
        provenance=TableProvenance(
            source_element_id=ELEMENT_ID,
            source_element_order=4,
            row_start=start,
            row_end=start + 2,
            sheet_name="Metrics",
        ),
    )
