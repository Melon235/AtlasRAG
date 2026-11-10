"""Exact-set repair of a published current revision's derived index rows."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import ValidationError

from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import CanonicalDataError, InvariantViolationError
from atlasrag.domain.indexing.models import (
    EmbeddingModelIdentity,
    RepresentationProfileV1,
)
from atlasrag.domain.ingestion.chunks import ChunkRecord, RetrievalRole
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.validation import ValidationOutcome
from atlasrag.providers.embedding.base import (
    EmbeddingProvider,
    validate_embedding_output,
)
from atlasrag.repositories.postgres.records import StoredChunk, StoredDocument
from atlasrag.services.indexing.ports import CandidateIndex
from atlasrag.services.indexing.records import (
    build_retrieval_record,
    map_retrieval_record_to_milvus,
)
from atlasrag.services.indexing.representation import RepresentationBuilder
from atlasrag.services.ingestion.chunking import ChunkingProfile
from atlasrag.services.ingestion.mapping import (
    ChunkStrategyMetadataV1,
    map_chunks_from_stored,
    map_current_document_from_stored,
)
from atlasrag.services.ingestion.validation import validate_chunk_set
from atlasrag.services.sync.failures import CurrentRepairBlockedError
from atlasrag.services.sync.ports import SyncUnitOfWorkFactory
from atlasrag.services.sync.verification import expected_current_projection

Clock = Callable[[], datetime]


class CurrentRepairOutcome(StrEnum):
    CLEAN = "CLEAN"
    REPAIRED = "REPAIRED"


def _json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _device_matches(identity_device: str, configured: str) -> bool:
    if identity_device == configured:
        return True
    return (
        configured == "cuda"
        and re.fullmatch(r"cuda:[0-9]+", identity_device) is not None
    )


@dataclass(frozen=True)
class _Current:
    document: DocumentContainer
    chunks: tuple[ChunkRecord, ...]
    stored_document: StoredDocument
    stored_chunks: tuple[StoredChunk, ...]


class CurrentIndexRepairService:
    """Repair derived current rows only; PostgreSQL current is never rewritten."""

    def __init__(
        self,
        uow_factory: SyncUnitOfWorkFactory,
        *,
        runtime: RuntimeConfig,
        embedding: EmbeddingProvider,
        index: CandidateIndex,
        clock: Clock,
        batch_size: int = 1,
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        self._uow_factory = uow_factory
        self._runtime = RuntimeConfig.model_validate(runtime)
        self._embedding = embedding
        self._index = index
        self._clock = clock
        self._batch_size = batch_size

    def _check_reproducible(self, current: _Current) -> None:
        if current.stored_document.current_pipeline_fingerprint != pipeline_fingerprint(
            self._runtime
        ):
            raise CurrentRepairBlockedError("PIPELINE_NOT_REPRODUCIBLE")
        if self._runtime.part_i.representation_version != "representation-v1":
            raise CurrentRepairBlockedError("REPRESENTATION_NOT_REPRODUCIBLE")
        try:
            identity = EmbeddingModelIdentity.model_validate(self._embedding.identity)
        except (AttributeError, ValueError):
            raise CurrentRepairBlockedError("MODEL_IDENTITY_UNAVAILABLE") from None
        models = self._runtime.models
        if (
            identity.model_id != models.embedding_model_id
            or identity.model_revision != models.embedding_model_revision
            or identity.tokenizer_id != models.embedding_tokenizer_id
            or identity.tokenizer_revision != models.embedding_tokenizer_revision
            or identity.dimension != self._runtime.part_i.embedding_dimension
            or not _device_matches(identity.device, models.model_device)
        ):
            raise CurrentRepairBlockedError("MODEL_IDENTITY_MISMATCH")
        try:
            if (
                getattr(self._index, "embedding_dimension")
                != self._runtime.part_i.embedding_dimension
                or getattr(self._index, "analyzer")
                != self._runtime.part_i.bm25_analyzer
            ):
                raise CurrentRepairBlockedError("INDEX_IDENTITY_MISMATCH")
        except AttributeError:
            raise CurrentRepairBlockedError("INDEX_IDENTITY_UNAVAILABLE") from None
        profile = ChunkingProfile()
        for stored in current.stored_chunks:
            try:
                strategy = ChunkStrategyMetadataV1.model_validate(
                    stored.strategy_metadata
                )
            except (ValidationError, ValueError):
                raise CurrentRepairBlockedError("CHUNK_STRATEGY_INVALID") from None
            if (
                strategy.chunker_behavior_version
                != self._runtime.part_i.chunker_behavior_version
                or strategy.profile_fingerprint != profile.profile_fingerprint
            ):
                raise CurrentRepairBlockedError("CHUNK_STRATEGY_NOT_REPRODUCIBLE")

    async def _load(self, document_id: str) -> tuple[_Current, dict[str, str]]:
        async with self._uow_factory() as unit:
            stored = await unit.documents.get_by_id(document_id)
            if stored is None or stored.current_revision_id is None:
                raise InvariantViolationError
            revision = stored.current_revision_id
            elements = await unit.elements.list_for_revision(document_id, revision)
            stored_chunks = await unit.chunks.list_for_revision(document_id, revision)
            try:
                document = map_current_document_from_stored(
                    stored, elements, revision_id=revision
                )
                chunks = map_chunks_from_stored(stored_chunks)
                if (
                    validate_chunk_set(document, chunks).outcome
                    is ValidationOutcome.FAIL
                ):
                    raise CanonicalDataError
            except (CanonicalDataError, ValueError):
                raise CurrentRepairBlockedError("CURRENT_CANONICAL_INVALID") from None
        expected = expected_current_projection(
            document,
            chunks,
            representation_version=self._runtime.part_i.representation_version,
        )
        return (
            _Current(
                document=document,
                chunks=chunks,
                stored_document=stored,
                stored_chunks=stored_chunks,
            ),
            expected,
        )

    async def _actual(
        self, document_id: str, revision_id: str
    ) -> tuple[dict[str, str], bool]:
        rows: dict[str, str] = {}
        duplicate = False
        async with aclosing(
            self._index.iter_revision_projection(
                document_id, revision_id, batch_size=1_000
            )
        ) as iterator:
            async for row in iterator:
                if row.chunk_id in rows:
                    duplicate = True
                    continue
                rows[row.chunk_id] = _json(row.model_dump(mode="json"))
        return rows, duplicate

    async def _rebuild(
        self,
        document_id: str,
        revision_id: str,
        current: _Current,
        expected: dict[str, str],
    ) -> None:
        await self._index.delete_revision(document_id, revision_id)
        await self._index.wait_revision_visible(document_id, revision_id)
        if (await self._actual(document_id, revision_id))[0]:
            raise CurrentRepairBlockedError("CURRENT_INDEX_DELETE_INCOMPLETE")
        retrievable = tuple(
            chunk
            for chunk in current.chunks
            if chunk.retrieval_role is RetrievalRole.RETRIEVABLE
        )
        builder = RepresentationBuilder(
            RepresentationProfileV1(),
            representation_version=self._runtime.part_i.representation_version,
        )
        for start in range(0, len(retrievable), self._batch_size):
            batch = retrievable[start : start + self._batch_size]
            representations = tuple(builder.build(chunk) for chunk in batch)
            raw_vectors = await self._embedding.embed(
                tuple(item.dense_text for item in representations)
            )
            vectors = validate_embedding_output(
                raw_vectors,
                expected_count=len(batch),
                dimension=self._runtime.part_i.embedding_dimension,
            )
            records = tuple(
                map_retrieval_record_to_milvus(
                    build_retrieval_record(
                        current.document,
                        chunk,
                        representation,
                        vector,
                    )
                )
                for chunk, representation, vector in zip(
                    batch, representations, vectors, strict=True
                )
            )
            await self._index.insert_revision_batch(
                records, document_id=document_id, revision_id=revision_id
            )
        await self._index.wait_revision_visible(document_id, revision_id)
        repaired, duplicate = await self._actual(document_id, revision_id)
        if duplicate or repaired != expected:
            raise CurrentRepairBlockedError("CURRENT_INDEX_REPAIR_MISMATCH")

    async def reconcile_current(self, document_id: str) -> CurrentRepairOutcome:
        current, expected = await self._load(document_id)
        async with self._uow_factory() as unit:
            stored = await unit.documents.get_by_id(document_id)
        if stored is None or stored.current_revision_id is None:
            raise InvariantViolationError
        revision_id = stored.current_revision_id
        if revision_id != current.stored_document.current_revision_id:
            raise CurrentRepairBlockedError("CURRENT_REVISION_CHANGED")
        actual, duplicate = await self._actual(document_id, revision_id)
        if not duplicate and actual == expected:
            return CurrentRepairOutcome.CLEAN
        self._check_reproducible(current)
        await self._rebuild(document_id, revision_id, current, expected)
        return CurrentRepairOutcome.REPAIRED
