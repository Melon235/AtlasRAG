"""Bounded plan-build and durable-recovery execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from atlasrag.domain.errors import (
    DependencyError,
    ProviderResponseError,
    ResourceExhaustedError,
    SourceMutationError,
)
from atlasrag.domain.ingestion.source import SourceFile
from atlasrag.services.indexing.build import BuildBusinessFailure
from atlasrag.services.sync.models import (
    BuildExecutionItem,
    BuildExecutionSummary,
    BuildMode,
    BuildOutcome,
    ReconciliationResult,
    RecoveryTarget,
    ResidueCleanup,
    ScanSnapshot,
    SyncAction,
    SyncActionKind,
    SyncPlan,
)
from atlasrag.services.sync.ports import FullBuildResult
from atlasrag.services.sync.recovery import DocumentRecoveryService

FullBuildExecutor = Callable[[SourceFile, str, str], Awaitable[FullBuildResult]]
_BUSINESS_FAILURE = {BuildOutcome.FAILED, BuildOutcome.TECHNICAL_FAILURE}


class BuildExecutionService:
    """Run builds before deletions, bounded across independent documents."""

    def __init__(
        self,
        *,
        full_build: FullBuildExecutor,
        recovery: DocumentRecoveryService,
        max_concurrency: int,
    ) -> None:
        if type(max_concurrency) is not int or max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")
        self._full_build = full_build
        self._recovery = recovery
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def _ready(self, item: BuildExecutionItem) -> BuildExecutionItem:
        cleanup = getattr(self._recovery, "cleanup_non_current", None)
        if not callable(cleanup):
            return item
        try:
            cleaned = await cleanup(item.document_id)
        except (DependencyError, ProviderResponseError, ResourceExhaustedError):
            cleaned = False
        return item.model_copy(
            update={
                "residue_cleanup": (
                    ResidueCleanup.CLEANED if cleaned else ResidueCleanup.FAILED
                )
            }
        )

    async def _cleanup(self, item: BuildExecutionItem) -> BuildExecutionItem:
        if item.outcome not in _BUSINESS_FAILURE:
            return item
        try:
            cleaned = await self._recovery.cleanup_candidate(
                item.document_id, item.revision_id
            )
        except (DependencyError, ProviderResponseError, ResourceExhaustedError):
            cleaned = False
        return item.model_copy(
            update={
                "residue_cleanup": (
                    ResidueCleanup.CLEANED if cleaned else ResidueCleanup.FAILED
                )
            }
        )

    async def _full(
        self, action: SyncAction, scan: ScanSnapshot, pipeline_fingerprint: str
    ) -> BuildExecutionItem:
        entry = scan.by_path().get(action.relative_source_path)
        if entry is None or action.revision_id is None:
            raise AssertionError("BUILD action requires a scanned source and revision")
        await self._recovery.reset_stale_candidate(
            action.document_id, action.revision_id
        )
        result = await self._full_build(
            entry.source, pipeline_fingerprint, action.revision_id
        )
        if result.revision_id != action.revision_id:
            raise AssertionError("full build revision changed during execution")
        if result.outcome == "READY":
            return await self._ready(
                BuildExecutionItem(
                    document_id=result.document_id,
                    revision_id=result.revision_id,
                    mode=BuildMode.FULL,
                    outcome=BuildOutcome.READY,
                )
            )
        if result.outcome == "SOURCE_MUTATED":
            return BuildExecutionItem(
                document_id=result.document_id,
                revision_id=result.revision_id,
                mode=BuildMode.FULL,
                outcome=BuildOutcome.SOURCE_MUTATED,
            )
        assert result.failed_stage is not None and result.reason_code is not None
        item = BuildExecutionItem(
            document_id=result.document_id,
            revision_id=result.revision_id,
            mode=BuildMode.FULL,
            outcome=BuildOutcome.FAILED,
            failed_stage=result.failed_stage,
            reason_code=result.reason_code.value,
            residue_cleanup=ResidueCleanup.FAILED,
        )
        return await self._cleanup(item)

    async def _resume(
        self,
        action: SyncAction,
        scan: ScanSnapshot,
        target: RecoveryTarget,
    ) -> BuildExecutionItem:
        entry = scan.by_path().get(action.relative_source_path)
        if entry is None or action.revision_id is None:
            raise AssertionError("recovery requires a scanned source and revision")
        await self._recovery.resume(target, entry.source)
        return await self._ready(
            BuildExecutionItem(
                document_id=action.document_id,
                revision_id=target.revision_id,
                mode=(
                    BuildMode.RESUME_PARSED
                    if target.boundary.value == "PARSED"
                    else BuildMode.RESUME_CHUNKED
                ),
                outcome=BuildOutcome.READY,
            )
        )

    async def _run(
        self,
        action: SyncAction,
        scan: ScanSnapshot,
        pipeline_fingerprint: str,
        target: RecoveryTarget | None,
    ) -> BuildExecutionItem:
        async with self._semaphore:
            try:
                if target is not None:
                    return await self._resume(action, scan, target)
                return await self._full(action, scan, pipeline_fingerprint)
            except BuildBusinessFailure as error:
                revision = action.revision_id or ""
                item = BuildExecutionItem(
                    document_id=action.document_id,
                    revision_id=revision,
                    mode=(
                        BuildMode.RESUME_PARSED
                        if target is not None and target.boundary.value == "PARSED"
                        else (
                            BuildMode.RESUME_CHUNKED
                            if target is not None
                            else BuildMode.FULL
                        )
                    ),
                    outcome=BuildOutcome.FAILED,
                    failed_stage=error.stage,
                    reason_code=error.reason.value,
                    residue_cleanup=ResidueCleanup.FAILED,
                )
                return await self._cleanup(item)
            except SourceMutationError:
                return BuildExecutionItem(
                    document_id=action.document_id,
                    revision_id=action.revision_id or "",
                    mode=(
                        BuildMode.RESUME_PARSED
                        if target is not None and target.boundary.value == "PARSED"
                        else (
                            BuildMode.RESUME_CHUNKED
                            if target is not None
                            else BuildMode.FULL
                        )
                    ),
                    outcome=BuildOutcome.SOURCE_MUTATED,
                )
            except (
                DependencyError,
                ProviderResponseError,
                ResourceExhaustedError,
            ) as error:
                revision = action.revision_id or ""
                item = BuildExecutionItem(
                    document_id=action.document_id,
                    revision_id=revision,
                    mode=(
                        BuildMode.RESUME_PARSED
                        if target is not None and target.boundary.value == "PARSED"
                        else (
                            BuildMode.RESUME_CHUNKED
                            if target is not None
                            else BuildMode.FULL
                        )
                    ),
                    outcome=BuildOutcome.TECHNICAL_FAILURE,
                    technical_code=error.code.value,
                    residue_cleanup=ResidueCleanup.FAILED,
                )
                return await self._cleanup(item)

    async def execute(
        self,
        plan: SyncPlan,
        scan: ScanSnapshot,
        reconciliation: ReconciliationResult,
    ) -> BuildExecutionSummary:
        targets = {
            target.document_id: target for target in reconciliation.recovery_targets
        }
        actions = tuple(
            action for action in plan.actions if action.kind is SyncActionKind.BUILD
        )
        tasks = [
            asyncio.create_task(
                self._run(
                    action,
                    scan,
                    plan.pipeline_fingerprint,
                    targets.get(action.document_id),
                )
            )
            for action in actions
        ]
        if not tasks:
            return BuildExecutionSummary()
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        items: list[BuildExecutionItem] = []
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                raise result
            items.append(result)
        return BuildExecutionSummary(
            items=tuple(sorted(items, key=lambda item: item.document_id))
        )
