"""Compute the final sync outcome independently from serving safety."""

from __future__ import annotations

from atlasrag.services.sync.models import (
    BuildExecutionSummary,
    DeletionSummary,
    GlobalSafetyResult,
    ReconciliationOutcome,
    ReconciliationResult,
    ServingSafety,
    SyncOutcome,
    SyncPlan,
    SyncResult,
)


def finalize_sync_result(
    *,
    reconciliation: ReconciliationResult,
    plan: SyncPlan | None,
    builds: BuildExecutionSummary | None,
    deletions: DeletionSummary | None,
    safety: GlobalSafetyResult | None,
    blocked_code: str | None = None,
) -> SyncResult:
    builds = builds or BuildExecutionSummary()
    deletions = deletions or DeletionSummary()
    plan_builds = len(plan.builds) if plan is not None else 0
    plan_deletions = len(plan.deletions) if plan is not None else 0
    if (
        blocked_code is not None
        or reconciliation.outcome is ReconciliationOutcome.BLOCKED
    ):
        code = blocked_code or (
            reconciliation.blocked_reasons[0]
            if reconciliation.blocked_reasons
            else "BLOCKED"
        )
        reasons = tuple(sorted(set(reconciliation.blocked_reasons) | {code}))
        return SyncResult(
            outcome=SyncOutcome.FAILED,
            serving_safety=ServingSafety.UNSAFE_TO_SERVE,
            checked_documents=reconciliation.checked_documents,
            planned_builds=plan_builds,
            planned_deletions=plan_deletions,
            unsafe_reasons=reasons,
            blocked_code=code,
        )
    assert safety is not None
    technical = len(builds.technical)
    failed = len(builds.failed)
    mutated = len(builds.source_mutated)
    failed_deletions = len(deletions.failed)
    unsafe = set(safety.unsafe_reasons)
    serving_safety = safety.serving_safety
    if technical:
        unsafe.add("TECHNICAL_BUILD_FAILURE")
        serving_safety = ServingSafety.UNSAFE_TO_SERVE
    if (
        technical
        or failed_deletions
        or safety.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    ):
        outcome = SyncOutcome.FAILED
    elif failed or mutated:
        outcome = SyncOutcome.PARTIAL_SUCCESS
    else:
        outcome = SyncOutcome.SUCCESS
    return SyncResult(
        outcome=outcome,
        serving_safety=serving_safety,
        checked_documents=reconciliation.checked_documents,
        planned_builds=plan_builds,
        planned_deletions=plan_deletions,
        ready_builds=len(builds.ready),
        failed_builds=failed,
        source_mutated_builds=mutated,
        technical_build_failures=technical,
        deleted_documents=len(deletions.deleted),
        failed_deletions=failed_deletions,
        verified_documents=safety.verified_documents,
        verified_records=safety.verified_records,
        unsafe_reasons=tuple(sorted(unsafe)),
    )
