"""Pure, deterministic sync planning with no I/O or side effects."""

from __future__ import annotations

from atlasrag.domain.ingestion.source import (
    derive_document_id,
    derive_revision_id,
    validate_sha256_hex,
)
from atlasrag.repositories.postgres.records import IngestionStatus
from atlasrag.services.sync.failures import SyncPlanError
from atlasrag.services.sync.models import (
    CanonicalDatabaseSnapshot,
    DatabaseDocumentSnapshot,
    ScanSnapshot,
    SyncAction,
    SyncActionKind,
    SyncPlan,
    SyncReason,
)


def _build_action(
    entry_path: str,
    document_id: str,
    content_hash: str,
    pipeline_fingerprint: str,
    reason: SyncReason,
) -> SyncAction:
    return SyncAction(
        kind=SyncActionKind.BUILD,
        document_id=document_id,
        relative_source_path=entry_path,
        reason=reason,
        content_hash=content_hash,
        revision_id=derive_revision_id(document_id, content_hash, pipeline_fingerprint),
    )


def _plan_document(
    entry_path: str,
    document_id: str,
    document: DatabaseDocumentSnapshot | None,
    content_hash: str,
    pipeline_fingerprint: str,
) -> SyncAction:
    if document is None:
        return _build_action(
            entry_path,
            document_id,
            content_hash,
            pipeline_fingerprint,
            SyncReason.NEW_SOURCE,
        )
    if not document.has_current:
        if (
            document.building_revision_id is not None
            and document.ingestion_status is not IngestionStatus.FAILED
            and (
                document.building_content_hash != content_hash
                or document.building_pipeline_fingerprint != pipeline_fingerprint
            )
        ):
            raise SyncPlanError("STALE_BUILDING_CANDIDATE")
        reason = (
            SyncReason.RETRY_FAILED_BUILD
            if document.ingestion_status is IngestionStatus.FAILED
            else SyncReason.NEW_SOURCE
        )
        return _build_action(
            entry_path,
            document_id,
            content_hash,
            pipeline_fingerprint,
            reason,
        )
    if document.current_content_hash != content_hash:
        reason = SyncReason.CONTENT_CHANGED
    elif document.current_pipeline_fingerprint != pipeline_fingerprint:
        reason = SyncReason.PIPELINE_CHANGED
    else:
        if (
            document.building_revision_id is not None
            and document.ingestion_status is not IngestionStatus.FAILED
        ):
            # A non-failed building candidate must not survive a reconciliation
            # that produced this snapshot. Refuse to plan over ambiguous ownership.
            raise SyncPlanError("STALE_BUILDING_CANDIDATE")
        return SyncAction(
            kind=SyncActionKind.UNCHANGED,
            document_id=document.document_id,
            relative_source_path=document.relative_source_path,
            current_revision_id=document.current_revision_id,
        )
    if (
        document.building_revision_id is not None
        and document.ingestion_status is not IngestionStatus.FAILED
        and (
            document.building_content_hash != content_hash
            or document.building_pipeline_fingerprint != pipeline_fingerprint
        )
    ):
        raise SyncPlanError("STALE_BUILDING_CANDIDATE")
    return _build_action(
        entry_path,
        document_id,
        content_hash,
        pipeline_fingerprint,
        reason,
    )


class SyncPlanner:
    """Map one complete scan and one canonical DB snapshot to one plan."""

    def plan(
        self,
        scan: ScanSnapshot,
        database: CanonicalDatabaseSnapshot,
        *,
        pipeline_fingerprint: str,
    ) -> SyncPlan:
        if not isinstance(scan, ScanSnapshot):
            raise TypeError("scan must be a ScanSnapshot")
        if not isinstance(database, CanonicalDatabaseSnapshot):
            raise TypeError("database must be a CanonicalDatabaseSnapshot")
        validate_sha256_hex(pipeline_fingerprint, "pipeline fingerprint")
        scanned = scan.by_path()
        persisted = database.by_path()
        actions: list[SyncAction] = []
        for entry in scan.entries:
            path = entry.source.relative_source_path
            document = persisted.get(path)
            document_id = (
                document.document_id
                if document is not None
                else derive_document_id(path)
            )
            actions.append(
                _plan_document(
                    path,
                    document_id,
                    document,
                    entry.content_hash,
                    pipeline_fingerprint,
                )
            )
        for document in database.documents:
            if document.relative_source_path in scanned:
                continue
            actions.append(
                SyncAction(
                    kind=SyncActionKind.DELETE,
                    document_id=document.document_id,
                    relative_source_path=document.relative_source_path,
                    current_revision_id=document.current_revision_id,
                )
            )
        kind_order = {
            SyncActionKind.BUILD: 0,
            SyncActionKind.DELETE: 1,
            SyncActionKind.UNCHANGED: 2,
        }
        ordered = tuple(
            sorted(
                actions,
                key=lambda action: (
                    kind_order[action.kind],
                    action.relative_source_path,
                ),
            )
        )
        return SyncPlan(
            pipeline_fingerprint=pipeline_fingerprint,
            actions=ordered,
        )
