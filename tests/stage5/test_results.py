"""Outcome and serving-safety composition."""

from __future__ import annotations

from atlasrag.domain.ingestion.source import derive_document_id
from atlasrag.repositories.postgres.records import FailedStage
from atlasrag.services.indexing.ports import BuildFailureCode
from atlasrag.services.sync.models import (
    BuildExecutionItem,
    BuildExecutionSummary,
    BuildMode,
    BuildOutcome,
    DeletionSummary,
    GlobalSafetyResult,
    ReconciliationOutcome,
    ReconciliationResult,
    ResidueCleanup,
    ServingSafety,
    SyncOutcome,
    SyncPlan,
)
from atlasrag.services.sync.results import finalize_sync_result
from stage5.helpers import PIPELINE


def reconciliation() -> ReconciliationResult:
    return ReconciliationResult(
        outcome=ReconciliationOutcome.CLEAN, checked_documents=1
    )


def plan() -> SyncPlan:
    return SyncPlan(pipeline_fingerprint=PIPELINE)


def safe() -> GlobalSafetyResult:
    return GlobalSafetyResult(serving_safety=ServingSafety.SAFE_TO_SERVE)


def test_failed_build_with_healthy_r1_is_partial_and_safe() -> None:
    doc = derive_document_id("a.txt")
    item = BuildExecutionItem(
        document_id=doc,
        revision_id="rev_" + "a" * 64,
        mode=BuildMode.FULL,
        outcome=BuildOutcome.FAILED,
        failed_stage=FailedStage.INDEX_WRITE,
        reason_code=BuildFailureCode.INDEX_WRITE.value,
        residue_cleanup=ResidueCleanup.CLEANED,
    )
    result = finalize_sync_result(
        reconciliation=reconciliation(),
        plan=plan(),
        builds=BuildExecutionSummary(items=(item,)),
        deletions=DeletionSummary(),
        safety=safe(),
    )
    assert result.outcome is SyncOutcome.PARTIAL_SUCCESS
    assert result.serving_safety is ServingSafety.SAFE_TO_SERVE


def test_technical_build_failure_can_never_be_safe() -> None:
    item = BuildExecutionItem(
        document_id=derive_document_id("a.txt"),
        revision_id="rev_" + "b" * 64,
        mode=BuildMode.FULL,
        outcome=BuildOutcome.TECHNICAL_FAILURE,
        technical_code="DEPENDENCY_UNAVAILABLE",
        residue_cleanup=ResidueCleanup.CLEANED,
    )
    result = finalize_sync_result(
        reconciliation=reconciliation(),
        plan=plan(),
        builds=BuildExecutionSummary(items=(item,)),
        deletions=DeletionSummary(),
        safety=safe(),
    )
    assert result.outcome is SyncOutcome.FAILED
    assert result.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "TECHNICAL_BUILD_FAILURE" in result.unsafe_reasons
