"""Resume durable Stage 3/4 boundaries without repeating parser work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from datetime import datetime

from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import (
    CanonicalDataError,
    ConfigurationError,
    InvariantViolationError,
    SourceMutationError,
)
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.failures import ChunkInputLimitError
from atlasrag.domain.ingestion.source import CandidateBuildContext, SourceFile
from atlasrag.domain.ingestion.validation import ValidationOutcome
from atlasrag.providers.embedding.base import EmbeddingProvider
from atlasrag.repositories.postgres.records import (
    FailedStage,
    IngestionStatus,
    StoredDocument,
)
from atlasrag.services.indexing.build import BuildBusinessFailure
from atlasrag.services.indexing.failure import (
    classify_index_failure,
    record_index_failure,
)
from atlasrag.services.indexing.ports import BuildFailureCode, CandidateIndex
from atlasrag.services.indexing.publication import publish_revision
from atlasrag.services.indexing.verification import CandidateVerificationAttempt
from atlasrag.services.ingestion import persistence
from atlasrag.services.ingestion.chunking import ChunkingProfile, build_chunks
from atlasrag.services.ingestion.identity import content_hash
from atlasrag.services.ingestion.mapping import (
    load_document_parse_metadata_envelope,
    map_document_from_stored,
    replace_document_parse_metadata_slot,
    serialize_document_parse_metadata_envelope,
)
from atlasrag.services.ingestion.validation import validate_chunk_set
from atlasrag.services.sync.models import RecoveryBoundary, RecoveryTarget
from atlasrag.services.sync.ports import SyncUnitOfWorkFactory

Clock = Callable[[], datetime]


async def _noop_checkpoint() -> None:
    return None


def _replace_document(document: StoredDocument, **changes: object) -> StoredDocument:
    payload = document.model_dump(mode="python")
    payload.update(changes)
    return StoredDocument.model_validate(payload)


class DocumentRecoveryService:
    """One invocation's resume/cleanup capability; caller owns document lease."""

    def __init__(
        self,
        uow_factory: SyncUnitOfWorkFactory,
        *,
        runtime: RuntimeConfig,
        embedding: EmbeddingProvider,
        index: CandidateIndex,
        clock: Clock,
        batch_size: int = 1,
        verification_batch_size: int = 1_000,
    ) -> None:
        self._uow_factory = uow_factory
        self._runtime = RuntimeConfig.model_validate(runtime)
        self._embedding = embedding
        self._index = index
        self._clock = clock
        self._batch_size = batch_size
        self._verification_batch_size = verification_batch_size

    def _context(
        self, target: RecoveryTarget, source: SourceFile
    ) -> CandidateBuildContext:
        if pipeline_fingerprint(self._runtime) != target.pipeline_fingerprint:
            raise ConfigurationError
        return CandidateBuildContext(
            source=source,
            document_id=target.document_id,
            content_hash=target.content_hash,
            pipeline_fingerprint=target.pipeline_fingerprint,
            revision_id=target.revision_id,
        )

    def _attempt(self, context: CandidateBuildContext) -> CandidateVerificationAttempt:
        return CandidateVerificationAttempt(
            self._uow_factory,
            context,
            config=self._runtime,
            index=self._index,
        )

    async def _verify_source(
        self, target: RecoveryTarget, source: SourceFile, context: CandidateBuildContext
    ) -> None:
        try:
            actual = await asyncio.to_thread(content_hash, source)
        except (OSError, ValueError):
            raise SourceMutationError from None
        if actual != target.content_hash:
            await persistence.discard_candidate(
                self._uow_factory,  # type: ignore[arg-type]
                context,
                clock=self._clock,
            )
            raise SourceMutationError

    async def _load_parsed(self, context: CandidateBuildContext) -> DocumentContainer:
        async with self._uow_factory() as unit:
            stored = await unit.documents.get_by_id(context.document_id)
            if (
                stored is None
                or stored.ingestion_status is not IngestionStatus.PARSED
                or stored.building_revision_id != context.revision_id
            ):
                raise InvariantViolationError
            elements = await unit.elements.list_for_revision(
                context.document_id, context.revision_id
            )
        try:
            return map_document_from_stored(
                stored, elements, revision_id=context.revision_id
            )
        except CanonicalDataError:
            raise InvariantViolationError from None

    async def _index_verify_publish(
        self,
        context: CandidateBuildContext,
        attempt: CandidateVerificationAttempt,
        checkpoint: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            await checkpoint()
            await attempt.index(
                embedding=self._embedding,
                clock=self._clock,
                batch_size=self._batch_size,
            )
            await checkpoint()
            await attempt.verify(batch_size=self._verification_batch_size)
            await checkpoint()
            await publish_revision(
                self._uow_factory,
                context,
                verification=attempt,
                clock=self._clock,
            )
        except BaseException as error:
            stage = classify_index_failure(error)
            if stage is None:
                raise
            await record_index_failure(
                self._uow_factory,
                context,
                error,
                clock=self._clock,
            )
            raise BuildBusinessFailure(stage, BuildFailureCode(stage.value)) from None

    async def resume(
        self,
        target: RecoveryTarget,
        source: SourceFile,
        *,
        checkpoint: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        check = checkpoint or _noop_checkpoint
        context = self._context(target, source)
        await self._verify_source(target, source, context)
        await check()
        if target.boundary is RecoveryBoundary.PARSED:
            document = await self._load_parsed(context)
            await check()
            await persistence.mark_chunking(
                self._uow_factory,  # type: ignore[arg-type]
                context,
                clock=self._clock,
            )
            profile = ChunkingProfile()
            try:
                chunks = await asyncio.to_thread(build_chunks, document, profile)
            except ChunkInputLimitError:
                await persistence.record_failure(
                    self._uow_factory,  # type: ignore[arg-type]
                    context,
                    FailedStage.CHUNK,
                    clock=self._clock,
                )
                raise BuildBusinessFailure(
                    FailedStage.CHUNK, BuildFailureCode.CHUNK
                ) from None
            if validate_chunk_set(document, chunks).outcome is ValidationOutcome.FAIL:
                await persistence.record_failure(
                    self._uow_factory,  # type: ignore[arg-type]
                    context,
                    FailedStage.CHUNK,
                    clock=self._clock,
                )
                raise BuildBusinessFailure(
                    FailedStage.CHUNK, BuildFailureCode.CHUNK
                ) from None
            await check()
            await persistence.persist_chunked(
                self._uow_factory,  # type: ignore[arg-type]
                context,
                document,
                chunks,
                chunker_behavior_version=self._runtime.part_i.chunker_behavior_version,
                profile_fingerprint=profile.profile_fingerprint,
                clock=self._clock,
            )
        attempt = self._attempt(context)
        await self._index_verify_publish(context, attempt, check)

    async def cleanup_non_current(self, document_id: str) -> bool:
        """Remove searchable revisions other than the published current one."""
        async with self._uow_factory() as unit:
            stored = await unit.documents.get_by_id(document_id)
        if stored is None:
            return True
        current = stored.current_revision_id
        if current is None:
            return True
        projection = getattr(self._index, "iter_document_projection")
        revisions: set[str] = set()
        async with aclosing(projection(document_id, batch_size=1_000)) as rows:
            async for row in rows:
                if row.revision_id != current:
                    revisions.add(row.revision_id)
        for revision in sorted(revisions):
            if not await self.cleanup_candidate(document_id, revision):
                return False
        return True

    async def cleanup_candidate(self, document_id: str, revision_id: str) -> bool:
        delete = getattr(self._index, "delete_revision")
        wait = getattr(self._index, "wait_revision_visible")
        projection = getattr(self._index, "iter_revision_projection")
        await delete(document_id, revision_id)
        await wait(document_id, revision_id)
        async with aclosing(
            projection(document_id, revision_id, batch_size=1_000)
        ) as rows:
            async for _ in rows:
                return False
        return True

    async def reset_stale_candidate(
        self, document_id: str, target_revision_id: str
    ) -> None:
        """Drop a stale building candidate before a planner-requested full retry."""
        async with self._uow_factory() as unit:
            stored = await unit.documents.get_by_id(document_id)
        if stored is None:
            return
        building = stored.building_revision_id
        if building is None or building == target_revision_id:
            return
        await self.cleanup_candidate(document_id, building)
        async with self._uow_factory() as unit:
            stored = await unit.documents.get_by_id(document_id)
            if stored is None:
                return
            await unit.chunks.delete_revision(document_id, building)
            await unit.elements.delete_revision(document_id, building)
            envelope = load_document_parse_metadata_envelope(stored)
            envelope = replace_document_parse_metadata_slot(
                envelope, slot_name="building", slot=None
            )
            restored = _replace_document(
                stored,
                building_revision_id=None,
                building_content_hash=None,
                building_pipeline_fingerprint=None,
                ingestion_status=(
                    IngestionStatus.READY
                    if stored.current_revision_id is not None
                    else IngestionStatus.RECEIVED
                ),
                failed_stage=None,
                parse_metadata=serialize_document_parse_metadata_envelope(envelope),
                updated_at=self._clock(),
            )
            load_document_parse_metadata_envelope(restored)
            await unit.documents.put(restored)
            await unit.commit()
