"""Batch canonical Parent/Table expansion inside the published revision."""

from __future__ import annotations

from typing import Any, cast

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
from atlasrag.repositories.postgres.records import StoredChunk, StoredDocument
from atlasrag.services.ingestion.mapping import map_chunk_from_stored
from atlasrag.services.local_evidence.ports import (
    ChunkBatchReader,
    LocalEvidenceUnitOfWorkFactory,
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
        if (
            self._cache_enabled
            and self._cache is not None
            and all(ref.evidence_type is EvidenceType.TEXT_PARENT for ref in refs)
        ):
            return await self._expand_text_cached(refs)
        chunk_ids = tuple(ref.context_id for ref in refs)
        if len(set(chunk_ids)) != len(chunk_ids):
            raise CanonicalDataError
        async with self._uow_factory() as unit:
            reader = cast(ChunkBatchReader, unit.chunks)
            stored_chunks = await reader.list_by_ids(chunk_ids)
            by_id = {chunk.chunk_id: chunk for chunk in stored_chunks}
            if len(by_id) != len(chunk_ids):
                raise CanonicalDataError
            documents: dict[str, StoredDocument] = {}
            for ref in refs:
                if ref.document_id not in documents:
                    document = await unit.documents.get_by_id(ref.document_id)
                    if document is None:
                        raise CanonicalDataError
                    documents[ref.document_id] = document
        evidence: list[LocalEvidence] = []
        for ref in refs:
            stored = by_id[ref.context_id]
            document = documents[ref.document_id]
            evidence.append(self._expand_one(ref, stored, document))
        return tuple(evidence)

    async def _expand_text_cached(
        self, refs: tuple[EvidenceRef, ...]
    ) -> tuple[LocalEvidence, ...]:
        chunk_ids = tuple(ref.context_id for ref in refs)
        if len(set(chunk_ids)) != len(chunk_ids):
            raise CanonicalDataError
        writes: list[LocalEvidence] = []
        evidence: list[LocalEvidence] = []
        cache = self._cache
        assert cache is not None
        async with self._uow_factory() as unit:
            for ref in refs:
                document = await unit.documents.get_by_id(ref.document_id)
                if document is None or document.current_revision_id != ref.revision_id:
                    raise CanonicalDataError
                reader = cast(Any, unit.chunks)
                header = await reader.get_canonical_context_header(
                    ref.document_id, ref.revision_id, ref.context_id
                )
                if (
                    header is None
                    or header.chunk_type is not ChunkType.TEXT_PARENT
                    or header.document_id != ref.document_id
                    or header.revision_id != ref.revision_id
                    or header.chunk_id != ref.context_id
                    or header.parent_id is not None
                ):
                    raise CanonicalDataError
                cached: CachedParentContext | None = None
                try:
                    cached = await cache.get_parent(ref.revision_id, ref.context_id)
                except (DependencyError, ProviderResponseError):
                    cached = None
                if cached is not None and (
                    cached.evidence_type is EvidenceType.TEXT_PARENT
                    and cached.document_id == ref.document_id
                    and cached.revision_id == ref.revision_id
                    and cached.context_id == ref.context_id
                ):
                    item = LocalEvidence(
                        evidence_type=EvidenceType.TEXT_PARENT,
                        context_id=ref.context_id,
                        document_id=ref.document_id,
                        revision_id=ref.revision_id,
                        content=cached.content,
                        provenance=LocalProvenance(
                            file_name=document.file_name,
                            relative_source_path=document.relative_source_path,
                            section_path=header.section_path,
                            source_anchor=None,
                        ),
                    )
                    evidence.append(item)
                    continue
                stored = await reader.get_by_id(ref.context_id)
                if stored is None:
                    raise CanonicalDataError
                item = self._expand_one(ref, stored, document)
                evidence.append(item)
                writes.append(item)
        if self._cache is not None:
            for item in writes:
                try:
                    await cache.put_parent(
                        CachedParentContext(
                            evidence_type=item.evidence_type,
                            context_id=item.context_id,
                            document_id=item.document_id,
                            revision_id=item.revision_id,
                            content=item.content,
                            provenance=item.provenance,
                        ),
                        ttl_seconds=self._cache_ttl_seconds,
                    )
                except (DependencyError, ProviderResponseError):
                    pass
        return tuple(evidence)

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
