"""Explicit mappings from canonical representations to transient derived rows."""

from atlasrag.domain.errors import InvariantViolationError
from atlasrag.domain.indexing.models import RetrievalRecord, RetrievalRepresentation
from atlasrag.domain.ingestion.chunks import ChunkRecord
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.providers.index.models import MilvusChunkRecord


def build_retrieval_record(
    document: DocumentContainer,
    chunk: ChunkRecord,
    representation: RetrievalRepresentation,
    dense_vector: tuple[float, ...],
) -> RetrievalRecord:
    """Attach canonical source/Parent identities to one already validated vector."""
    if (document.document_id, document.revision_id) != (
        chunk.document_id,
        chunk.revision_id,
    ) or (
        representation.chunk_id,
        representation.document_id,
        representation.revision_id,
    ) != (chunk.chunk_id, chunk.document_id, chunk.revision_id):
        raise InvariantViolationError
    return RetrievalRecord(
        chunk_id=representation.chunk_id,
        document_id=representation.document_id,
        revision_id=representation.revision_id,
        file_name=document.source.file_name,
        parent_id=chunk.parent_id,
        chunk_type=representation.chunk_type,
        source_type=document.source.source_type,
        content=representation.dense_text,
        sparse_text=representation.sparse_text,
        dense_vector=dense_vector,
        structured_metadata=representation.structured_metadata,
    )


def map_retrieval_record_to_milvus(record: RetrievalRecord) -> MilvusChunkRecord:
    """Serialize typed structural facts explicitly at the existing Milvus boundary."""
    validated = RetrievalRecord.model_validate(record)
    return MilvusChunkRecord(
        chunk_id=validated.chunk_id,
        document_id=validated.document_id,
        revision_id=validated.revision_id,
        file_name=validated.file_name,
        parent_id=validated.parent_id,
        chunk_type=validated.chunk_type,
        source_type=validated.source_type,
        content=validated.content,
        sparse_text=validated.sparse_text,
        dense_vector=validated.dense_vector,
        structured_metadata=validated.structured_metadata.model_dump(mode="json"),
    )
