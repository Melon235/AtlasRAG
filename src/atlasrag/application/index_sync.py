"""Injected Stage 5 maintenance composition and runner; not a runtime owner."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from atlasrag.application.document_build import _LeasePoolCapture
from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import ConfigurationError
from atlasrag.domain.ingestion.source import SourceFile
from atlasrag.graphs.indexing.index_graph import build_index_graph
from atlasrag.graphs.indexing.sync_contracts import IndexGraphSteps
from atlasrag.services.indexing.build import finish_task, settled_operation
from atlasrag.services.sync.deletion import MissingSourceDeletionService
from atlasrag.services.sync.execution import BuildExecutionService
from atlasrag.services.sync.models import (
    BuildExecutionSummary,
    DeletionSummary,
    GlobalSafetyResult,
    ReconciliationResult,
    RecoveryTarget,
    ScanSnapshot,
    SyncPlan,
    SyncRequest,
    SyncResult,
)
from atlasrag.services.sync.planner import SyncPlanner
from atlasrag.services.sync.reconciliation import StartupReconciliationService
from atlasrag.services.sync.recovery import DocumentRecoveryService
from atlasrag.services.sync.scanner import KnowledgeDirectoryScanner
from atlasrag.services.sync.snapshot import CanonicalDatabaseSnapshotService
from atlasrag.services.sync.verification import GlobalKnowledgeVerificationService


class SyncServices(IndexGraphSteps):
    """Compose graph steps from already-injected business capabilities."""

    def __init__(
        self,
        *,
        reconciliation: StartupReconciliationService,
        scanner: KnowledgeDirectoryScanner,
        database_snapshot: CanonicalDatabaseSnapshotService,
        planner: SyncPlanner,
        build_execution: BuildExecutionService,
        deletion: MissingSourceDeletionService,
        verification: GlobalKnowledgeVerificationService,
        pipeline: str,
    ) -> None:
        self.reconciliation = reconciliation
        self.scanner = scanner
        self.database_snapshot = database_snapshot
        self.planner = planner
        self.build_execution = build_execution
        self.deletion = deletion
        self.verification = verification
        self._pipeline = pipeline

    async def reconcile(self) -> ReconciliationResult:
        return await self.reconciliation.reconcile()

    async def scan(self, knowledge_directory: Path) -> ScanSnapshot:
        return self.scanner.scan(knowledge_directory)

    async def plan(self, scan: ScanSnapshot) -> SyncPlan:
        database = await self.database_snapshot.load()
        return self.planner.plan(
            scan,
            database,
            pipeline_fingerprint=self._pipeline,
        )

    async def execute_builds(
        self,
        plan: SyncPlan,
        scan: ScanSnapshot,
        reconciliation: ReconciliationResult,
    ) -> BuildExecutionSummary:
        return await self.build_execution.execute(plan, scan, reconciliation)

    async def execute_deletions(
        self, plan: SyncPlan, scan: ScanSnapshot
    ) -> DeletionSummary:
        return await self.deletion.delete(plan.deletions, scan=scan)

    async def final_verify(
        self,
        builds: BuildExecutionSummary,
        deletions: DeletionSummary,
    ) -> GlobalSafetyResult:
        return await self.verification.verify(
            deletion_summary=deletions, build_summary=builds
        )


class IndexSyncApplication:
    """Run one maintenance graph; all dependencies are injected by the caller."""

    def __init__(
        self,
        services: IndexGraphSteps,
        *,
        runtime: RuntimeConfig,
        initialize_index: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._services = services
        self._runtime = RuntimeConfig.model_validate(runtime)
        self._initialize_index = initialize_index

    async def run(self, request: SyncRequest) -> SyncResult:
        request = SyncRequest.model_validate(request)
        if request.expected_pipeline_fingerprint != pipeline_fingerprint(self._runtime):
            raise ConfigurationError
        if self._initialize_index is not None:
            await self._initialize_index()
        graph = build_index_graph(self._services)
        output = await graph.ainvoke({"request": request})
        return SyncResult.model_validate(output["final_result"])


class RecoveryLock(Protocol):
    async def release(self) -> None: ...


class RecoveryPool(Protocol):
    pass


class DocumentRecoveryRunner:
    """Own one document's whole recovery/reset/cleanup lease."""

    def __init__(
        self,
        *,
        recovery: DocumentRecoveryService,
        pool: object,
        lock_manager_factory: Callable[[Any], Any],
        metadata_repository_factory: Callable[[Any], Any],
        contention_poll_seconds: float = 0.05,
    ) -> None:
        self._recovery = recovery
        self._pool = pool
        self._manager_factory = lock_manager_factory
        self._metadata_factory = metadata_repository_factory
        self._poll = contention_poll_seconds

    async def _with_lease(
        self,
        document_id: str,
        operation: Callable[[Callable[[], Awaitable[None]]], Awaitable[Any]],
    ) -> Any:
        capture = _LeasePoolCapture(self._pool, self._metadata_factory)  # type: ignore[arg-type]
        manager = self._manager_factory(capture)
        lease = None
        try:
            while lease is None:
                acquisition = asyncio.create_task(manager.try_acquire(document_id))
                try:
                    lease = await asyncio.shield(acquisition)
                except asyncio.CancelledError as cancellation:
                    try:
                        lease = await finish_task(acquisition)
                    except BaseException as error:
                        cancellation.add_note(
                            f"recovery lease acquisition: {type(error).__name__}"
                        )
                    raise cancellation
                if lease is None:
                    await asyncio.sleep(self._poll)
            await capture.probe()
            return await operation(capture.probe)
        finally:
            if lease is not None:
                await settled_operation(capture.release(lease))

    async def resume(self, target: RecoveryTarget, source: SourceFile) -> None:
        async def operation(
            checkpoint: Callable[[], Awaitable[None]],
        ) -> None:
            await self._recovery.resume(target, source, checkpoint=checkpoint)

        await self._with_lease(target.document_id, operation)

    async def cleanup_non_current(self, document_id: str) -> bool:
        async def operation(
            checkpoint: Callable[[], Awaitable[None]],
        ) -> bool:
            await checkpoint()
            return await self._recovery.cleanup_non_current(document_id)

        return bool(await self._with_lease(document_id, operation))

    async def cleanup_candidate(self, document_id: str, revision_id: str) -> bool:
        async def operation(
            checkpoint: Callable[[], Awaitable[None]],
        ) -> bool:
            await checkpoint()
            return await self._recovery.cleanup_candidate(document_id, revision_id)

        return bool(await self._with_lease(document_id, operation))

    async def reset_stale_candidate(
        self, document_id: str, target_revision_id: str
    ) -> None:
        async def operation(
            checkpoint: Callable[[], Awaitable[None]],
        ) -> None:
            await checkpoint()
            await self._recovery.reset_stale_candidate(document_id, target_revision_id)

        await self._with_lease(document_id, operation)
