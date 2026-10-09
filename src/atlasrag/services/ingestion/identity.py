"""Deterministic source and candidate identity construction."""

from __future__ import annotations

import hashlib

from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.ingestion.elements import (
    CanonicalElement,
    CanonicalElementMetadata,
    TableData,
    derive_element_id,
    normalize_canonical_text,
    normalize_section_path,
    normalize_source_anchor,
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
    normalized_content = normalize_canonical_text(content)
    normalized_section = normalize_section_path(section_path)
    validated_anchor = normalize_source_anchor(source_anchor)
    validated_metadata = CanonicalElementMetadata.model_validate(
        metadata if metadata is not None else CanonicalElementMetadata()
    )
    validated_table = (
        None if table_data is None else TableData.model_validate(table_data)
    )
    candidate_element_id = derive_element_id(
        revision_id=revision_id,
        order=order,
        element_type=element_type,
        content=normalized_content,
        section_path=normalized_section,
        source_anchor=validated_anchor,
        table_data=validated_table,
    )
    return CanonicalElement(
        element_id=candidate_element_id,
        document_id=document_id,
        revision_id=revision_id,
        element_type=element_type,
        order=order,
        content=normalized_content,
        section_path=normalized_section,
        source_anchor=validated_anchor,
        metadata=validated_metadata,
        table_data=validated_table,
    )
