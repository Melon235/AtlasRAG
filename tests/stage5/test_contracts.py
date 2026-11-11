"""Closed Stage 5 contracts and deterministic planner behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from atlasrag.domain.ingestion.source import derive_document_id, derive_revision_id
from atlasrag.repositories.postgres.records import IngestionStatus
from atlasrag.services.sync.models import (
    DatabaseDocumentSnapshot,
    DeletionItem,
    DeletionOutcome,
    DeletionSummary,
    GlobalSafetyResult,
    ReconciliationOutcome,
    ReconciliationResult,
    RecoveryBoundary,
    RecoveryTarget,
    ServingSafety,
    SyncAction,
    SyncActionKind,
    SyncOutcome,
    SyncPlan,
    SyncReason,
    SyncRequest,
    SyncResult,
)
from stage5.helpers import PIPELINE, document, entry, scan

ROOT = Path("/tmp/atlasrag-stage5-model-root")


def test_scan_snapshot_is_sorted_and_unique(tmp_path: Path) -> None:
    b = entry(tmp_path, "b.txt", "b")
    a = entry(tmp_path, "a.txt", "a")
    snapshot = scan(b, a)
    assert tuple(e.source.relative_source_path for e in snapshot.entries) == (
        "a.txt",
        "b.txt",
    )
    with pytest.raises(ValidationError):
        from atlasrag.services.sync.models import ScanSnapshot

        ScanSnapshot(entries=(b, a))


def test_database_snapshot_rejects_ambiguous_ownership(tmp_path: Path) -> None:
    value = entry(tmp_path, "a.txt", "a")
    valid = document(value)
    with pytest.raises(ValidationError):
        DatabaseDocumentSnapshot.model_validate(
            valid.model_dump() | {"current_revision_id": valid.building_revision_id}
        )
    with pytest.raises(ValidationError):
        DatabaseDocumentSnapshot.model_validate(
            valid.model_dump()
            | {
                "ingestion_status": IngestionStatus.READY,
                "building_revision_id": "rev_" + "1" * 64,
            }
        )


def test_build_action_revision_binding(tmp_path: Path) -> None:
    value = entry(tmp_path, "a.txt", "a")
    doc_id = derive_document_id("a.txt")
    revision = derive_revision_id(doc_id, value.content_hash, PIPELINE)
    action = SyncAction(
        kind=SyncActionKind.BUILD,
        document_id=doc_id,
        relative_source_path="a.txt",
        reason=SyncReason.NEW_SOURCE,
        content_hash=value.content_hash,
        revision_id=revision,
    )
    plan = SyncPlan(pipeline_fingerprint=PIPELINE, actions=(action,))
    assert plan.builds == (action,)
    with pytest.raises(ValidationError):
        SyncPlan(
            pipeline_fingerprint=PIPELINE,
            actions=(action.model_copy(update={"revision_id": "rev_" + "2" * 64}),),
        )


def test_plan_rejects_noncanonical_action_order(tmp_path: Path) -> None:
    value = entry(tmp_path, "a.txt", "a")
    build = SyncAction(
        kind=SyncActionKind.BUILD,
        document_id=derive_document_id("a.txt"),
        relative_source_path="a.txt",
        reason=SyncReason.NEW_SOURCE,
        content_hash=value.content_hash,
        revision_id=derive_revision_id(
            derive_document_id("a.txt"), value.content_hash, PIPELINE
        ),
    )
    delete = SyncAction(
        kind=SyncActionKind.DELETE,
        document_id=derive_document_id("b.txt"),
        relative_source_path="b.txt",
    )
    with pytest.raises(ValidationError):
        SyncPlan(pipeline_fingerprint=PIPELINE, actions=(delete, build))


def test_recovery_target_binds_persisted_candidate(tmp_path: Path) -> None:
    value = entry(tmp_path, "a.txt", "a")
    doc_id = derive_document_id("a.txt")
    revision = derive_revision_id(doc_id, value.content_hash, PIPELINE)
    target = RecoveryTarget(
        document_id=doc_id,
        revision_id=revision,
        relative_source_path="a.txt",
        boundary=RecoveryBoundary.PARSED,
        content_hash=value.content_hash,
        pipeline_fingerprint=PIPELINE,
    )
    assert target.boundary is RecoveryBoundary.PARSED
    with pytest.raises(ValidationError):
        RecoveryTarget.model_validate(
            target.model_dump() | {"revision_id": "rev_" + "3" * 64}
        )


def test_reconciliation_blocked_requires_reasons() -> None:
    with pytest.raises(ValidationError):
        ReconciliationResult(outcome=ReconciliationOutcome.BLOCKED)
    result = ReconciliationResult(
        outcome=ReconciliationOutcome.BLOCKED,
        blocked_reasons=("CANONICAL_OWNERSHIP_INVALID",),
    )
    assert result.outcome is ReconciliationOutcome.BLOCKED


def test_safety_verdicts_are_separated() -> None:
    safe = SyncResult(
        outcome=SyncOutcome.PARTIAL_SUCCESS,
        serving_safety=ServingSafety.SAFE_TO_SERVE,
        failed_builds=1,
    )
    assert safe.serving_safety is ServingSafety.SAFE_TO_SERVE
    with pytest.raises(ValidationError):
        SyncResult(
            outcome=SyncOutcome.FAILED,
            serving_safety=ServingSafety.SAFE_TO_SERVE,
        )
    with pytest.raises(ValidationError):
        GlobalSafetyResult(serving_safety=ServingSafety.UNSAFE_TO_SERVE)


def test_deletion_rollback_cannot_claim_success() -> None:
    with pytest.raises(ValidationError):
        DeletionItem(
            document_id=derive_document_id("a.txt"),
            outcome=DeletionOutcome.DELETED,
            postgres_deleted=False,
        )
    item = DeletionItem(
        document_id=derive_document_id("a.txt"),
        outcome=DeletionOutcome.FAILED,
        failure_code="POSTGRES_DELETE_FAILED",
        milvus_deleted=True,
    )
    assert item.postgres_deleted is False
    assert item.milvus_deleted is True
    assert DeletionSummary(items=(item,)).failed == (item,)


def test_sync_request_requires_path_and_fingerprint(tmp_path: Path) -> None:
    request = SyncRequest(
        knowledge_directory=tmp_path, expected_pipeline_fingerprint=PIPELINE
    )
    assert request.knowledge_directory == tmp_path
    with pytest.raises(ValidationError):
        SyncRequest.model_validate(
            {
                "knowledge_directory": str(tmp_path),
                "expected_pipeline_fingerprint": PIPELINE,
            }
        )
