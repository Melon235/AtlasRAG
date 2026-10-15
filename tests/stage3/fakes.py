"""Transaction-aware in-memory fakes for Stage 3 candidate service tests."""

from __future__ import annotations

from asyncio import CancelledError
from collections.abc import Iterable
from dataclasses import dataclass, field
from types import TracebackType
from typing import TypeVar

from atlasrag.domain.errors import InvariantViolationError
from atlasrag.repositories.postgres.records import (
    StoredChunk,
    StoredDocument,
    StoredElement,
)

_KeyT = TypeVar("_KeyT")
_ValueT = TypeVar("_ValueT")


def _merge_mapping_delta(
    committed: dict[_KeyT, _ValueT],
    baseline: dict[_KeyT, _ValueT],
    working: dict[_KeyT, _ValueT],
) -> None:
    for key in baseline.keys() - working.keys():
        committed.pop(key, None)
    for key, value in working.items():
        if key not in baseline or baseline[key] != value:
            committed[key] = value


@dataclass(slots=True)
class FakeDatabase:
    """Committed fake persistence state shared by fresh Unit-of-Work objects."""

    documents: dict[str, StoredDocument] = field(default_factory=dict)
    elements: dict[tuple[str, str], tuple[StoredElement, ...]] = field(
        default_factory=dict
    )
    chunks: dict[tuple[str, str], tuple[StoredChunk, ...]] = field(default_factory=dict)
    query_cache_invalidation_required: bool = False

    def clone(self) -> FakeDatabase:
        return FakeDatabase(
            documents=dict(self.documents),
            elements=dict(self.elements),
            chunks=dict(self.chunks),
            query_cache_invalidation_required=(self.query_cache_invalidation_required),
        )


class FakeUnitOfWorkFactory:
    """Create one isolated transaction snapshot per service capability."""

    def __init__(self, database: FakeDatabase | None = None) -> None:
        self.database = database if database is not None else FakeDatabase()
        self.unit_of_works: list[FakeUnitOfWork] = []
        self.calls: list[str] = []
        self.document_writes: list[StoredDocument] = []
        self.runtime_metadata_accesses = 0
        self._fail_after: dict[str, BaseException] = {}

    def __call__(self) -> FakeUnitOfWork:
        unit_of_work = FakeUnitOfWork(self)
        self.unit_of_works.append(unit_of_work)
        return unit_of_work

    def fail_after(self, operation: str, error: BaseException) -> None:
        self._fail_after[operation] = error

    def raise_after_if_planned(self, operation: str) -> None:
        error = self._fail_after.pop(operation, None)
        if error is not None:
            raise error

    @property
    def commit_count(self) -> int:
        return sum(unit_of_work.commit_count for unit_of_work in self.unit_of_works)

    @property
    def rollback_count(self) -> int:
        return sum(unit_of_work.rollback_count for unit_of_work in self.unit_of_works)


class FakeDocumentRepository:
    def __init__(
        self,
        factory: FakeUnitOfWorkFactory,
        working: FakeDatabase,
    ) -> None:
        self._factory = factory
        self._working = working

    async def put(self, document: StoredDocument) -> None:
        self._factory.calls.append("documents.put")
        self._working.documents[document.document_id] = document
        self._factory.document_writes.append(document)
        self._factory.raise_after_if_planned("documents.put")

    async def get_by_id(self, document_id: str) -> StoredDocument | None:
        self._factory.calls.append("documents.get_by_id")
        result = self._working.documents.get(document_id)
        self._factory.raise_after_if_planned("documents.get_by_id")
        return result

    async def get_by_relative_source_path(
        self, relative_source_path: str
    ) -> StoredDocument | None:
        self._factory.calls.append("documents.get_by_relative_source_path")
        matches = tuple(
            document
            for document in self._working.documents.values()
            if document.relative_source_path == relative_source_path
        )
        if len(matches) > 1:
            raise InvariantViolationError
        self._factory.raise_after_if_planned("documents.get_by_relative_source_path")
        return matches[0] if matches else None


