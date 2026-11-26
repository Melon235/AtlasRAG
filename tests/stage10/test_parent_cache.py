from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from atlasrag._canonical import canonical_sha256
from atlasrag.domain.enums import EvidenceType, SourceType
from atlasrag.domain.evidence import EvidenceRef, LocalProvenance
from atlasrag.domain.ingestion.chunks import (
    ChunkingStrategy,
    ChunkType,
    ElementSpan,
    RetrievalRole,
    TextParentProvenance,
    build_canonical_chunk,
)
from atlasrag.domain.ingestion.source import derive_document_id, derive_revision_id
from atlasrag.providers.cache.models import CachedParentContext
from atlasrag.repositories.postgres.chunks import CanonicalChunkHeader
from atlasrag.repositories.postgres.records import (
    IngestionStatus,
    StoredChunk,
    StoredDocument,
)
from atlasrag.services.ingestion.chunking import (
    DEFAULT_CHUNKER_BEHAVIOR_VERSION,
    PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
)
from atlasrag.services.ingestion.mapping import map_chunk_to_stored
from atlasrag.services.local_evidence.parent_context import ParentContextService

DOC = derive_document_id("guide.md")
REV = derive_revision_id(
    DOC, canonical_sha256({"seed": "guide"}), canonical_sha256({"pipeline": "v1"})
)
_CANONICAL_PARENT = map_chunk_to_stored(
    build_canonical_chunk(
        document_id=DOC,
        revision_id=REV,
        chunk_type=ChunkType.TEXT_PARENT,
        retrieval_role=RetrievalRole.CONTEXT_ONLY,
        chunking_strategy=ChunkingStrategy.STRUCTURE,
        raw_content="  canonical parent  ",
        parent_id=None,
        order=0,
        section_path=("Intro",),
        provenance=TextParentProvenance(
            source_spans=(
                ElementSpan(
                    element_id="elem_" + "1" * 64,
                    element_order=0,
                    char_start=0,
                    char_end=len("  canonical parent  "),
                ),
            )
        ),
    ),
    chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
    profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
    created_at=datetime(2026, 1, 1, tzinfo=UTC),
)
CTX = _CANONICAL_PARENT.chunk_id


def _document() -> StoredDocument:
    return StoredDocument(
        document_id=DOC,
        file_name="guide.md",
        relative_source_path="guide.md",
        source_type=SourceType.MD,
        observed_content_hash="a" * 64,
        current_revision_id=REV,
        current_content_hash="a" * 64,
        current_pipeline_fingerprint="f" * 64,
        ingestion_status=IngestionStatus.READY,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


class _Chunks:
    def __init__(self) -> None:
        self.full_reads = 0

    async def get_canonical_context_header(
        self, document_id: str, revision_id: str, chunk_id: str
    ) -> CanonicalChunkHeader:
        return CanonicalChunkHeader(
            document_id=document_id,
            revision_id=revision_id,
            chunk_id=chunk_id,
            chunk_type=_CANONICAL_PARENT.chunk_type,
            parent_id=None,
            section_path=_CANONICAL_PARENT.section_path,
            source_anchor=_CANONICAL_PARENT.source_anchor,
            sheet_name=_CANONICAL_PARENT.sheet_name,
            metadata=_CANONICAL_PARENT.metadata,
            strategy_metadata=_CANONICAL_PARENT.strategy_metadata,
        )

    async def list_by_ids(self, chunk_ids: tuple[str, ...]) -> tuple[StoredChunk, ...]:
        self.full_reads += 1
        return tuple([await self.get_by_id(chunk_id) for chunk_id in chunk_ids])

    async def get_by_id(self, chunk_id: str) -> StoredChunk:
        self.full_reads += 1
        return _CANONICAL_PARENT


class _Documents:
    async def get_by_id(self, document_id: str) -> StoredDocument:
        return _document()


class _Unit:
    def __init__(self, chunks: _Chunks) -> None:
        self.documents = _Documents()
        self.chunks = chunks

    async def __aenter__(self) -> _Unit:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _Cache:
    def __init__(self, hit: bool = True) -> None:
        self.puts = 0
        self.hit = hit

    async def get_parent(
        self, revision_id: str, parent_id: str
    ) -> CachedParentContext | None:
        if not self.hit:
            return None
        return CachedParentContext(
            evidence_type=EvidenceType.TEXT_PARENT,
            context_id=CTX,
            document_id=DOC,
            revision_id=REV,
            content="  parent content  ",
            provenance=LocalProvenance(
                file_name="cached.md", relative_source_path="cached.md"
            ),
        )

    async def put_parent(self, *args: object, **kwargs: object) -> None:
        self.puts += 1


@pytest.mark.asyncio
async def test_valid_text_parent_cache_hit_reconstructs_authoritative_provenance() -> (
    None
):
    cache = _Cache()
    chunks = _Chunks()
    service = ParentContextService(
        cast(Any, lambda: _Unit(chunks)),
        cache=cache,  # type: ignore[arg-type]
        cache_enabled=True,
    )
    ref = EvidenceRef(
        evidence_type=EvidenceType.TEXT_PARENT,
        context_id=CTX,
        document_id=DOC,
        revision_id=REV,
    )
    evidence = await service.expand((ref,))
    assert len(evidence) == 1
    assert evidence[0].content == "  parent content  "
    assert evidence[0].provenance.file_name == "guide.md"
    assert evidence[0].provenance.relative_source_path == "guide.md"
    assert evidence[0].provenance.section_path == ("Intro",)
    assert cache.puts == 0
    assert chunks.full_reads == 0
