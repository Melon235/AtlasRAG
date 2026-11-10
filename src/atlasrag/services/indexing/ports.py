"""Narrow structural capabilities for durable candidate indexing."""

from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from datetime import datetime
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self

from atlasrag.domain.ingestion.chunks import ChunkRecord
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.source import CandidateBuildContext
from atlasrag.providers.index.models import MilvusChunkRecord
from atlasrag.providers.index.projection import MilvusRevisionProjection
from atlasrag.repositories.postgres.records import (
    FailedStage,
    RuntimeMetadata,
    StoredChunk,
    StoredDocument,
    StoredElement,
)
from atlasrag.services.ingestion.persistence import (
    CandidateChunkRepository,
    CandidateDocumentRepository,
    CandidateElementRepository,
)

Clock = Callable[[], datetime]


class IndexingDocuments(Protocol):
    def get_by_id(self, document_id: str) -> Awaitable[StoredDocument | None]: ...

    def put(self, document: StoredDocument) -> Awaitable[None]: ...


class IndexingElements(Protocol):
    def list_for_revision(
        self, document_id: str, revision_id: str
    ) -> Awaitable[tuple[StoredElement, ...]]: ...


class IndexingChunks(Protocol):
    def list_for_revision(
        self, document_id: str, revision_id: str
    ) -> Awaitable[tuple[StoredChunk, ...]]: ...


class IndexingUnitOfWork(Protocol):
    @property
    def documents(self) -> IndexingDocuments: ...

    @property
    def elements(self) -> IndexingElements: ...

    @property
    def chunks(self) -> IndexingChunks: ...

    def __aenter__(self) -> Awaitable[Self]: ...

    def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Awaitable[None]: ...

    def commit(self) -> Awaitable[None]: ...


IndexingUnitOfWorkFactory = Callable[[], IndexingUnitOfWork]


class PublicationRuntimeMetadata(Protocol):
    def set_query_cache_invalidation_required(
        self, required: bool
    ) -> Awaitable[RuntimeMetadata]: ...


class PublicationUnitOfWork(IndexingUnitOfWork, Protocol):
    @property
    def runtime_metadata(self) -> PublicationRuntimeMetadata: ...


PublicationUnitOfWorkFactory = Callable[[], PublicationUnitOfWork]


class CandidateIndex(Protocol):
    @property
    def embedding_dimension(self) -> int: ...

    @property
    def analyzer(self) -> str: ...

    def initialize(self) -> Awaitable[None]:
        """Validate actual collection compatibility before candidate mutations."""
        ...

    def delete_revision(self, document_id: str, revision_id: str) -> Awaitable[int]: ...

    def wait_revision_visible(
        self, document_id: str, revision_id: str
    ) -> Awaitable[None]: ...

    def iter_revision_projection(
        self, document_id: str, revision_id: str, *, batch_size: int
    ) -> AsyncGenerator[MilvusRevisionProjection, None]: ...

    def insert_revision_batch(
        self,
        records: Sequence[MilvusChunkRecord],
        *,
        document_id: str,
        revision_id: str,
    ) -> Awaitable[None]: ...


class BuildDocuments(IndexingDocuments, CandidateDocumentRepository, Protocol):
    pass


class BuildElements(IndexingElements, CandidateElementRepository, Protocol):
    pass


class BuildChunks(IndexingChunks, CandidateChunkRepository, Protocol):
    pass


class BuildUnitOfWork(PublicationUnitOfWork, Protocol):
    @property
    def documents(self) -> BuildDocuments: ...

    @property
    def elements(self) -> BuildElements: ...

    @property
    def chunks(self) -> BuildChunks: ...


class BuildFailureCode(StrEnum):
    """Closed, content-free Part I result codes; not public ErrorCode additions."""

    PARSE = "PARSE"
    CHUNK = "CHUNK"
    REPRESENTATION = "REPRESENTATION"
    EMBEDDING = "EMBEDDING"
    INDEX_WRITE = "INDEX_WRITE"
    INDEX_VERIFY = "INDEX_VERIFY"


class DocumentBuildSteps(Protocol):
    @property
    def context(self) -> CandidateBuildContext: ...

    async def checkpoint(self) -> None: ...
    async def prepare_candidate(self) -> None: ...
    async def parse_source(self) -> DocumentContainer: ...
    async def persist_parsed(self, document: DocumentContainer) -> None: ...
    async def build_chunks(
        self, document: DocumentContainer
    ) -> tuple[ChunkRecord, ...]: ...
    async def persist_chunked(
        self, document: DocumentContainer, chunks: tuple[ChunkRecord, ...]
    ) -> None: ...
    async def index_candidate(self) -> None: ...
    async def verify_candidate(self) -> None: ...
    async def publish_revision(self) -> None: ...
    async def record_failure(self, stage: FailedStage) -> None: ...
    async def discard_candidate(self) -> None: ...
