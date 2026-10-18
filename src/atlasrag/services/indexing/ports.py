"""Narrow structural capabilities for durable candidate indexing."""

from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self

from atlasrag.providers.index.models import MilvusChunkRecord
from atlasrag.providers.index.projection import MilvusRevisionProjection
from atlasrag.repositories.postgres.records import (
    StoredChunk,
    StoredDocument,
    StoredElement,
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
