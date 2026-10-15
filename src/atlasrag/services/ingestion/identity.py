"""Deterministic source and candidate identity construction."""

from __future__ import annotations

import hashlib

from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.ingestion.chunks import (
    ChunkingStrategy,
    ChunkProvenance,
    ChunkRecord,
    ChunkType,
    RetrievalRole,
    TableSlice,
    build_canonical_chunk,
    derive_chunk_content_hash,
    derive_chunk_id,
)
from atlasrag.domain.ingestion.elements import (
    CanonicalElement,
    CanonicalElementMetadata,
    TableData,
    build_canonical_element,
)
from atlasrag.domain.ingestion.enums import ElementType
from atlasrag.domain.ingestion.source import (
    CandidateBuildContext,
    SourceFile,
    canonical_relative_source_path,
    derive_document_id,
    derive_revision_id,
    resolve_source_target,
)

_DEFAULT_HASH_CHUNK_SIZE = 1024 * 1024


def document_id(source: SourceFile | str) -> str:
    """Return the v1 document identity for a source or canonical relative path."""
    if isinstance(source, SourceFile):
        relative_source_path = source.relative_source_path
    elif isinstance(source, str):
        relative_source_path = canonical_relative_source_path(source)
    else:
        raise TypeError("source must be a SourceFile or relative path string")
    return derive_document_id(relative_source_path)


def content_hash(
    source: SourceFile,
    *,
    chunk_size: int = _DEFAULT_HASH_CHUNK_SIZE,
) -> str:
    """Stream source bytes into a bounded-memory lowercase SHA-256 digest."""
    if not isinstance(source, SourceFile):
        raise TypeError("source must be a SourceFile")
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")

    digest = hashlib.sha256()
    target = resolve_source_target(source)
    with target.open("rb") as stream:
        while block := stream.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def revision_id(
    document_id: str,
    content_hash: str,
    pipeline_fingerprint: str,
) -> str:
    """Return the canonical candidate revision identity."""
    return derive_revision_id(document_id, content_hash, pipeline_fingerprint)


def build_candidate_context(
    source: SourceFile,
    pipeline_fingerprint: str,
) -> CandidateBuildContext:
    """Hash one source and construct its immutable dependency-free context."""
    if not isinstance(source, SourceFile):
        raise TypeError("source must be a SourceFile")
    candidate_document_id = document_id(source)
    candidate_content_hash = content_hash(source)
    candidate_revision_id = revision_id(
        candidate_document_id,
        candidate_content_hash,
        pipeline_fingerprint,
    )
    return CandidateBuildContext(
        source=source,
        document_id=candidate_document_id,
        content_hash=candidate_content_hash,
        pipeline_fingerprint=pipeline_fingerprint,
        revision_id=candidate_revision_id,
    )


def build_element(
    *,
    document_id: str,
    revision_id: str,
    element_type: ElementType,
    order: int,
    content: str,
    section_path: tuple[str, ...] = (),
    source_anchor: SourceAnchor | None = None,
    metadata: CanonicalElementMetadata | None = None,
    table_data: TableData | None = None,
) -> CanonicalElement:
    """Normalize canonical element inputs and attach their deterministic ID."""
    return build_canonical_element(
        document_id=document_id,
        revision_id=revision_id,
        element_type=element_type,
        order=order,
        content=content,
        section_path=section_path,
        source_anchor=source_anchor,
        metadata=metadata,
        table_data=table_data,
    )


def chunk_content_hash(raw_content: str) -> str:
    """Return the canonical content digest for one chunk rendering."""
    return derive_chunk_content_hash(raw_content)


def chunk_id(
    *,
    document_id: str,
    revision_id: str,
    chunk_type: ChunkType,
    chunking_strategy: ChunkingStrategy,
    provenance: ChunkProvenance,
    content_hash: str,
) -> str:
    """Return a deterministic chunk identity for typed structural lineage."""
    return derive_chunk_id(
        document_id=document_id,
        revision_id=revision_id,
        chunk_type=chunk_type,
        chunking_strategy=chunking_strategy,
        provenance=provenance,
        content_hash=content_hash,
    )


def build_chunk(
    *,
    document_id: str,
    revision_id: str,
    chunk_type: ChunkType,
    retrieval_role: RetrievalRole,
    chunking_strategy: ChunkingStrategy,
    raw_content: str,
    parent_id: str | None,
    order: int,
    section_path: tuple[str, ...],
    provenance: ChunkProvenance,
    table_slice: TableSlice | None = None,
) -> ChunkRecord:
    """Normalize one chunk and attach its deterministic content and chunk IDs."""
    return build_canonical_chunk(
        document_id=document_id,
        revision_id=revision_id,
        chunk_type=chunk_type,
        retrieval_role=retrieval_role,
        chunking_strategy=chunking_strategy,
        raw_content=raw_content,
        parent_id=parent_id,
        order=order,
        section_path=section_path,
        provenance=provenance,
        table_slice=table_slice,
    )
