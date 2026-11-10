"""Narrow structural PostgreSQL capabilities used by Stage 5 services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Protocol, Self

from atlasrag.repositories.postgres.records import (
    FailedStage,
    RuntimeMetadata,
    StoredChunk,
    StoredDocument,
    StoredElement,
)
from atlasrag.services.indexing.ports import BuildFailureCode


class SyncDocuments(Protocol):
    def list(self) -> Awaitable[tuple[StoredDocument, ...]]: ...

    def get_by_id(self, document_id: str) -> Awaitable[StoredDocument | None]: ...

    def put(self, document: StoredDocument) -> Awaitable[None]: ...

    def delete(self, document_id: str) -> Awaitable[int]: ...


class SyncElements(Protocol):
    def list_for_revision(
        self, document_id: str, revision_id: str
    ) -> Awaitable[tuple[StoredElement, ...]]: ...

    def delete_revision(self, document_id: str, revision_id: str) -> Awaitable[int]: ...

    def delete_document(self, document_id: str) -> Awaitable[int]: ...


class SyncChunks(Protocol):
    def list_for_revision(
        self, document_id: str, revision_id: str
    ) -> Awaitable[tuple[StoredChunk, ...]]: ...

    def delete_revision(self, document_id: str, revision_id: str) -> Awaitable[int]: ...

    def delete_document(self, document_id: str) -> Awaitable[int]: ...


class SyncRuntimeMetadata(Protocol):
    def get(self) -> Awaitable[RuntimeMetadata]: ...

    def set_query_cache_invalidation_required(
        self, required: bool
    ) -> Awaitable[RuntimeMetadata]: ...


class SyncUnitOfWork(Protocol):
    @property
    def documents(self) -> SyncDocuments: ...

    @property
    def elements(self) -> SyncElements: ...

    @property
    def chunks(self) -> SyncChunks: ...

    @property
    def runtime_metadata(self) -> SyncRuntimeMetadata: ...

    def __aenter__(self) -> Awaitable[Self]: ...

    def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Awaitable[None]: ...

    def commit(self) -> Awaitable[None]: ...


SyncUnitOfWorkFactory = Callable[[], SyncUnitOfWork]


class DocumentLease(Protocol):
    async def release(self) -> None: ...


class DocumentLeaseManager(Protocol):
    async def acquire(self, document_id: str) -> DocumentLease | None: ...


class FullBuildResult(Protocol):
    """Structural Stage 4 build result consumed without a graph-layer import."""

    document_id: str
    revision_id: str
    outcome: str
    failed_stage: FailedStage | None
    reason_code: BuildFailureCode | None
