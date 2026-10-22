"""Per-invocation document build capabilities outside graph orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import InvariantViolationError
from atlasrag.domain.indexing.failures import (
    EmbeddingInputLimitError,
    EmbeddingOutputError,
    IndexVerificationError,
    RepresentationInputLimitError,
)
from atlasrag.domain.ingestion.chunks import ChunkRecord
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.failures import ChunkInputLimitError
from atlasrag.domain.ingestion.source import CandidateBuildContext
from atlasrag.domain.ingestion.validation import ValidationOutcome
from atlasrag.providers.embedding.base import EmbeddingProvider
from atlasrag.providers.index.projection import IndexWriteRejectedError
from atlasrag.providers.parsing.routing import ParserRouter
from atlasrag.repositories.postgres.records import FailedStage
from atlasrag.services.indexing.failure import (
    classify_index_failure,
    record_index_failure,
)
from atlasrag.services.indexing.ports import (
    BuildFailureCode,
    BuildUnitOfWork,
    CandidateIndex,
    Clock,
)
from atlasrag.services.indexing.publication import publish_revision
from atlasrag.services.indexing.verification import CandidateVerificationAttempt
from atlasrag.services.ingestion import persistence
from atlasrag.services.ingestion.chunking import ChunkingProfile, build_chunks
from atlasrag.services.ingestion.source_guard import ensure_source_unchanged
from atlasrag.services.ingestion.validation import (
    validate_chunk_set,
    validate_parsed_document,
)

_T = TypeVar("_T")
_IndexFailure = (
    RepresentationInputLimitError
    | EmbeddingInputLimitError
    | EmbeddingOutputError
    | IndexWriteRejectedError
    | IndexVerificationError
)
_INDEX_FAILURES = (
    RepresentationInputLimitError,
    EmbeddingInputLimitError,
    EmbeddingOutputError,
    IndexWriteRejectedError,
    IndexVerificationError,
)


async def finish_task(task: asyncio.Future[_T]) -> _T:
    """Settle already-issued work despite repeated cancellation of this waiter."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def settled_operation(operation: Awaitable[_T]) -> _T:
    """Cancel at this operation boundary only, after issued work has settled."""
    task = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as primary:
        try:
            await finish_task(task)
        except BaseException as cleanup:
            if cleanup is not primary:
                primary.add_note(f"operation settlement: {type(cleanup).__name__}")
        raise primary


@dataclass(frozen=True)
class BuildResources:
    """Process-owned shared providers; no initialization or global registry here."""

    config: RuntimeConfig
    uow_factory: Callable[[], BuildUnitOfWork]
    parsers: ParserRouter
    embedding: EmbeddingProvider
    index: CandidateIndex
    clock: Clock
    chunking_profile: ChunkingProfile = field(default_factory=ChunkingProfile)
    index_batch_size: int = 1
    verification_batch_size: int = 1000


class BuildBusinessFailure(Exception):
    """A positively classified, content-free routing signal for the build graph."""

    def __init__(self, stage: FailedStage, reason: BuildFailureCode) -> None:
        self.stage = stage
        self.reason = reason
        super().__init__(stage, reason)