class FakeElementRepository:
    def __init__(
        self,
        factory: FakeUnitOfWorkFactory,
        working: FakeDatabase,
    ) -> None:
        self._factory = factory
        self._working = working

    async def replace_revision_set(
        self,
        document_id: str,
        revision_id: str,
        elements: Iterable[StoredElement],
    ) -> None:
        self._factory.calls.append("elements.replace_revision_set")
        records = tuple(elements)
        if any(
            record.document_id != document_id or record.revision_id != revision_id
            for record in records
        ) or len({record.element_id for record in records}) != len(records):
            raise InvariantViolationError
        self._working.elements[(document_id, revision_id)] = records
        self._factory.raise_after_if_planned("elements.replace_revision_set")

    async def delete_revision(self, document_id: str, revision_id: str) -> int:
        self._factory.calls.append("elements.delete_revision")
        removed = self._working.elements.pop((document_id, revision_id), ())
        self._factory.raise_after_if_planned("elements.delete_revision")
        return len(removed)

    async def list_for_revision(
        self,
        document_id: str,
        revision_id: str,
    ) -> tuple[StoredElement, ...]:
        self._factory.calls.append("elements.list_for_revision")
        records = self._working.elements.get((document_id, revision_id), ())
        self._factory.raise_after_if_planned("elements.list_for_revision")
        return tuple(
            sorted(records, key=lambda record: (record.order_index, record.element_id))
        )


class FakeChunkRepository:
    def __init__(
        self,
        factory: FakeUnitOfWorkFactory,
        working: FakeDatabase,
    ) -> None:
        self._factory = factory
        self._working = working

    async def replace_revision_set(
        self,
        document_id: str,
        revision_id: str,
        chunks: Iterable[StoredChunk],
    ) -> None:
        self._factory.calls.append("chunks.replace_revision_set")
        records = tuple(chunks)
        if any(
            record.document_id != document_id or record.revision_id != revision_id
            for record in records
        ) or len({record.chunk_id for record in records}) != len(records):
            raise InvariantViolationError
        self._working.chunks[(document_id, revision_id)] = records
        self._factory.raise_after_if_planned("chunks.replace_revision_set")

    async def delete_revision(self, document_id: str, revision_id: str) -> int:
        self._factory.calls.append("chunks.delete_revision")
        removed = self._working.chunks.pop((document_id, revision_id), ())
        self._factory.raise_after_if_planned("chunks.delete_revision")
        return len(removed)


class FakeUnitOfWork:
    """Rollback the working snapshot unless one explicit commit succeeds."""

    def __init__(self, factory: FakeUnitOfWorkFactory) -> None:
        self._factory = factory
        self._baseline: FakeDatabase | None = None
        self._working: FakeDatabase | None = None
        self._documents: FakeDocumentRepository | None = None
        self._elements: FakeElementRepository | None = None
        self._chunks: FakeChunkRepository | None = None
        self._active = False
        self.commit_count = 0
        self.rollback_count = 0

    @property
    def documents(self) -> FakeDocumentRepository:
        self._guard()
        assert self._documents is not None
        return self._documents

    @property
    def elements(self) -> FakeElementRepository:
        self._guard()
        assert self._elements is not None
        return self._elements

    @property
    def chunks(self) -> FakeChunkRepository:
        self._guard()
        assert self._chunks is not None
        return self._chunks

    @property
    def runtime_metadata(self) -> None:
        self._factory.runtime_metadata_accesses += 1
        raise AssertionError("candidate services must not access runtime metadata")

    async def __aenter__(self) -> FakeUnitOfWork:
        if self._active or self._baseline is not None or self._working is not None:
            raise InvariantViolationError
        self._baseline = self._factory.database.clone()
        self._working = self._baseline.clone()
        self._documents = FakeDocumentRepository(self._factory, self._working)
        self._elements = FakeElementRepository(self._factory, self._working)
        self._chunks = FakeChunkRepository(self._factory, self._working)
        self._active = True
        return self

    async def commit(self) -> None:
        self._guard()
        if self.commit_count:
            raise InvariantViolationError
        assert self._baseline is not None
        assert self._working is not None
        self._factory.raise_after_if_planned("uow.commit")
        committed = self._factory.database
        _merge_mapping_delta(
            committed.documents,
            self._baseline.documents,
            self._working.documents,
        )
        _merge_mapping_delta(
            committed.elements,
            self._baseline.elements,
            self._working.elements,
        )
        _merge_mapping_delta(
            committed.chunks,
            self._baseline.chunks,
            self._working.chunks,
        )
        if (
            self._working.query_cache_invalidation_required
            != self._baseline.query_cache_invalidation_required
        ):
            committed.query_cache_invalidation_required = (
                self._working.query_cache_invalidation_required
            )
        self.commit_count += 1
        self._active = False

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, traceback
        if self.commit_count == 0:
            self.rollback_count += 1
        self._active = False
        if isinstance(exc_value, CancelledError):
            return None

    def _guard(self) -> None:
        if not self._active:
            raise InvariantViolationError
