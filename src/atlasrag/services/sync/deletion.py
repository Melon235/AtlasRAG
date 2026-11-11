"""Milvus-first, PostgreSQL-last missing-source deletion."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import aclosing
from datetime import datetime
from pathlib import Path

from atlasrag.domain.errors import (
    CanonicalDataError,
    DependencyError,
    InvariantViolationError,
    ProviderResponseError,
)
from atlasrag.repositories.postgres.records import RuntimeMetadata
from atlasrag.services.indexing.build import finish_task, settled_operation
from atlasrag.services.sync.models import (
    DeletionItem,
    DeletionOutcome,
    DeletionSummary,
    ScanSnapshot,
    SyncAction,
)
from atlasrag.services.sync.ports import (
    DocumentLeaseManager,
    SyncUnitOfWorkFactory,
)

Clock = Callable[[], datetime]


class MissingSourceDeletionService:
    """Delete only after a complete scan; never delete on a stale/partial plan."""

    def __init__(
        self,
        uow_factory: SyncUnitOfWorkFactory,
        *,
        index: object,
        leases: DocumentLeaseManager,
        knowledge_directory: Path,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._index = index
        self._leases = leases
        self._knowledge_directory = knowledge_directory
        self._clock = clock

    def _source_exists(self, relative_source_path: str) -> bool:
        return self._knowledge_directory.joinpath(
            *relative_source_path.split("/")
        ).exists()

    async def _acquire(self, document_id: str) -> object | None:
        acquisition = asyncio.create_task(self._leases.acquire(document_id))
        try:
            return await asyncio.shield(acquisition)
        except asyncio.CancelledError as cancellation:
            try:
                lease = await finish_task(acquisition)
            except BaseException as error:
                cancellation.add_note(
                    f"deletion lease acquisition: {type(error).__name__}"
                )
                raise cancellation
            if lease is not None:
                release = getattr(lease, "release")
                await settled_operation(release())
            raise cancellation

    async def _probe(self, lease: object) -> None:
        probe = getattr(lease, "probe", None)
        if callable(probe):
            await probe()

    async def _delete_milvus(self, document_id: str) -> bool:
        delete_document = getattr(self._index, "delete_document", None)
        projection = getattr(self._index, "iter_document_projection", None)
        if not all(callable(item) for item in (delete_document, projection)):
            raise InvariantViolationError
        assert callable(delete_document) and callable(projection)
        await delete_document(document_id)
        # The provider's document delete performs its own visibility barrier; an
        # explicit Strong document scan is still required before PostgreSQL.
        async with aclosing(projection(document_id, batch_size=1_000)) as rows:
            async for _ in rows:
                return False
        return True

    async def _delete_postgres(self, document_id: str) -> bool:
        async with self._uow_factory() as unit:
            stored = await unit.documents.get_by_id(document_id)
            if stored is not None:
                await unit.chunks.delete_document(document_id)
                await unit.elements.delete_document(document_id)
                await unit.documents.delete(document_id)
            metadata = RuntimeMetadata.model_validate(
                await unit.runtime_metadata.set_query_cache_invalidation_required(True)
            )
            if not metadata.query_cache_invalidation_required:
                raise InvariantViolationError
            await unit.commit()
        return True

    async def delete(
        self,
        actions: tuple[SyncAction, ...],
        *,
        scan: ScanSnapshot,
    ) -> DeletionSummary:
        del scan  # presence is revalidated on the filesystem after locking
        items: list[DeletionItem] = []
        for action in sorted(actions, key=lambda item: item.document_id):
            lease = await self._acquire(action.document_id)
            if lease is None:
                items.append(
                    DeletionItem(
                        document_id=action.document_id,
                        outcome=DeletionOutcome.FAILED,
                        failure_code="LEASE_UNAVAILABLE",
                    )
                )
                continue
            try:
                await self._probe(lease)
                if self._source_exists(action.relative_source_path):
                    items.append(
                        DeletionItem(
                            document_id=action.document_id,
                            outcome=DeletionOutcome.FAILED,
                            failure_code="SOURCE_REAPPEARED",
                        )
                    )
                    continue
                async with self._uow_factory() as unit:
                    stored = await unit.documents.get_by_id(action.document_id)
                if stored is None:
                    items.append(
                        DeletionItem(
                            document_id=action.document_id,
                            outcome=DeletionOutcome.FAILED,
                            failure_code="STATE_CHANGED",
                        )
                    )
                    continue
                if (
                    stored.document_id != action.document_id
                    or stored.relative_source_path != action.relative_source_path
                    or (
                        action.current_revision_id is not None
                        and stored.current_revision_id != action.current_revision_id
                    )
                ):
                    items.append(
                        DeletionItem(
                            document_id=action.document_id,
                            outcome=DeletionOutcome.FAILED,
                            failure_code="STATE_CHANGED",
                        )
                    )
                    continue
                await self._probe(lease)
                try:
                    milvus_absent = await self._delete_milvus(action.document_id)
                except (DependencyError, ProviderResponseError):
                    items.append(
                        DeletionItem(
                            document_id=action.document_id,
                            outcome=DeletionOutcome.FAILED,
                            failure_code="MILVUS_DELETE_FAILED",
                        )
                    )
                    continue
                if not milvus_absent:
                    items.append(
                        DeletionItem(
                            document_id=action.document_id,
                            outcome=DeletionOutcome.FAILED,
                            failure_code="VISIBILITY_REMAINS",
                            milvus_deleted=True,
                        )
                    )
                    continue
                await self._probe(lease)
                try:
                    await self._delete_postgres(action.document_id)
                except (DependencyError, CanonicalDataError):
                    items.append(
                        DeletionItem(
                            document_id=action.document_id,
                            outcome=DeletionOutcome.FAILED,
                            failure_code="POSTGRES_DELETE_FAILED",
                            milvus_deleted=True,
                        )
                    )
                    continue
                items.append(
                    DeletionItem(
                        document_id=action.document_id,
                        outcome=DeletionOutcome.DELETED,
                        milvus_deleted=True,
                        postgres_deleted=True,
                    )
                )
            finally:
                release = getattr(lease, "release")
                await settled_operation(release())
        return DeletionSummary(items=tuple(items))