class DocumentBuildServices:
    """Fresh per invocation, with private failure/proof and shared provider owners."""

    def __init__(
        self,
        resources: BuildResources,
        context: CandidateBuildContext,
        *,
        checkpoint: Callable[[], Awaitable[None]],
    ) -> None:
        self.resources = resources
        self.context = CandidateBuildContext.model_validate(context)
        self._checkpoint = checkpoint
        self._pending_error: _IndexFailure | None = None
        self._attempt = CandidateVerificationAttempt(
            resources.uow_factory,
            self.context,
            config=resources.config,
            index=resources.index,
        )

    def invalidate(self) -> None:
        self._attempt.invalidate_verified()
        self._pending_error = None

    async def checkpoint(self) -> None:
        try:
            await self._checkpoint()
        except BaseException:
            self.invalidate()
            raise

    async def prepare_candidate(self) -> None:
        await persistence.prepare_candidate(
            self.resources.uow_factory, self.context, clock=self.resources.clock
        )

    async def parse_source(self) -> DocumentContainer:
        await persistence.mark_parsing(
            self.resources.uow_factory, self.context, clock=self.resources.clock
        )
        parser = self.resources.parsers.parser_for(self.context.source)
        # Frozen Docling/TXT may issue unshielded threads; MinerU keeps its
        # required invoking thread. Cancellation stops before the next step.
        document = await settled_operation(
            parser.parse(self.context.source, self.context)
        )
        await settled_operation(
            asyncio.to_thread(
                ensure_source_unchanged, self.context.source, self.context.content_hash
            )
        )
        if validate_parsed_document(document).outcome is ValidationOutcome.FAIL:
            raise BuildBusinessFailure(FailedStage.PARSE, BuildFailureCode.PARSE)
        return document

    async def persist_parsed(self, document: DocumentContainer) -> None:
        await persistence.persist_parsed(
            self.resources.uow_factory,
            self.context,
            document,
            clock=self.resources.clock,
        )

    async def build_chunks(
        self, document: DocumentContainer
    ) -> tuple[ChunkRecord, ...]:
        await persistence.mark_chunking(
            self.resources.uow_factory, self.context, clock=self.resources.clock
        )
        try:
            chunks = await settled_operation(
                asyncio.to_thread(
                    build_chunks, document, self.resources.chunking_profile
                )
            )
        except ChunkInputLimitError:
            raise BuildBusinessFailure(
                FailedStage.CHUNK, BuildFailureCode.CHUNK
            ) from None
        if validate_chunk_set(document, chunks).outcome is ValidationOutcome.FAIL:
            raise BuildBusinessFailure(FailedStage.CHUNK, BuildFailureCode.CHUNK)
        return chunks

    async def persist_chunked(
        self, document: DocumentContainer, chunks: tuple[ChunkRecord, ...]
    ) -> None:
        await persistence.persist_chunked(
            self.resources.uow_factory,
            self.context,
            document,
            chunks,
            chunker_behavior_version=self.resources.config.part_i.chunker_behavior_version,
            profile_fingerprint=self.resources.chunking_profile.profile_fingerprint,
            clock=self.resources.clock,
        )

    def _classified(self, error: _IndexFailure) -> BuildBusinessFailure:
        stage = classify_index_failure(error)
        if stage is None:
            raise InvariantViolationError
        self._pending_error = error
        return BuildBusinessFailure(stage, BuildFailureCode(stage.value))

    async def index_candidate(self) -> None:
        try:
            await self._attempt.index(
                embedding=self.resources.embedding,
                clock=self.resources.clock,
                batch_size=self.resources.index_batch_size,
            )
        except _INDEX_FAILURES as error:
            raise self._classified(error) from None

    async def verify_candidate(self) -> None:
        try:
            await self._attempt.verify(
                batch_size=self.resources.verification_batch_size
            )
        except IndexVerificationError as error:
            raise self._classified(error) from None

    async def publish_revision(self) -> None:
        await publish_revision(
            self.resources.uow_factory,
            self.context,
            verification=self._attempt,
            clock=self.resources.clock,
        )

    async def record_failure(self, stage: FailedStage) -> None:
        try:
            if stage in {FailedStage.PARSE, FailedStage.CHUNK}:
                await persistence.record_failure(
                    self.resources.uow_factory,
                    self.context,
                    stage,
                    clock=self.resources.clock,
                )
            else:
                error = self._pending_error
                if error is None or classify_index_failure(error) is not stage:
                    raise InvariantViolationError
                await record_index_failure(
                    self.resources.uow_factory,
                    self.context,
                    error,
                    clock=self.resources.clock,
                )
        finally:
            self.invalidate()

    async def discard_candidate(self) -> None:
        try:
            await persistence.discard_candidate(
                self.resources.uow_factory, self.context, clock=self.resources.clock
            )
        finally:
            self.invalidate()
