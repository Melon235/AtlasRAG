"""Read the durable document projection used by deterministic sync planning."""

from __future__ import annotations

from pydantic import ValidationError

from atlasrag.domain.errors import CanonicalDataError
from atlasrag.repositories.postgres.records import StoredDocument
from atlasrag.services.ingestion.mapping import load_document_parse_metadata_envelope
from atlasrag.services.sync.failures import SyncPlanError
from atlasrag.services.sync.models import (
    CanonicalDatabaseSnapshot,
    DatabaseDocumentSnapshot,
)
from atlasrag.services.sync.ports import SyncUnitOfWorkFactory


def _project(document: StoredDocument) -> DatabaseDocumentSnapshot:
    try:
        stored = StoredDocument.model_validate(document)
        load_document_parse_metadata_envelope(stored)
        return DatabaseDocumentSnapshot(
            document_id=stored.document_id,
            relative_source_path=stored.relative_source_path,
            file_name=stored.file_name,
            source_type=stored.source_type,
            current_revision_id=stored.current_revision_id,
            current_content_hash=stored.current_content_hash,
            current_pipeline_fingerprint=stored.current_pipeline_fingerprint,
            building_revision_id=stored.building_revision_id,
            building_content_hash=stored.building_content_hash,
            building_pipeline_fingerprint=stored.building_pipeline_fingerprint,
            ingestion_status=stored.ingestion_status,
            failed_stage=stored.failed_stage,
        )
    except (CanonicalDataError, ValidationError, ValueError):
        raise SyncPlanError("DOCUMENT_SNAPSHOT_INVALID") from None


class CanonicalDatabaseSnapshotService:
    """Load all document rows; this service never commits or repairs state."""

    def __init__(self, uow_factory: SyncUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def load(self) -> CanonicalDatabaseSnapshot:
        async with self._uow_factory() as unit:
            documents = await unit.documents.list()
        projected = tuple(
            sorted(
                (_project(document) for document in documents),
                key=lambda d: d.relative_source_path,
            )
        )
        try:
            return CanonicalDatabaseSnapshot(documents=projected)
        except (ValidationError, ValueError):
            raise SyncPlanError("DOCUMENT_SNAPSHOT_INVALID") from None
