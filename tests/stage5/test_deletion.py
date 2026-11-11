"""Milvus-first deletion with locking, revalidation, and marker rollback."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from atlasrag.domain.errors import DependencyUnavailableError
from atlasrag.services.sync.deletion import MissingSourceDeletionService
from atlasrag.services.sync.models import (
    DeletionOutcome,
    ScanSnapshot,
    SyncAction,
    SyncActionKind,
)
from stage5.fakes import MemoryIndex
from stage5.test_current_repair import published

NOW = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)


class Lease:
    def __init__(self) -> None:
        self.released = 0

    async def release(self) -> None:
        self.released += 1


class Leases:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.leases: list[Lease] = []

    async def acquire(self, document_id: str) -> Lease | None:
        if not self.available:
            return None
        lease = Lease()
        self.leases.append(lease)
        return lease


class FailingMilvus(MemoryIndex):
    async def delete_document(self, document_id: str) -> int:
        raise DependencyUnavailableError


def action(stored: object) -> SyncAction:
    return SyncAction(
        kind=SyncActionKind.DELETE,
        document_id=stored.document_id,  # type: ignore[attr-defined]
        relative_source_path=stored.relative_source_path,  # type: ignore[attr-defined]
        current_revision_id=stored.current_revision_id,  # type: ignore[attr-defined]
    )


def service(
    factory: object, index: object, leases: object, root: Path
) -> MissingSourceDeletionService:
    return MissingSourceDeletionService(
        factory,  # type: ignore[arg-type]
        index=index,
        leases=leases,  # type: ignore[arg-type]
        knowledge_directory=root,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_deletion_removes_milvus_then_pg_and_sets_marker(tmp_path: Path) -> None:
    factory, stored, repair, _, index, _ = published(tmp_path)
    await repair.reconcile_current(stored.document_id)
    (tmp_path / "guides" / "candidate.txt").unlink()
    leases = Leases()
    result = await service(factory, index, leases, tmp_path).delete(
        (action(stored),), scan=ScanSnapshot()
    )
    item = result.items[0]
    assert item.outcome is DeletionOutcome.DELETED
    assert item.milvus_deleted and item.postgres_deleted
    assert index.rows == []
    assert factory.database.documents == {}
    assert factory.database.elements == {}
    assert factory.database.chunks == {}
    assert factory.database.query_cache_invalidation_required is True
    assert leases.leases[0].released == 1


@pytest.mark.asyncio
async def test_reappeared_source_blocks_deletion(tmp_path: Path) -> None:
    factory, stored, _, _, index, _ = published(tmp_path)
    leases = Leases()
    result = await service(factory, index, leases, tmp_path).delete(
        (action(stored),), scan=ScanSnapshot()
    )
    assert result.items[0].outcome is DeletionOutcome.FAILED
    assert result.items[0].failure_code == "SOURCE_REAPPEARED"
    assert stored.document_id in factory.database.documents


@pytest.mark.asyncio
async def test_milvus_failure_prevents_postgres_delete(tmp_path: Path) -> None:
    factory, stored, _, _, _, _ = published(tmp_path)
    (tmp_path / "guides" / "candidate.txt").unlink()
    result = await service(factory, FailingMilvus(), Leases(), tmp_path).delete(
        (action(stored),), scan=ScanSnapshot()
    )
    assert result.items[0].failure_code == "MILVUS_DELETE_FAILED"
    assert stored.document_id in factory.database.documents


@pytest.mark.asyncio
async def test_postgres_failure_after_milvus_delete_is_unsafe_not_deleted(
    tmp_path: Path,
) -> None:
    factory, stored, repair, _, index, _ = published(tmp_path)
    await repair.reconcile_current(stored.document_id)
    (tmp_path / "guides" / "candidate.txt").unlink()
    factory.fail_after("documents.delete", DependencyUnavailableError())
    result = await service(factory, index, Leases(), tmp_path).delete(
        (action(stored),), scan=ScanSnapshot()
    )
    item = result.items[0]
    assert item.failure_code == "POSTGRES_DELETE_FAILED"
    assert item.milvus_deleted and not item.postgres_deleted
    assert stored.document_id in factory.database.documents
    assert factory.database.query_cache_invalidation_required is False
    assert index.rows == []


@pytest.mark.asyncio
async def test_marker_failure_rolls_back_postgres_delete(tmp_path: Path) -> None:
    factory, stored, _, _, index, _ = published(tmp_path)
    (tmp_path / "guides" / "candidate.txt").unlink()
    factory.fail_after("runtime_metadata.set", DependencyUnavailableError())
    result = await service(factory, index, Leases(), tmp_path).delete(
        (action(stored),), scan=ScanSnapshot()
    )
    assert result.items[0].failure_code == "POSTGRES_DELETE_FAILED"
    assert stored.document_id in factory.database.documents
    assert factory.database.query_cache_invalidation_required is False


@pytest.mark.asyncio
async def test_changed_current_revision_is_not_deleted(tmp_path: Path) -> None:
    factory, stored, _, _, index, _ = published(tmp_path)
    (tmp_path / "guides" / "candidate.txt").unlink()
    changed = stored.model_copy(update={"current_revision_id": "rev_" + "9" * 64})
    factory.database.documents[stored.document_id] = changed
    result = await service(factory, index, Leases(), tmp_path).delete(
        (action(stored),), scan=ScanSnapshot()
    )
    assert result.items[0].failure_code == "STATE_CHANGED"
    assert stored.document_id in factory.database.documents
