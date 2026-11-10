"""Minimal Stage 5 maintenance State; no dependencies or derived data."""

from typing import TypedDict

from atlasrag.services.sync.models import (
    BuildExecutionSummary,
    DeletionSummary,
    ReconciliationResult,
    ScanSnapshot,
    SyncPlan,
    SyncRequest,
    SyncResult,
)


class IndexGraphInput(TypedDict):
    request: SyncRequest


class IndexGraphOutput(TypedDict):
    final_result: SyncResult


class IndexGraphState(TypedDict, total=False):
    request: SyncRequest
    reconciliation_result: ReconciliationResult
    scan_snapshot: ScanSnapshot
    sync_plan: SyncPlan
    build_summary: BuildExecutionSummary
    deletion_summary: DeletionSummary
    final_result: SyncResult
