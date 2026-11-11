"""Durable-boundary reconciliation before a new sync plan."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path

from atlasrag.domain.errors import CanonicalDataError, InvariantViolationError
from atlasrag.domain.ingestion.chunks import ChunkRecord
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.source import SourceFile
from atlasrag.domain.ingestion.validation import ValidationOutcome
from atlasrag.repositories.postgres.records import (
    IngestionStatus,
    StoredDocument,
)
from atlasrag.services.ingestion.identity import content_hash
from atlasrag.services.ingestion.mapping import (
    DocumentParseMetadataEnvelopeV1,
    load_document_parse_metadata_envelope,
    map_chunks_from_stored,
    map_document_from_stored,
    replace_document_parse_metadata_slot,
    serialize_document_parse_metadata_envelope,
)
from atlasrag.services.ingestion.validation import validate_chunk_set
from atlasrag.services.sync.failures import RecoveryBlockedError, SyncFailure
from atlasrag.services.sync.models import (
    ReconciliationOutcome,
    ReconciliationResult,
    RecoveryBoundary,
    RecoveryTarget,
)
from atlasrag.services.sync.ports import SyncUnitOfWork, SyncUnitOfWorkFactory

Clock = Callable[[], datetime]


class _SourceState(Enum):
    AVAILABLE = auto()
    MISSING = auto()
    UNAVAILABLE = auto()


@dataclass(frozen=True)
class _DocState:
    document: StoredDocument
    envelope: DocumentParseMetadataEnvelopeV1
    source_state: _SourceState
    source_hash: str | None


def _utc(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return an aware timestamp")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("clock must return UTC")
    return value


def _replace(document: StoredDocument, **changes: object) -> StoredDocument:
    payload = document.model_dump(mode="python")
    payload.update(changes)
    return StoredDocument.model_validate(payload)


def _clear_building(
    document: StoredDocument,
    envelope: DocumentParseMetadataEnvelopeV1,
    *,
    timestamp: datetime,
) -> StoredDocument:
    envelope = replace_document_parse_metadata_slot(
        envelope, slot_name="building", slot=None
    )
    return _replace(
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
        parse_metadata=serialize_document_parse_metadata_envelope(envelope),
        updated_at=timestamp,
    )


class StartupReconciliationService:
    """Normalize durable boundaries; never plan, scan, or invent content."""

    def __init__(
        self,
        uow_factory: SyncUnitOfWorkFactory,
        *,
        knowledge_directory: Path,
        pipeline_fingerprint: str,
        clock: Clock,
        index: object | None = None,
        current_repair: object | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._knowledge_directory = knowledge_directory
        self._pipeline_fingerprint = pipeline_fingerprint
        self._clock = clock
        # These capabilities are deliberately optional for pure offline tests;
        # production composition provides the real provider and repair service.
        self._index = index
        self._current_repair = current_repair

    def _source_state(
        self, document: StoredDocument
    ) -> tuple[_SourceState, str | None]:
        root = self._knowledge_directory
        parts = document.relative_source_path.split("/")
        candidate = root.joinpath(*parts)
        if not candidate.exists():
            return _SourceState.MISSING, None
        try:
            source = SourceFile.from_knowledge_directory(
                root, document.relative_source_path
            )
            return _SourceState.AVAILABLE, content_hash(source)
        except (OSError, RuntimeError, ValueError):
            return _SourceState.UNAVAILABLE, None

    async def _read(self, unit: SyncUnitOfWork, document_id: str) -> _DocState | None:
        document = await unit.documents.get_by_id(document_id)
        if document is None:
            return None
        try:
            envelope = load_document_parse_metadata_envelope(document)
        except CanonicalDataError:
            raise RecoveryBlockedError("UNKNOWN_OR_INVALID_METADATA") from None
        state, digest = self._source_state(document)
        return _DocState(document, envelope, state, digest)

    async def _delete_milvus_revision(self, document_id: str, revision_id: str) -> None:
        if self._index is None:
            return
        delete = getattr(self._index, "delete_revision", None)
        wait = getattr(self._index, "wait_revision_visible", None)
        projection = getattr(self._index, "iter_revision_projection", None)
        if not callable(delete) or not callable(wait) or not callable(projection):
            raise InvariantViolationError
        await delete(document_id, revision_id)
        await wait(document_id, revision_id)
        async for _ in projection(document_id, revision_id, batch_size=1_000):
            raise RecoveryBlockedError("MILVUS_CANDIDATE_RESIDUE_UNREMOVABLE")

    async def _discard_candidate(
        self,
        *,
        document_id: str,
        revision_id: str,
        expected_status: IngestionStatus | tuple[IngestionStatus, ...],
    ) -> bool:
        await self._delete_milvus_revision(document_id, revision_id)
        async with self._uow_factory() as unit:
            state = await self._read(unit, document_id)
            if state is None:
                raise InvariantViolationError
            expected = (
                (expected_status,)
                if isinstance(expected_status, IngestionStatus)
                else expected_status
            )
            if state.document.ingestion_status not in expected:
                raise InvariantViolationError
            if state.document.building_revision_id != revision_id:
                raise InvariantViolationError
            await unit.elements.delete_revision(document_id, revision_id)
            await unit.chunks.delete_revision(document_id, revision_id)
            restored = _clear_building(
                state.document, state.envelope, timestamp=_utc(self._clock)
            )
            load_document_parse_metadata_envelope(restored)
            await unit.documents.put(restored)
            await unit.commit()
        return True

    async def _normalize_chunking(self, document_id: str, revision_id: str) -> None:
        timestamp = _utc(self._clock)
        async with self._uow_factory() as unit:
            state = await self._read(unit, document_id)
            if state is None:
                raise InvariantViolationError
            if (
                state.document.ingestion_status is not IngestionStatus.CHUNKING
                or state.document.building_revision_id != revision_id
            ):
                raise InvariantViolationError
            await unit.chunks.delete_revision(document_id, revision_id)
            restored = _replace(
                state.document,
                ingestion_status=IngestionStatus.PARSED,
                updated_at=timestamp,
            )
            load_document_parse_metadata_envelope(restored)
            await unit.documents.put(restored)
            await unit.commit()

    async def _normalize_indexing(self, document_id: str, revision_id: str) -> None:
        await self._delete_milvus_revision(document_id, revision_id)
        timestamp = _utc(self._clock)
        async with self._uow_factory() as unit:
            state = await self._read(unit, document_id)
            if state is None:
                raise InvariantViolationError
            if (
                state.document.ingestion_status is not IngestionStatus.INDEXING
                or state.document.building_revision_id != revision_id
            ):
                raise InvariantViolationError
            restored = _replace(
                state.document,
                ingestion_status=IngestionStatus.CHUNKED,
                updated_at=timestamp,
            )
            load_document_parse_metadata_envelope(restored)
            await unit.documents.put(restored)
            await unit.commit()

    async def _validate_elements(
        self, unit: SyncUnitOfWork, document: StoredDocument, revision_id: str
    ) -> DocumentContainer:
        elements = await unit.elements.list_for_revision(
            document.document_id, revision_id
        )
        try:
            return map_document_from_stored(document, elements, revision_id=revision_id)
        except CanonicalDataError:
            raise RecoveryBlockedError("PARSED_CANONICAL_INVALID") from None

    async def _validate_chunks(
        self,
        unit: SyncUnitOfWork,
        document: StoredDocument,
        canonical: DocumentContainer,
    ) -> tuple[ChunkRecord, ...]:
        assert document.building_revision_id is not None
        stored = await unit.chunks.list_for_revision(
            document.document_id, document.building_revision_id
        )
        try:
            chunks = map_chunks_from_stored(stored)
            if validate_chunk_set(canonical, chunks).outcome is ValidationOutcome.FAIL:
                raise CanonicalDataError
        except (CanonicalDataError, ValueError):
            raise RecoveryBlockedError("CHUNKED_CANONICAL_INVALID") from None
        return chunks

    def _target(
        self,
        document: StoredDocument,
        boundary: RecoveryBoundary,
    ) -> RecoveryTarget:
        assert document.building_revision_id is not None
        assert document.building_content_hash is not None
        assert document.building_pipeline_fingerprint is not None
        return RecoveryTarget(
            document_id=document.document_id,
            revision_id=document.building_revision_id,
            relative_source_path=document.relative_source_path,
            boundary=boundary,
            content_hash=document.building_content_hash,
            pipeline_fingerprint=document.building_pipeline_fingerprint,
        )

    async def reconcile(self) -> ReconciliationResult:
        try:
            root = self._knowledge_directory.resolve(strict=True)
        except (OSError, RuntimeError):
            return ReconciliationResult(
                outcome=ReconciliationOutcome.BLOCKED,
                blocked_reasons=("KNOWLEDGE_DIRECTORY_UNAVAILABLE",),
            )
        if not root.is_dir():
            return ReconciliationResult(
                outcome=ReconciliationOutcome.BLOCKED,
                blocked_reasons=("KNOWLEDGE_DIRECTORY_UNAVAILABLE",),
            )
        blocked: list[str] = []
        repaired: list[str] = []
        targets: list[RecoveryTarget] = []
        checked = 0

        async with self._uow_factory() as unit:
            documents = await unit.documents.list()
        for document in documents:
            checked += 1
            document_id = document.document_id
            building = document.building_revision_id
            try:
                if (
                    document.current_revision_id is not None
                    and building is not None
                    and document.current_revision_id == building
                ):
                    blocked.append("CURRENT_EQUALS_BUILDING")
                    continue
                async with self._uow_factory() as unit:
                    state = await self._read(unit, document_id)
                if state is None:
                    blocked.append("DOCUMENT_DISAPPEARED_DURING_RECONCILIATION")
                    continue
                current = state.document
                if state.source_state is _SourceState.UNAVAILABLE:
                    blocked.append("SOURCE_UNREADABLE")
                    continue
                if (
                    self._current_repair is not None
                    and current.current_revision_id is not None
                    and state.source_state is not _SourceState.MISSING
                ):
                    try:
                        repair = getattr(self._current_repair, "reconcile_current")
                        outcome = await repair(document_id)
                    except SyncFailure as error:
                        blocked.append(error.code)
                        continue
                    if getattr(outcome, "value", outcome) == "REPAIRED":
                        repaired.append(document_id)

                status = current.ingestion_status
                if status is IngestionStatus.READY:
                    if (
                        current.building_revision_id is not None
                        or state.envelope.building
                    ):
                        blocked.append("READY_WITH_BUILDING_OWNERSHIP")
                    continue

                if status in {IngestionStatus.RECEIVED, IngestionStatus.PARSING}:
                    if building is None:
                        if status is IngestionStatus.PARSING:
                            blocked.append("INCOMPLETE_CANDIDATE_WITHOUT_IDENTITY")
                        continue
                    await self._discard_candidate(
                        document_id=document_id,
                        revision_id=building,
                        expected_status=(status,),
                    )
                    repaired.append(document_id)
                    continue

                if status is IngestionStatus.FAILED:
                    if building is not None:
                        await self._delete_milvus_revision(document_id, building)
                    continue

                if building is None:
                    blocked.append("BUILDING_STATE_WITHOUT_IDENTITY")
                    continue

                matches_source = (
                    state.source_state is _SourceState.AVAILABLE
                    and state.source_hash == current.building_content_hash
                    and current.building_pipeline_fingerprint
                    == self._pipeline_fingerprint
                )
                if not matches_source:
                    if building is not None:
                        await self._discard_candidate(
                            document_id=document_id,
                            revision_id=building,
                            expected_status=(
                                IngestionStatus.PARSED,
                                IngestionStatus.CHUNKING,
                                IngestionStatus.CHUNKED,
                                IngestionStatus.INDEXING,
                            ),
                        )
                        repaired.append(document_id)
                    continue

                normalize_chunking = False
                async with self._uow_factory() as unit:
                    state = await self._read(unit, document_id)
                    if state is None:
                        raise InvariantViolationError
                    assert state.document.building_revision_id is not None
                    canonical = await self._validate_elements(
                        unit, state.document, state.document.building_revision_id
                    )
                    if status is IngestionStatus.CHUNKING:
                        normalize_chunking = True
                        boundary = RecoveryBoundary.PARSED
                    elif status is IngestionStatus.PARSED:
                        boundary = RecoveryBoundary.PARSED
                    else:
                        await self._validate_chunks(unit, state.document, canonical)
                        boundary = RecoveryBoundary.CHUNKED
                if normalize_chunking:
                    assert current.building_revision_id is not None
                    await self._normalize_chunking(
                        document_id, current.building_revision_id
                    )
                    repaired.append(document_id)
                if status is IngestionStatus.INDEXING:
                    assert current.building_revision_id is not None
                    await self._normalize_indexing(
                        document_id, current.building_revision_id
                    )
                    repaired.append(document_id)
                targets.append(self._target(current, boundary))
            except SyncFailure as error:
                blocked.append(error.code)

        if self._index is not None:
            try:
                await self._cleanup_non_current()
            except SyncFailure as error:
                blocked.append(error.code)

        blocked = sorted(set(blocked))
        if blocked:
            return ReconciliationResult(
                outcome=ReconciliationOutcome.BLOCKED,
                blocked_reasons=tuple(blocked),
                checked_documents=checked,
            )
        return ReconciliationResult(
            outcome=(
                ReconciliationOutcome.REPAIRED
                if repaired
                else ReconciliationOutcome.CLEAN
            ),
            recovery_targets=tuple(
                sorted(targets, key=lambda target: target.document_id)
            ),
            repaired_document_ids=tuple(sorted(set(repaired))),
            checked_documents=checked,
        )

    async def _cleanup_non_current(self) -> None:
        index = self._index
        assert index is not None
        iterator = getattr(index, "iter_all_projection", None)
        delete = getattr(index, "delete_revision", None)
        wait = getattr(index, "wait_revision_visible", None)
        scan_revision = getattr(index, "iter_revision_projection", None)
        if not all(callable(item) for item in (iterator, delete, wait, scan_revision)):
            raise InvariantViolationError
        assert (
            callable(iterator)
            and callable(delete)
            and callable(wait)
            and callable(scan_revision)
        )
        async with self._uow_factory() as unit:
            documents = await unit.documents.list()
        expected = {
            (document.document_id, document.current_revision_id)
            for document in documents
            if document.current_revision_id is not None
        }
        actual: set[tuple[str, str]] = set()
        async for row in iterator(batch_size=1_000):
            actual.add((row.document_id, row.revision_id))
        for document_id, revision_id in sorted(actual - expected):
            await delete(document_id, revision_id)
            await wait(document_id, revision_id)
            async for _ in scan_revision(document_id, revision_id, batch_size=1_000):
                raise RecoveryBlockedError("MILVUS_RESIDUE_UNREMOVABLE")
