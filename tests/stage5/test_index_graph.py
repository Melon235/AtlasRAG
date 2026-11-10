"""Thin six-node index_graph topology and fail-closed routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlasrag.graphs.indexing.index_graph import build_index_graph
from atlasrag.services.sync.failures import ScanError, SyncPlanError
from atlasrag.services.sync.models import (
    BuildExecutionSummary,
    DeletionSummary,
    GlobalSafetyResult,
    ReconciliationOutcome,
    ReconciliationResult,
    ScanSnapshot,
    ServingSafety,
    SyncOutcome,
    SyncPlan,
    SyncRequest,
    SyncResult,
)
from stage5.helpers import PIPELINE


class Steps:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.reconciliation = ReconciliationResult(
            outcome=ReconciliationOutcome.CLEAN, checked_documents=0
        )
        self.scan_error: Exception | None = None
        self.plan_error: Exception | None = None
        self.safety = GlobalSafetyResult(serving_safety=ServingSafety.SAFE_TO_SERVE)

    async def reconcile(self) -> ReconciliationResult:
        self.calls.append("reconcile")
        return self.reconciliation

    async def scan(self, knowledge_directory: Path) -> ScanSnapshot:
        self.calls.append("scan")
        if self.scan_error is not None:
            raise self.scan_error
        return ScanSnapshot()

    async def plan(self, scan: ScanSnapshot) -> SyncPlan:
        self.calls.append("plan")
        if self.plan_error is not None:
            raise self.plan_error
        return SyncPlan(pipeline_fingerprint=PIPELINE)

    async def execute_builds(
        self, plan: SyncPlan, scan: ScanSnapshot, reconciliation: ReconciliationResult
    ) -> BuildExecutionSummary:
        self.calls.append("builds")
        return BuildExecutionSummary()

    async def execute_deletions(
        self, plan: SyncPlan, scan: ScanSnapshot
    ) -> DeletionSummary:
        self.calls.append("deletions")
        return DeletionSummary()

    async def final_verify(
        self, builds: BuildExecutionSummary, deletions: DeletionSummary
    ) -> GlobalSafetyResult:
        self.calls.append("verify")
        return self.safety


async def invoke(steps: Steps, tmp_path: Path) -> SyncResult:
    graph = build_index_graph(steps)
    assert graph.checkpointer is None
    output = await graph.ainvoke(
        {
            "request": SyncRequest(
                knowledge_directory=tmp_path, expected_pipeline_fingerprint=PIPELINE
            )
        }
    )
    return SyncResult.model_validate(output["final_result"])


@pytest.mark.asyncio
async def test_empty_knowledge_directory_runs_all_six_nodes(tmp_path: Path) -> None:
    steps = Steps()
    result = await invoke(steps, tmp_path)
    assert steps.calls == ["reconcile", "scan", "plan", "builds", "deletions", "verify"]
    assert result.outcome is SyncOutcome.SUCCESS
    assert result.serving_safety is ServingSafety.SAFE_TO_SERVE


@pytest.mark.asyncio
async def test_blocked_reconciliation_stops_before_scan_or_delete(
    tmp_path: Path,
) -> None:
    steps = Steps()
    steps.reconciliation = ReconciliationResult(
        outcome=ReconciliationOutcome.BLOCKED,
        blocked_reasons=("CURRENT_EQUALS_BUILDING",),
        checked_documents=1,
    )
    result = await invoke(steps, tmp_path)
    assert steps.calls == ["reconcile"]
    assert result.outcome is SyncOutcome.FAILED
    assert result.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "CURRENT_EQUALS_BUILDING" in result.unsafe_reasons


@pytest.mark.asyncio
async def test_scan_failure_stops_before_plan_and_delete(tmp_path: Path) -> None:
    steps = Steps()
    steps.scan_error = ScanError("SCAN_PERMISSION_DENIED")
    result = await invoke(steps, tmp_path)
    assert steps.calls == ["reconcile", "scan"]
    assert result.outcome is SyncOutcome.FAILED
    assert result.blocked_code == "SCAN_PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_plan_failure_stops_before_build_or_delete(tmp_path: Path) -> None:
    steps = Steps()
    steps.plan_error = SyncPlanError("STALE_BUILDING_CANDIDATE")
    result = await invoke(steps, tmp_path)
    assert steps.calls == ["reconcile", "scan", "plan"]
    assert result.blocked_code == "STALE_BUILDING_CANDIDATE"


@pytest.mark.asyncio
async def test_unsafe_verdict_is_failed_but_still_a_result(tmp_path: Path) -> None:
    steps = Steps()
    steps.safety = GlobalSafetyResult(
        serving_safety=ServingSafety.UNSAFE_TO_SERVE,
        unsafe_reasons=("ORPHAN_RECORDS",),
    )
    result = await invoke(steps, tmp_path)
    assert result.outcome is SyncOutcome.FAILED
    assert result.serving_safety is ServingSafety.UNSAFE_TO_SERVE
