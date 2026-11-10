"""Bounded build/recovery execution and per-document isolation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from atlasrag.domain.errors import DependencyUnavailableError, SourceMutationError
from atlasrag.domain.ingestion.source import derive_document_id, derive_revision_id
from atlasrag.repositories.postgres.records import FailedStage
from atlasrag.services.indexing.ports import BuildFailureCode
from atlasrag.services.sync.execution import BuildExecutionService
from atlasrag.services.sync.models import (
    BuildExecutionSummary,
    BuildMode,
    BuildOutcome,
    ReconciliationResult,
    RecoveryBoundary,
    RecoveryTarget,
    ResidueCleanup,
)
from atlasrag.services.sync.planner import SyncPlanner
from stage5.helpers import PIPELINE, database, entry, scan


@dataclass
class Result:
    document_id: str
    revision_id: str
    outcome: str
    failed_stage: FailedStage | None = None
    reason_code: BuildFailureCode | None = None


class Recovery:
    def __init__(self) -> None:
        self.resumed: list[object] = []
        self.cleaned: list[tuple[str, str]] = []
        self.resets: list[tuple[str, str]] = []
        self.fail: BaseException | None = None
        self.cleanup_result = True

    async def resume(self, target: object, source: object) -> None:
        if self.fail is not None:
            raise self.fail
        self.resumed.append(target)

    async def cleanup_candidate(self, document_id: str, revision_id: str) -> bool:
        self.cleaned.append((document_id, revision_id))
        return self.cleanup_result

    async def reset_stale_candidate(self, document_id: str, revision_id: str) -> None:
        self.resets.append((document_id, revision_id))


async def execute(
    tmp_path: Path,
    *,
    full: object,
    recovery: Recovery,
    concurrency: int = 2,
    with_recovery: bool = False,
    count: int = 1,
) -> tuple[BuildExecutionSummary, Recovery]:
    entries = [
        entry(tmp_path, f"{index}.txt", f"content {index}") for index in range(count)
    ]
    snapshot = scan(*entries)
    plan = SyncPlanner().plan(snapshot, database(), pipeline_fingerprint=PIPELINE)
    targets: tuple[RecoveryTarget, ...] = ()
    if with_recovery:
        first = entries[0]
        doc_id = derive_document_id(first.source.relative_source_path)
        targets = (
            RecoveryTarget(
                document_id=doc_id,
                revision_id=derive_revision_id(doc_id, first.content_hash, PIPELINE),
                relative_source_path=first.source.relative_source_path,
                boundary=RecoveryBoundary.CHUNKED,
                content_hash=first.content_hash,
                pipeline_fingerprint=PIPELINE,
            ),
        )
    reconciliation = ReconciliationResult(
        outcome="CLEAN" if not targets else "REPAIRED",  # type: ignore[arg-type]
        recovery_targets=targets,
    )
    service = BuildExecutionService(
        full_build=full,  # type: ignore[arg-type]
        recovery=recovery,  # type: ignore[arg-type]
        max_concurrency=concurrency,
    )
    return await service.execute(plan, snapshot, reconciliation), recovery


@pytest.mark.asyncio
async def test_full_build_and_recovery_are_executed_by_mode(tmp_path: Path) -> None:
    async def full(source: object, fingerprint: str, expected_revision: str) -> Result:
        path = source.relative_source_path  # type: ignore[attr-defined]
        return Result(
            document_id=derive_document_id(path),
            revision_id=derive_revision_id(
                derive_document_id(path), entry_hash(path, tmp_path), PIPELINE
            ),
            outcome="READY",
        )

    summary, recovery = await execute(tmp_path, full=full, recovery=Recovery(), count=2)
    assert len(summary.items) == 2
    assert all(item.outcome is BuildOutcome.READY for item in summary.items)
    assert recovery.resumed == []


def entry_hash(path: str, root: Path) -> str:
    from atlasrag.domain.ingestion.source import SourceFile
    from atlasrag.services.ingestion.identity import content_hash

    return content_hash(SourceFile.from_knowledge_directory(root, path))


@pytest.mark.asyncio
async def test_failed_build_is_cleaned_and_does_not_cancel_other_builds(
    tmp_path: Path,
) -> None:
    async def full(source: object, fingerprint: str, expected_revision: str) -> Result:
        path = source.relative_source_path  # type: ignore[attr-defined]
        doc = derive_document_id(path)
        revision = derive_revision_id(doc, entry_hash(path, tmp_path), PIPELINE)
        if path == "0.txt":
            return Result(
                document_id=doc,
                revision_id=revision,
                outcome="FAILED",
                failed_stage=FailedStage.INDEX_WRITE,
                reason_code=BuildFailureCode.INDEX_WRITE,
            )
        return Result(document_id=doc, revision_id=revision, outcome="READY")

    summary, recovery = await execute(tmp_path, full=full, recovery=Recovery(), count=2)
    failed = [item for item in summary.items if item.outcome is BuildOutcome.FAILED]
    ready = [item for item in summary.items if item.outcome is BuildOutcome.READY]
    assert len(failed) == 1 and len(ready) == 1
    assert failed[0].residue_cleanup is ResidueCleanup.CLEANED
    assert recovery.cleaned == [(failed[0].document_id, failed[0].revision_id)]


@pytest.mark.asyncio
async def test_recovery_target_uses_resume_mode(tmp_path: Path) -> None:
    async def full(source: object, fingerprint: str, expected_revision: str) -> Result:
        raise AssertionError("recovery must not run a full build")

    summary, recovery = await execute(
        tmp_path, full=full, recovery=Recovery(), with_recovery=True
    )
    assert summary.items[0].mode is BuildMode.RESUME_CHUNKED
    assert len(recovery.resumed) == 1


@pytest.mark.asyncio
async def test_source_mutation_is_not_a_business_failure(tmp_path: Path) -> None:
    async def full(source: object, fingerprint: str, expected_revision: str) -> Result:
        raise SourceMutationError

    summary, _ = await execute(tmp_path, full=full, recovery=Recovery())
    assert summary.items[0].outcome is BuildOutcome.SOURCE_MUTATED
    assert summary.items[0].failed_stage is None


@pytest.mark.asyncio
async def test_technical_failure_is_not_recorded_as_business_failed(
    tmp_path: Path,
) -> None:
    async def full(source: object, fingerprint: str, expected_revision: str) -> Result:
        raise DependencyUnavailableError

    summary, recovery = await execute(tmp_path, full=full, recovery=Recovery())
    item = summary.items[0]
    assert item.outcome is BuildOutcome.TECHNICAL_FAILURE
    assert item.technical_code == "DEPENDENCY_UNAVAILABLE"
    assert item.failed_stage is None
    assert recovery.cleaned == [(item.document_id, item.revision_id)]


@pytest.mark.asyncio
async def test_concurrency_is_bounded_and_isolated(tmp_path: Path) -> None:
    active = 0
    peak = 0

    async def full(source: object, fingerprint: str, expected_revision: str) -> Result:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        path = source.relative_source_path  # type: ignore[attr-defined]
        doc = derive_document_id(path)
        return Result(
            document_id=doc,
            revision_id=derive_revision_id(doc, entry_hash(path, tmp_path), PIPELINE),
            outcome="READY",
        )

    summary, _ = await execute(
        tmp_path, full=full, recovery=Recovery(), concurrency=1, count=3
    )
    assert peak == 1
    assert len(summary.ready) == 3
