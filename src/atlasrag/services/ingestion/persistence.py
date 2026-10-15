"""Small explicit-commit capabilities for one Stage 3 candidate revision."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta
from types import TracebackType
from typing import Protocol, Self, TypeVar, cast

from atlasrag.domain.errors import InvariantViolationError
from atlasrag.domain.ingestion.chunks import ChunkRecord
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.source import (
    CandidateBuildContext,
    CanonicalSource,
)
from atlasrag.domain.ingestion.validation import ValidationOutcome
from atlasrag.repositories.postgres.records import (
    FailedStage,
    IngestionStatus,
    JsonObject,
    StoredChunk,
    StoredDocument,
    StoredElement,
)
from atlasrag.services.ingestion.mapping import (
    DocumentParseMetadataEnvelopeV1,
    build_document_parse_metadata_slot,
    empty_document_parse_metadata_envelope,
    load_document_parse_metadata_envelope,
    map_chunks_to_stored,
    map_document_from_stored,
    map_elements_to_stored,
    replace_document_parse_metadata_slot,
    serialize_document_parse_metadata_envelope,
)
from atlasrag.services.ingestion.validation import (
    validate_chunk_set,
    validate_parsed_document,
)

Clock = Callable[[], datetime]


class CandidateDocumentRepository(Protocol):
    """Only document operations used by candidate capabilities."""

    def put(self, document: StoredDocument) -> Awaitable[None]: ...

    def get_by_id(self, document_id: str) -> Awaitable[StoredDocument | None]: ...

    def get_by_relative_source_path(
        self, relative_source_path: str
    ) -> Awaitable[StoredDocument | None]: ...


class CandidateElementRepository(Protocol):
    """Only exact-set element operations used by candidate capabilities."""

    def replace_revision_set(
        self,
        document_id: str,
        revision_id: str,
        elements: Iterable[StoredElement],
    ) -> Awaitable[None]: ...

    def delete_revision(self, document_id: str, revision_id: str) -> Awaitable[int]: ...

    def list_for_revision(
        self,
        document_id: str,
        revision_id: str,
    ) -> Awaitable[tuple[StoredElement, ...]]: ...


class CandidateChunkRepository(Protocol):
    """Only exact-set chunk operations used by candidate capabilities."""

    def replace_revision_set(
        self,
        document_id: str,
        revision_id: str,
        chunks: Iterable[StoredChunk],
    ) -> Awaitable[None]: ...

    def delete_revision(self, document_id: str, revision_id: str) -> Awaitable[int]: ...


class CandidateUnitOfWork(Protocol):
    """Structural subset implemented by the frozen PostgresUnitOfWork."""

    @property
    def documents(self) -> CandidateDocumentRepository: ...

    @property
    def elements(self) -> CandidateElementRepository: ...

    @property
    def chunks(self) -> CandidateChunkRepository: ...

    def __aenter__(self) -> Awaitable[Self]: ...

    def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Awaitable[None]: ...

    def commit(self) -> Awaitable[None]: ...


_UnitOfWorkT_co = TypeVar(
    "_UnitOfWorkT_co",
    bound=CandidateUnitOfWork,
    covariant=True,
)


class UnitOfWorkFactory(Protocol[_UnitOfWorkT_co]):
    """Construct a fresh Unit of Work for exactly one capability call."""

    def __call__(self) -> _UnitOfWorkT_co: ...


def _canonical_context(context: CandidateBuildContext) -> CandidateBuildContext:
    if not isinstance(context, CandidateBuildContext):
        raise TypeError("context must be a CandidateBuildContext")
    return CandidateBuildContext.model_validate(context)


def _utc_timestamp(clock: Clock) -> datetime:
    timestamp = clock()
    if (
        not isinstance(timestamp, datetime)
        or timestamp.tzinfo is None
        or timestamp.utcoffset() != timedelta(0)
    ):
        raise ValueError("clock must return an aware UTC timestamp")
    return timestamp


def _replace_document(
    document: StoredDocument,
    **changes: object,
) -> StoredDocument:
    payload: dict[str, object] = document.model_dump(mode="python")
    payload.update(changes)
    return StoredDocument.model_validate(payload)


def _serialize_envelope(
    envelope: DocumentParseMetadataEnvelopeV1,
) -> JsonObject:
    return cast(JsonObject, serialize_document_parse_metadata_envelope(envelope))


def _require_source_scope(
    document: StoredDocument,
    context: CandidateBuildContext,
) -> None:
    source = context.source
    if (
        document.document_id != context.document_id
        or document.relative_source_path != source.relative_source_path
        or document.file_name != source.file_name
        or document.source_type is not source.source_type
    ):
        raise InvariantViolationError


def _require_building_triple(
    document: StoredDocument,
    context: CandidateBuildContext,
) -> None:
    _require_source_scope(document, context)
    if (
        document.observed_content_hash != context.content_hash
        or document.building_revision_id != context.revision_id
        or document.building_content_hash != context.content_hash
        or document.building_pipeline_fingerprint != context.pipeline_fingerprint
    ):
        raise InvariantViolationError


async def _load_candidate(
    repository: CandidateDocumentRepository,
    context: CandidateBuildContext,
) -> tuple[StoredDocument, DocumentParseMetadataEnvelopeV1]:
    document = await repository.get_by_id(context.document_id)
    if document is None:
        raise InvariantViolationError
    envelope = load_document_parse_metadata_envelope(document)
    _require_building_triple(document, context)
    return document, envelope


def _require_document_scope(
    document: DocumentContainer,
    context: CandidateBuildContext,
) -> DocumentContainer:
    canonical = DocumentContainer.model_validate(document)
    if (
        canonical.document_id != context.document_id
        or canonical.revision_id != context.revision_id
        or canonical.source != CanonicalSource.from_source_file(context.source)
    ):
        raise InvariantViolationError
    return canonical


def _validated_document(
    document: DocumentContainer,
    context: CandidateBuildContext,
) -> DocumentContainer:
    validation = validate_parsed_document(document)
    if validation.outcome is ValidationOutcome.FAIL:
        raise InvariantViolationError
    return _require_document_scope(document, context)


async def prepare_candidate(
    uow_factory: UnitOfWorkFactory[CandidateUnitOfWork],
    context: CandidateBuildContext,
    *,
    clock: Clock,
) -> StoredDocument:
    """Create or reset one exact candidate while retaining the last-good triple."""
    canonical = _canonical_context(context)
    timestamp = _utc_timestamp(clock)
    source = canonical.source

    async with uow_factory() as unit_of_work:
        existing = await unit_of_work.documents.get_by_relative_source_path(
            source.relative_source_path
        )
        if existing is None:
            conflicting_identity = await unit_of_work.documents.get_by_id(
                canonical.document_id
            )
            if conflicting_identity is not None:
                raise InvariantViolationError
            envelope = empty_document_parse_metadata_envelope()
            candidate = StoredDocument(
                document_id=canonical.document_id,
                file_name=source.file_name,
                relative_source_path=source.relative_source_path,
                source_type=source.source_type,
                observed_content_hash=canonical.content_hash,
                building_revision_id=canonical.revision_id,
                building_content_hash=canonical.content_hash,
                building_pipeline_fingerprint=canonical.pipeline_fingerprint,
                ingestion_status=IngestionStatus.RECEIVED,
                parse_metadata=_serialize_envelope(envelope),
                created_at=timestamp,
                updated_at=timestamp,
            )
        else:
            envelope = load_document_parse_metadata_envelope(existing)
            _require_source_scope(existing, canonical)
            if existing.current_revision_id == canonical.revision_id:
                raise InvariantViolationError
            if (
                existing.building_revision_id is not None
                and existing.building_revision_id != canonical.revision_id
            ):
                raise InvariantViolationError
            if existing.building_revision_id is not None:
                _require_building_triple(existing, canonical)
            envelope = replace_document_parse_metadata_slot(
                envelope,
                slot_name="building",
                slot=None,
            )
            candidate = _replace_document(
                existing,
                file_name=source.file_name,
                relative_source_path=source.relative_source_path,
                source_type=source.source_type,
                observed_content_hash=canonical.content_hash,
                building_revision_id=canonical.revision_id,
                building_content_hash=canonical.content_hash,
                building_pipeline_fingerprint=canonical.pipeline_fingerprint,
                ingestion_status=IngestionStatus.RECEIVED,
                failed_stage=None,
                parse_metadata=_serialize_envelope(envelope),
                updated_at=timestamp,
            )

        load_document_parse_metadata_envelope(candidate)
        await unit_of_work.documents.put(candidate)
        await unit_of_work.commit()
    return candidate


async def _transition(
    uow_factory: UnitOfWorkFactory[CandidateUnitOfWork],
    context: CandidateBuildContext,
    *,
    expected: IngestionStatus,
    target: IngestionStatus,
    timestamp: datetime,
) -> StoredDocument:
    async with uow_factory() as unit_of_work:
        document, _ = await _load_candidate(unit_of_work.documents, context)
        if document.ingestion_status is not expected:
            raise InvariantViolationError
        updated = _replace_document(
            document,
            ingestion_status=target,
            failed_stage=None,
            updated_at=timestamp,
        )
        load_document_parse_metadata_envelope(updated)
        await unit_of_work.documents.put(updated)
        await unit_of_work.commit()
    return updated


async def mark_parsing(
    uow_factory: UnitOfWorkFactory[CandidateUnitOfWork],
    context: CandidateBuildContext,
    *,
    clock: Clock,
) -> StoredDocument:
    """Apply the exact ``RECEIVED -> PARSING`` candidate transition."""
    canonical = _canonical_context(context)
    return await _transition(
        uow_factory,
        canonical,
        expected=IngestionStatus.RECEIVED,
        target=IngestionStatus.PARSING,
        timestamp=_utc_timestamp(clock),
    )


async def persist_parsed(
    uow_factory: UnitOfWorkFactory[CandidateUnitOfWork],
    context: CandidateBuildContext,
    document: DocumentContainer,
    *,
    clock: Clock,
) -> StoredDocument:
    """Atomically replace candidate elements and establish ``PARSED``."""
    canonical_context = _canonical_context(context)
    canonical_document = _validated_document(document, canonical_context)
    timestamp = _utc_timestamp(clock)
    building_slot = build_document_parse_metadata_slot(canonical_document)
    elements = map_elements_to_stored(
        canonical_document.elements,
        created_at=timestamp,
    )

    async with uow_factory() as unit_of_work:
        stored_document, envelope = await _load_candidate(
            unit_of_work.documents,
            canonical_context,
        )
        if stored_document.ingestion_status not in {
            IngestionStatus.PARSING,
            IngestionStatus.PARSED,
        }:
            raise InvariantViolationError
        envelope = replace_document_parse_metadata_slot(
            envelope,
            slot_name="building",
            slot=building_slot,
        )
        updated = _replace_document(
            stored_document,
            ingestion_status=IngestionStatus.PARSED,
            failed_stage=None,
            parse_metadata=_serialize_envelope(envelope),
            updated_at=timestamp,
        )
        load_document_parse_metadata_envelope(updated)
        await unit_of_work.elements.replace_revision_set(
            canonical_context.document_id,
            canonical_context.revision_id,
            elements,
        )
        await unit_of_work.documents.put(updated)
        await unit_of_work.commit()
    return updated


async def mark_chunking(
    uow_factory: UnitOfWorkFactory[CandidateUnitOfWork],
    context: CandidateBuildContext,
    *,
    clock: Clock,
) -> StoredDocument:
    """Apply the exact ``PARSED -> CHUNKING`` candidate transition."""
    canonical = _canonical_context(context)
    return await _transition(
        uow_factory,
        canonical,
        expected=IngestionStatus.PARSED,
        target=IngestionStatus.CHUNKING,
        timestamp=_utc_timestamp(clock),
    )


async def persist_chunked(
    uow_factory: UnitOfWorkFactory[CandidateUnitOfWork],
    context: CandidateBuildContext,
    document: DocumentContainer,
    chunks: Iterable[ChunkRecord],
    *,
    chunker_behavior_version: str,
    profile_fingerprint: str,
    clock: Clock,
) -> StoredDocument:
    """Atomically replace candidate chunks and establish ``CHUNKED``."""
    canonical_context = _canonical_context(context)
    canonical_document = _validated_document(document, canonical_context)
    canonical_chunks = tuple(chunks)
    validation = validate_chunk_set(canonical_document, canonical_chunks)
    if validation.outcome is ValidationOutcome.FAIL:
        raise InvariantViolationError
    timestamp = _utc_timestamp(clock)
    stored_chunks = map_chunks_to_stored(
        canonical_chunks,
        chunker_behavior_version=chunker_behavior_version,
        profile_fingerprint=profile_fingerprint,
        created_at=timestamp,
    )

    async with uow_factory() as unit_of_work:
        stored_document, _ = await _load_candidate(
            unit_of_work.documents,
            canonical_context,
        )
        if stored_document.ingestion_status not in {
            IngestionStatus.CHUNKING,
            IngestionStatus.CHUNKED,
        }:
            raise InvariantViolationError
        stored_elements = await unit_of_work.elements.list_for_revision(
            canonical_context.document_id,
            canonical_context.revision_id,
        )
        persisted_document = map_document_from_stored(
            stored_document,
            stored_elements,
            revision_id=canonical_context.revision_id,
        )
        if persisted_document != canonical_document:
            raise InvariantViolationError
        persisted_validation = validate_chunk_set(
            persisted_document,
            canonical_chunks,
        )
        if persisted_validation.outcome is ValidationOutcome.FAIL:
            raise InvariantViolationError
        updated = _replace_document(
            stored_document,
            ingestion_status=IngestionStatus.CHUNKED,
            failed_stage=None,
            updated_at=timestamp,
        )
        load_document_parse_metadata_envelope(updated)
        await unit_of_work.chunks.replace_revision_set(
            canonical_context.document_id,
            canonical_context.revision_id,
            stored_chunks,
        )
        await unit_of_work.documents.put(updated)
        await unit_of_work.commit()
    return updated


async def record_failure(
    uow_factory: UnitOfWorkFactory[CandidateUnitOfWork],
    context: CandidateBuildContext,
    failed_stage: FailedStage,
    *,
    clock: Clock,
) -> StoredDocument:
    """Record one explicitly classified PARSE or CHUNK business failure."""
    canonical = _canonical_context(context)
    allowed_origins = {
        FailedStage.PARSE: IngestionStatus.PARSING,
        FailedStage.CHUNK: IngestionStatus.CHUNKING,
    }
    if not isinstance(failed_stage, FailedStage) or failed_stage not in allowed_origins:
        raise InvariantViolationError
    timestamp = _utc_timestamp(clock)

    async with uow_factory() as unit_of_work:
        document, _ = await _load_candidate(unit_of_work.documents, canonical)
        if document.ingestion_status is not allowed_origins[failed_stage]:
            raise InvariantViolationError
        failed = _replace_document(
            document,
            ingestion_status=IngestionStatus.FAILED,
            failed_stage=failed_stage,
            updated_at=timestamp,
        )
        load_document_parse_metadata_envelope(failed)
        await unit_of_work.documents.put(failed)
        await unit_of_work.commit()
    return failed


async def discard_candidate(
    uow_factory: UnitOfWorkFactory[CandidateUnitOfWork],
    context: CandidateBuildContext,
    *,
    clock: Clock,
) -> StoredDocument:
    """Delete candidate outputs while retaining the document identity/current view."""
    canonical = _canonical_context(context)
    timestamp = _utc_timestamp(clock)

    async with uow_factory() as unit_of_work:
        document, envelope = await _load_candidate(
            unit_of_work.documents,
            canonical,
        )
        envelope = replace_document_parse_metadata_slot(
            envelope,
            slot_name="building",
            slot=None,
        )
        restored = _replace_document(
            document,
            building_revision_id=None,
            building_content_hash=None,
            building_pipeline_fingerprint=None,
            ingestion_status=(
                IngestionStatus.READY
                if document.current_revision_id is not None
                else IngestionStatus.RECEIVED
            ),
            failed_stage=None,
            parse_metadata=_serialize_envelope(envelope),
            updated_at=timestamp,
        )
        load_document_parse_metadata_envelope(restored)
        await unit_of_work.elements.delete_revision(
            canonical.document_id,
            canonical.revision_id,
        )
        await unit_of_work.chunks.delete_revision(
            canonical.document_id,
            canonical.revision_id,
        )
        await unit_of_work.documents.put(restored)
        await unit_of_work.commit()
    return restored
