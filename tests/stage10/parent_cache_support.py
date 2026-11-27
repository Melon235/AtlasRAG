"""A-R5 deterministic Parent Context Cache harness.

Reuses the real ``RedisCacheProvider``, ``ParentContextService``,
``LocalEvidenceRunner``, ``RuntimeGate``, ``CacheFreshnessBarrier``,
``MaintenanceCoordinator``, ``ApplicationCoordinator``, Stage 3 canonical chunk
builders/mappings, and ``FakeUnitOfWorkFactory`` while isolating only raw Redis
transport and PostgreSQL storage boundaries.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, cast

from cache_support import FakeRawRedis
from stage3.fakes import FakeDatabase, FakeUnitOfWork, FakeUnitOfWorkFactory

from atlasrag._canonical import canonical_sha256
from atlasrag.domain.enums import EvidenceType, SourceType
from atlasrag.domain.evidence import EvidenceRef, LocalProvenance, SourceAnchor
from atlasrag.domain.ingestion.chunks import (
    ChunkingStrategy,
    ChunkRecord,
    ChunkType,
    ElementSpan,
    RetrievalRole,
    TableProvenance,
    TableSlice,
    TableSliceMode,
    TextParentProvenance,
    build_canonical_chunk,
    render_table_slice,
)
from atlasrag.domain.ingestion.document import DocumentMetadata, ParseInfo
from atlasrag.domain.ingestion.enums import ElementType, ParseOutcome, ParseReliability
from atlasrag.domain.ingestion.source import derive_document_id, derive_revision_id
from atlasrag.providers.cache.models import CachedParentContext
from atlasrag.providers.cache.redis import RedisCacheProvider
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
from atlasrag.services.ingestion.identity import build_element
from atlasrag.services.ingestion.mapping import (
    build_document_parse_metadata_slot,
    empty_document_parse_metadata_envelope,
    map_chunk_to_stored,
    replace_document_parse_metadata_slot,
    serialize_document_parse_metadata_envelope,
)
from atlasrag.services.local_evidence.parent_context import ParentContextService

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
ELEMENT_A = "elem_" + "1" * 64
ELEMENT_B = "elem_" + "2" * 64
PIPELINE_FINGERPRINT = canonical_sha256({"pipeline": "ar5-test"})


class ParentFakeRawRedis(FakeRawRedis):
    """Extend ``FakeRawRedis`` with delayed GET and per-call GET/SET fault injection."""

    def __init__(self, *, page_size: int = 100) -> None:
        super().__init__(page_size=page_size)
        self.get_modes: list[str] = []
        self.get_started = asyncio.Event()
        self.get_release = asyncio.Event()
        self.get_keys: list[str] = []
        self.set_keys: list[tuple[str, int | None]] = []

    async def get(self, key: str) -> str | None:
        self.get_keys.append(key)
        mode = self.get_modes.pop(0) if self.get_modes else self.get_mode
        if mode == "delay":
            self.get_calls += 1
            self._notify("get")
            self.get_started.set()
            await self.get_release.wait()
            return self.values.get(key)
        previous = self.get_mode
        self.get_mode = mode
        try:
            return await super().get(key)
        finally:
            self.get_mode = previous

    async def set(self, key: str, value: str, ex: int | None = None) -> object:
        self.set_keys.append((key, ex))
        return await super().set(key, value, ex=ex)


class TrackedChunkRepository:
    """Chunk repository fake matching ``ChunkRepository`` contracts over ``FakeDatabase``."""

    def __init__(
        self,
        factory: TrackedUnitOfWorkFactory,
        working: FakeDatabase,
    ) -> None:
        self._factory = factory
        self._working = working

    def _all_chunks(self) -> dict[str, StoredChunk]:
        by_id: dict[str, StoredChunk] = {}
        for chunks in self._working.chunks.values():
            for chunk in chunks:
                by_id[chunk.chunk_id] = chunk
        return by_id

    def _to_header(self, chunk: StoredChunk) -> CanonicalChunkHeader:
        payload: dict[str, object] = {
            "chunk_id": chunk.chunk_id,
            "document_id": chunk.document_id,
            "revision_id": chunk.revision_id,
            "chunk_type": chunk.chunk_type,
            "parent_id": chunk.parent_id,
            "section_path": chunk.section_path,
            "source_anchor": chunk.source_anchor,
        }
        if "sheet_name" in CanonicalChunkHeader.model_fields:
            payload["sheet_name"] = chunk.sheet_name
        if "metadata" in CanonicalChunkHeader.model_fields:
            payload["metadata"] = (
                None if self._factory.omit_header_metadata else chunk.metadata
            )
        if "strategy_metadata" in CanonicalChunkHeader.model_fields:
            payload["strategy_metadata"] = (
                None if self._factory.omit_header_metadata else chunk.strategy_metadata
            )
        return CanonicalChunkHeader.model_validate(payload)

    async def list_by_ids(self, chunk_ids: Sequence[str]) -> tuple[StoredChunk, ...]:
        self._factory.calls.append("chunks.list_by_ids")
        self._factory.list_by_ids_calls.append(tuple(chunk_ids))
        by_id = self._all_chunks()
        found = [by_id[cid] for cid in chunk_ids if cid in by_id]
        found.sort(key=lambda item: item.chunk_id)
        return tuple(found)

    async def get_by_id(self, chunk_id: str) -> StoredChunk | None:
        self._factory.calls.append("chunks.get_by_id")
        self._factory.get_by_id_calls.append(chunk_id)
        return self._all_chunks().get(chunk_id)

    async def get_canonical_context_header(
        self, document_id: str, revision_id: str, chunk_id: str
    ) -> CanonicalChunkHeader | None:
        self._factory.calls.append("chunks.get_canonical_context_header")
        self._factory.header_calls.append((document_id, revision_id, chunk_id))
        for chunk in self._working.chunks.get((document_id, revision_id), ()):
            if chunk.chunk_id == chunk_id:
                return self._to_header(chunk)
        return None


class BatchHeaderTrackedChunkRepository(TrackedChunkRepository):
    """Variant exposing ``list_canonical_context_headers`` like ``ChunkRepository``."""

    async def list_canonical_context_headers(
        self, chunk_ids: Sequence[str]
    ) -> tuple[CanonicalChunkHeader, ...]:
        self._factory.calls.append("chunks.list_canonical_context_headers")
        self._factory.batch_header_calls.append(tuple(chunk_ids))
        by_id = self._all_chunks()
        found = [self._to_header(by_id[cid]) for cid in chunk_ids if cid in by_id]
        found.sort(key=lambda item: item.chunk_id)
        return tuple(found)


class TrackedUnitOfWork(FakeUnitOfWork):
    def __init__(self, factory: TrackedUnitOfWorkFactory) -> None:
        super().__init__(factory)
        self._tracked_factory = factory
        self._tracked_chunks: TrackedChunkRepository | None = None

    @property
    def chunks(self) -> Any:
        self._guard()
        assert self._tracked_chunks is not None
        return self._tracked_chunks

    async def __aenter__(self) -> TrackedUnitOfWork:
        await super().__aenter__()
        assert self._working is not None
        self._tracked_factory.active_uows += 1
        self._tracked_factory.max_active_uows = max(
            self._tracked_factory.max_active_uows,
            self._tracked_factory.active_uows,
        )
        if self._tracked_factory.support_batch_headers:
            self._tracked_chunks = BatchHeaderTrackedChunkRepository(
                self._tracked_factory, self._working
            )
        else:
            self._tracked_chunks = TrackedChunkRepository(
                self._tracked_factory, self._working
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await super().__aexit__(exc_type, exc, traceback)
        finally:
            self._tracked_factory.active_uows -= 1


class TrackedUnitOfWorkFactory(FakeUnitOfWorkFactory):
    def __init__(
        self,
        database: FakeDatabase | None = None,
        *,
        support_batch_headers: bool = True,
        omit_header_metadata: bool = False,
    ) -> None:
        super().__init__(database)
        self.support_batch_headers = support_batch_headers
        self.omit_header_metadata = omit_header_metadata
        self.active_uows = 0
        self.max_active_uows = 0
        self.list_by_ids_calls: list[tuple[str, ...]] = []
        self.get_by_id_calls: list[str] = []
        self.header_calls: list[tuple[str, str, str]] = []
        self.batch_header_calls: list[tuple[str, ...]] = []

    def __call__(self) -> TrackedUnitOfWork:
        unit = TrackedUnitOfWork(self)
        self.unit_of_works.append(unit)
        return unit


def build_stored_document(
    *,
    relative_source_path: str = "docs/guide.md",
    file_name: str = "guide.md",
    source_type: SourceType = SourceType.MD,
    content_seed: str = "rev-1",
    published: bool = True,
) -> tuple[StoredDocument, str, str]:
    document_id = derive_document_id(relative_source_path)
    content_hash = canonical_sha256({"seed": content_seed})
    revision_id = derive_revision_id(document_id, content_hash, PIPELINE_FINGERPRINT)
    envelope = empty_document_parse_metadata_envelope()
    if published:
        element = build_element(
            document_id=document_id,
            revision_id=revision_id,
            element_type=ElementType.PARAGRAPH,
            order=0,
            content="Canonical source body",
        )
        slot_model = build_document_parse_metadata_slot(
            cast(
                Any,
                {
                    "schema_version": 1,
                    "document_id": document_id,
                    "revision_id": revision_id,
                    "source": {
                        "relative_source_path": relative_source_path,
                        "file_name": file_name,
                        "source_type": source_type.value,
                    },
                    "metadata": DocumentMetadata(),
                    "parse_info": ParseInfo(
                        outcome=ParseOutcome.SUCCESS,
                        reliability=ParseReliability.RELIABLE,
                        parser_name="test-parser",
                        parser_version="1",
                    ),
                    "elements": (element,),
                },
            )
        )
        envelope = replace_document_parse_metadata_slot(
            envelope, slot_name="current", slot=slot_model
        )
    stored = StoredDocument(
        document_id=document_id,
        file_name=file_name,
        relative_source_path=relative_source_path,
        source_type=source_type,
        observed_content_hash=content_hash,
        current_revision_id=revision_id if published else None,
        current_content_hash=content_hash if published else None,
        current_pipeline_fingerprint=PIPELINE_FINGERPRINT if published else None,
        ingestion_status=IngestionStatus.READY
        if published
        else IngestionStatus.RECEIVED,
        parse_metadata=cast(Any, serialize_document_parse_metadata_envelope(envelope)),
        created_at=NOW,
        updated_at=NOW,
    )
    return stored, document_id, revision_id


def build_stored_text_parent(
    *,
    document_id: str,
    revision_id: str,
    content: str = "  Canonical parent content\nline 2\t中文  ",
    section_path: tuple[str, ...] = ("Section", "Sub"),
    order: int = 0,
    element_id: str = ELEMENT_A,
) -> StoredChunk:
    span_len = max(1, len(content))
    chunk = build_canonical_chunk(
        document_id=document_id,
        revision_id=revision_id,
        chunk_type=ChunkType.TEXT_PARENT,
        retrieval_role=RetrievalRole.CONTEXT_ONLY,
        chunking_strategy=ChunkingStrategy.STRUCTURE,
        raw_content=content,
        parent_id=None,
        order=order,
        section_path=section_path,
        provenance=TextParentProvenance(
            source_spans=(
                ElementSpan(
                    element_id=element_id,
                    element_order=order,
                    char_start=0,
                    char_end=span_len,
                ),
            )
        ),
    )
    return map_chunk_to_stored(
        chunk,
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )


def build_stored_table(
    *,
    document_id: str,
    revision_id: str,
    order: int = 1,
    element_id: str = ELEMENT_B,
    sheet_name: str | None = "Summary",
    source_anchor: SourceAnchor | None = None,
) -> StoredChunk:
    anchor = source_anchor or SourceAnchor(sheet_name=sheet_name, cell_range="A1:B2")
    table_slice = TableSlice(
        mode=TableSliceMode.FULL,
        source_element_id=element_id,
        headers=("Key", "Value"),
        rows=(("Alpha", "1"),),
        row_start=0,
        row_end=1,
        sheet_name=sheet_name,
        source_anchor=anchor,
    )
    raw = render_table_slice(table_slice)
    chunk = build_canonical_chunk(
        document_id=document_id,
        revision_id=revision_id,
        chunk_type=ChunkType.TABLE,
        retrieval_role=RetrievalRole.RETRIEVABLE,
        chunking_strategy=ChunkingStrategy.TABLE,
        raw_content=raw,
        parent_id=None,
        order=order,
        section_path=("Tables",),
        provenance=TableProvenance(
            source_element_id=element_id,
            source_element_order=order,
            row_start=0,
            row_end=1,
            sheet_name=sheet_name,
        ),
        table_slice=table_slice,
    )
    return map_chunk_to_stored(
        chunk,
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )


def build_stored_text_child(
    parent: ChunkRecord,
    *,
    order: int = 1,
    element_id: str = ELEMENT_A,
) -> StoredChunk:
    from atlasrag.domain.ingestion.chunks import TextChildProvenance

    child = build_canonical_chunk(
        document_id=parent.document_id,
        revision_id=parent.revision_id,
        chunk_type=ChunkType.TEXT_CHILD,
        retrieval_role=RetrievalRole.RETRIEVABLE,
        chunking_strategy=ChunkingStrategy.RECURSIVE,
        raw_content=parent.raw_content,
        parent_id=parent.chunk_id,
        order=order,
        section_path=parent.section_path,
        provenance=TextChildProvenance(
            source_spans=(
                ElementSpan(
                    element_id=element_id,
                    element_order=0,
                    char_start=0,
                    char_end=max(1, len(parent.raw_content)),
                ),
            ),
            parent_char_start=0,
            parent_char_end=len(parent.raw_content),
        ),
    )
    return map_chunk_to_stored(
        child,
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )


def ref_for(chunk: StoredChunk, evidence_type: EvidenceType) -> EvidenceRef:
    return EvidenceRef(
        evidence_type=evidence_type,
        context_id=chunk.chunk_id,
        document_id=chunk.document_id,
        revision_id=chunk.revision_id,
    )


def cached_payload_for(
    chunk: StoredChunk,
    *,
    content: str | None = None,
    file_name: str = "wrong.md",
    relative_source_path: str = "wrong/path.md",
    section_path: tuple[str, ...] = ("StaleSection",),
) -> CachedParentContext:
    return CachedParentContext(
        evidence_type=EvidenceType.TEXT_PARENT,
        context_id=chunk.chunk_id,
        document_id=chunk.document_id,
        revision_id=chunk.revision_id,
        content=chunk.content if content is None else content,
        provenance=LocalProvenance(
            file_name=file_name,
            relative_source_path=relative_source_path,
            section_path=section_path,
        ),
    )


@dataclass
class ParentCacheRig:
    db: FakeDatabase
    factory: TrackedUnitOfWorkFactory
    raw: ParentFakeRawRedis
    provider: RedisCacheProvider
    service: ParentContextService
    disabled_service: ParentContextService
    uow_active_during_redis: list[tuple[str, bool]] = field(default_factory=list)


def build_parent_cache_rig(
    *,
    cache_enabled: bool = True,
    ttl_seconds: int = 3600,
    support_batch_headers: bool = True,
    omit_header_metadata: bool = False,
) -> ParentCacheRig:
    db = FakeDatabase()
    factory = TrackedUnitOfWorkFactory(
        db,
        support_batch_headers=support_batch_headers,
        omit_header_metadata=omit_header_metadata,
    )
    raw = ParentFakeRawRedis()
    uow_active_during_redis: list[tuple[str, bool]] = []
    raw.observer = lambda op: uow_active_during_redis.append(
        (op, factory.active_uows > 0)
    )
    provider = RedisCacheProvider(cast(Any, raw))
    service = ParentContextService(
        cast(Any, factory),
        cache=provider,
        cache_enabled=cache_enabled,
        cache_ttl_seconds=ttl_seconds,
    )
    disabled_service = ParentContextService(
        cast(Any, factory),
        cache=provider,
        cache_enabled=False,
        cache_ttl_seconds=ttl_seconds,
    )
    return ParentCacheRig(
        db=db,
        factory=factory,
        raw=raw,
        provider=provider,
        service=service,
        disabled_service=disabled_service,
        uow_active_during_redis=uow_active_during_redis,
    )
