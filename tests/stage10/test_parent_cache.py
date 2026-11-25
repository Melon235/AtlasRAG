from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from atlasrag.domain.enums import EvidenceType, SourceType
from atlasrag.domain.evidence import EvidenceRef, LocalProvenance
from atlasrag.providers.cache.models import CachedParentContext
from atlasrag.repositories.postgres.chunks import CanonicalChunkHeader
from atlasrag.repositories.postgres.records import (
    IngestionStatus,
    StoredChunk,
    StoredChunkType,
    StoredDocument,
)
from atlasrag.services.local_evidence.parent_context import ParentContextService

DOC = "d" * 64
REV = "r" * 64
CTX = "c" * 64


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
    async def get_canonical_context_header(
        self, document_id: str, revision_id: str, chunk_id: str
    ) -> CanonicalChunkHeader:
        return CanonicalChunkHeader(
            document_id=document_id,
            revision_id=revision_id,
            chunk_id=chunk_id,
            chunk_type=StoredChunkType.TEXT_PARENT,
            parent_id=None,
            section_path=("Intro",),
        )

    async def list_by_ids(self, chunk_ids: tuple[str, ...]) -> tuple[StoredChunk, ...]:
        return tuple([await self.get_by_id(chunk_id) for chunk_id in chunk_ids])

    async def get_by_id(self, chunk_id: str) -> StoredChunk:
        return StoredChunk(
            chunk_id=chunk_id,
            document_id=DOC,
            revision_id=REV,
            chunk_type=StoredChunkType.TEXT_PARENT,
            content="  canonical parent  ",
            section_path=("Intro",),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


class _Documents:
    async def get_by_id(self, document_id: str) -> StoredDocument:
        return _document()


class _Unit:
    documents = _Documents()
    chunks = _Chunks()

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
    service = ParentContextService(
        cast(Any, lambda: _Unit()),
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
