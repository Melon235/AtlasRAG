"""Batch canonical Parent/Table expansion inside the published revision."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from atlasrag._canonical import validate_unicode_scalar_text
from atlasrag.domain.enums import EvidenceType
from atlasrag.domain.errors import (
    CanonicalDataError,
    DependencyError,
    ProviderResponseError,
)
from atlasrag.domain.evidence import EvidenceRef, LocalEvidence, LocalProvenance
from atlasrag.domain.ingestion.chunks import (
    ChunkType,
    TableProvenance,
    TextParentProvenance,
)
from atlasrag.providers.cache.models import CachedParentContext
from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.repositories.postgres.chunks import CanonicalChunkHeader
from atlasrag.repositories.postgres.records import (
    StoredChunk,
    StoredChunkType,
    StoredDocument,
)
from atlasrag.services.ingestion.mapping import (
    map_chunk_from_stored,
    validate_text_parent_chunk_header,
)
from atlasrag.services.local_evidence.ports import (
    ChunkBatchReader,
    LocalEvidenceUnitOfWorkFactory,
)


def _is_cache_key_safe(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        validate_unicode_scalar_text(value)
    except ValueError:
        return False
    return bool(
        value
        and value == value.strip()
        and ":" not in value
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _to_cached_parent(item: LocalEvidence) -> CachedParentContext | None:
    if (
        item.evidence_type is not EvidenceType.TEXT_PARENT
        or not item.content.strip()
        or not _is_cache_key_safe(item.revision_id)
        or not _is_cache_key_safe(item.context_id)
    ):
        return None
    return CachedParentContext(
        evidence_type=item.evidence_type,
        context_id=item.context_id,
        document_id=item.document_id,
        revision_id=item.revision_id,
        content=item.content,
        provenance=item.provenance,
    )


class ParentContextService:
    def __init__(
        self,
        uow_factory: LocalEvidenceUnitOfWorkFactory,
        *,
        cache: RedisCacheProvider | None = None,
        cache_enabled: bool = False,
        cache_ttl_seconds: int = 86_400,
    ) -> None:
        self._uow_factory = uow_factory
        self._cache = cache
        self._cache_enabled = cache_enabled
        self._cache_ttl_seconds = cache_ttl_seconds

    async def expand(self, refs: tuple[EvidenceRef, ...]) -> tuple[LocalEvidence, ...]:
        if not refs:
            return ()
        chunk_ids = tuple(ref.context_id for ref in refs)
        if len(set(chunk_ids)) != len(chunk_ids):
            raise CanonicalDataError

        cache = self._cache if self._cache_enabled else None
        hits: dict[str, CachedParentContext] = {}
        if cache is not None:
            for ref in refs:
                if (
                    ref.evidence_type is not EvidenceType.TEXT_PARENT
                    or not _is_cache_key_safe(ref.revision_id)
                    or not _is_cache_key_safe(ref.context_id)
                ):
                    continue
                try:
                    cached = await cache.get_parent(ref.revision_id, ref.context_id)
                except (DependencyError, ProviderResponseError):
                    cached = None
                if (
                    cached is not None
                    and cached.evidence_type is EvidenceType.TEXT_PARENT
                    and cached.document_id == ref.document_id
                    and cached.revision_id == ref.revision_id
                    and cached.context_id == ref.context_id
                ):
                    hits[ref.context_id] = cached

        documents: dict[str, StoredDocument] = {}
        validated_section_paths: dict[str, tuple[str, ...]] = {}
        stored_by_id: dict[str, StoredChunk] = {}

        async with self._uow_factory() as unit:
            for ref in refs:
                document = documents.get(ref.document_id)
                if document is None:
                    document = await unit.documents.get_by_id(ref.document_id)
                    if document is None:
                        raise CanonicalDataError
                    documents[ref.document_id] = document
                if (
                    document.document_id != ref.document_id
                    or document.current_revision_id != ref.revision_id
                ):
                    raise CanonicalDataError

            reader = cast(ChunkBatchReader, unit.chunks)
            hit_refs = tuple(ref for ref in refs if ref.context_id in hits)
            if hit_refs:
                headers_by_id = await self._load_headers(reader, hit_refs)
                for ref in hit_refs:
                    header = headers_by_id.get(ref.context_id)
                    if (
                        header is None
                        or header.document_id != ref.document_id
                        or header.revision_id != ref.revision_id
                        or header.chunk_id != ref.context_id
                        or header.chunk_type is not StoredChunkType.TEXT_PARENT
                        or header.parent_id is not None
                    ):
                        raise CanonicalDataError
                    if header.metadata is None or header.strategy_metadata is None:
                        continue
                    validated_section_paths[ref.context_id] = (
                        validate_text_parent_chunk_header(header)
                    )

            full_read_ids = tuple(
                ref.context_id
                for ref in refs
                if ref.context_id not in validated_section_paths
            )
            if full_read_ids:
                stored_chunks = await reader.list_by_ids(full_read_ids)
                stored_by_id = {chunk.chunk_id: chunk for chunk in stored_chunks}
                if len(stored_by_id) != len(full_read_ids) or not all(
                    chunk_id in stored_by_id for chunk_id in full_read_ids
                ):
                    raise CanonicalDataError

        writes: list[CachedParentContext] = []
        evidence: list[LocalEvidence] = []
        for ref in refs:
            document = documents[ref.document_id]
            section_path = validated_section_paths.get(ref.context_id)
            if section_path is not None:
                cached = hits[ref.context_id]
                evidence.append(
                    LocalEvidence(
                        evidence_type=EvidenceType.TEXT_PARENT,
                        context_id=ref.context_id,
                        document_id=ref.document_id,
                        revision_id=ref.revision_id,
                        content=cached.content,
                        provenance=LocalProvenance(
                            file_name=document.file_name,
                            relative_source_path=document.relative_source_path,
                            section_path=section_path,
                            source_anchor=None,
                        ),
                    )
                )
                continue

            item = self._expand_one(ref, stored_by_id[ref.context_id], document)
            evidence.append(item)
            if cache is not None and ref.context_id not in hits:
                payload = _to_cached_parent(item)
                if payload is not None:
                    writes.append(payload)

        if cache is not None:
            for payload in writes:
                try:
                    await cache.put_parent(
                        payload,
                        ttl_seconds=self._cache_ttl_seconds,
                    )
                except (DependencyError, ProviderResponseError):
                    pass
        return tuple(evidence)

    async def _load_headers(
        self,
        reader: ChunkBatchReader,
        hit_refs: tuple[EvidenceRef, ...],
    ) -> dict[str, CanonicalChunkHeader]:
        batch_loader = getattr(reader, "list_canonical_context_headers", None)
        if callable(batch_loader):
            loaded = cast(
                Sequence[CanonicalChunkHeader],
                await batch_loader(tuple(ref.context_id for ref in hit_refs)),
            )
            return {header.chunk_id: header for header in loaded}
        headers: dict[str, CanonicalChunkHeader] = {}
        for ref in hit_refs:
            header = await reader.get_canonical_context_header(
                ref.document_id, ref.revision_id, ref.context_id
            )
            if header is not None:
                headers[ref.context_id] = header
        return headers

    def _expand_one(
        self, ref: EvidenceRef, stored: StoredChunk, document: StoredDocument
    ) -> LocalEvidence:
        try:
            chunk = map_chunk_from_stored(stored)
        except (CanonicalDataError, ValueError):
            raise CanonicalDataError from None
        if (
            document.document_id != ref.document_id
            or document.current_revision_id != ref.revision_id
            or chunk.document_id != ref.document_id
            or chunk.revision_id != ref.revision_id
            or chunk.chunk_id != ref.context_id
            or chunk.parent_id is not None
        ):
            raise CanonicalDataError
        if ref.evidence_type is EvidenceType.TEXT_PARENT:
            if (
                chunk.chunk_type is not ChunkType.TEXT_PARENT
                or not isinstance(chunk.provenance, TextParentProvenance)
                or chunk.table_slice is not None
            ):
                raise CanonicalDataError
            anchor = None
        elif ref.evidence_type is EvidenceType.TABLE:
            if (
                chunk.chunk_type is not ChunkType.TABLE
                or not isinstance(chunk.provenance, TableProvenance)
                or chunk.table_slice is None
            ):
                raise CanonicalDataError
            anchor = chunk.table_slice.source_anchor
        else:
            raise CanonicalDataError
        return LocalEvidence(
            evidence_type=ref.evidence_type,
            context_id=ref.context_id,
            document_id=ref.document_id,
            revision_id=ref.revision_id,
            content=chunk.raw_content,
            provenance=LocalProvenance(
                file_name=document.file_name,
                relative_source_path=document.relative_source_path,
                section_path=chunk.section_path,
                source_anchor=anchor,
            ),
        )
