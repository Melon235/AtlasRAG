"""Dependency-free Stage 5 graph service boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from atlasrag.services.sync.models import (
    BuildExecutionSummary,
    DeletionSummary,
    GlobalSafetyResult,
    ReconciliationResult,
    ScanSnapshot,
    SyncPlan,
)


class IndexGraphSteps(Protocol):
    """Business capabilities injected into the thin maintenance graph."""

    async def reconcile(self) -> ReconciliationResult: ...

    async def scan(self, knowledge_directory: Path) -> ScanSnapshot: ...

    async def plan(self, scan: ScanSnapshot) -> SyncPlan: ...

    async def execute_builds(
        self,
        plan: SyncPlan,
        scan: ScanSnapshot,
        reconciliation: ReconciliationResult,
    ) -> BuildExecutionSummary: ...

    async def execute_deletions(
        self, plan: SyncPlan, scan: ScanSnapshot
    ) -> DeletionSummary: ...

    async def final_verify(
        self,
        builds: BuildExecutionSummary,
        deletions: DeletionSummary,
    ) -> GlobalSafetyResult: ...
