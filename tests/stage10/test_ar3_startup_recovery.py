"""A-R3 source reappearance, scan safety and explicit BLOCKED recovery."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from ar3_support import (
    AR3Environment,
    AR3Runtime,
    build_environment,
    rebuild_environment,
    start_runtime,
)

from atlasrag.domain.errors import (
    DependencyUnavailableError,
    RuntimeNotReadyError,
)
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.services.sync.models import (
    BuildExecutionSummary,
    DeletionSummary,
    ServingSafety,
    SyncOutcome,
)


async def _blocked_runtime(environment: AR3Environment) -> AR3Runtime:
    runtime = await start_runtime(environment)
    assert runtime.gate.mode.value == "BLOCKED"
    snapshot = await runtime.coordinator.health()
    assert snapshot.knowledge_verified is False
    return runtime


@pytest.mark.asyncio
async def test_source_reappearance_after_planning_blocks_deletion(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    source = tmp_path / "guides" / "candidate.txt"
    original_bytes = source.read_bytes()
    source.unlink()
    environment.leases.block_probe = 1
    task = asyncio.create_task(environment.run_sync())
    await asyncio.wait_for(environment.leases.probe_reached.wait(), timeout=2)

    source.write_bytes(original_bytes)
    environment.leases.probe_release.set()
    result = await asyncio.wait_for(task, timeout=2)

    assert result.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "REQUIRED_DELETION_FAILED" in result.unsafe_reasons
    assert result.failed_deletions == 1
    assert environment.stored.document_id in environment.database.documents
    assert environment.rows
    assert environment.database.query_cache_invalidation_required is False
    assert environment.leases.released == 1

    fresh = rebuild_environment(environment)
    runtime = await start_runtime(fresh)
    assert runtime.gate.mode.value == "SERVING"
    assert fresh.sync_calls == 0
    assert fresh.verify_calls == 1


@pytest.mark.asyncio
async def test_incomplete_scan_blocks_before_any_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = await build_environment(tmp_path)
    runtime = await start_runtime(environment)
    assert runtime.gate.mode.value == "SERVING"
    assert environment.stored is not None
    original_rows = tuple(row.model_dump() for row in environment.rows)
    (tmp_path / "guides" / "candidate.txt").unlink()

    def denied(path: object) -> object:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "scandir", denied)
    result = await runtime.coordinator.run_maintenance()

    assert result.outcome is SyncOutcome.FAILED
    assert result.blocked_code == "SCAN_PERMISSION_DENIED"
    assert result.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert runtime.gate.mode.value == "BLOCKED"
    assert environment.stored.document_id in environment.database.documents
    assert tuple(row.model_dump() for row in environment.rows) == original_rows
    assert environment.database.query_cache_invalidation_required is False
    assert environment.leases.released == 0
    with pytest.raises(RuntimeNotReadyError):
        await runtime.coordinator.run_query(object())  # type: ignore[arg-type]
    assert runtime.retrieval.calls == 0


@pytest.mark.asyncio
async def test_changed_canonical_revision_after_planning_blocks_deletion(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    (tmp_path / "guides" / "candidate.txt").unlink()
    environment.leases.block_probe = 1
    task = asyncio.create_task(environment.run_sync())
    await asyncio.wait_for(environment.leases.probe_reached.wait(), timeout=2)

    changed = environment.stored.model_copy(
        update={"current_revision_id": "rev_" + "9" * 64}
    )
    environment.database.documents[changed.document_id] = changed
    environment.leases.probe_release.set()
    result = await asyncio.wait_for(task, timeout=2)

    assert result.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert result.failed_deletions == 1
    assert environment.rows
    assert environment.database.query_cache_invalidation_required is False
    assert environment.leases.released == 1

    fresh = rebuild_environment(environment)
    runtime = await start_runtime(fresh)
    assert runtime.gate.mode.value == "BLOCKED"
    verification = await fresh.verification_service.verify(
        deletion_summary=DeletionSummary(),
        build_summary=BuildExecutionSummary(),
    )
    assert verification.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    with pytest.raises(RuntimeNotReadyError):
        await runtime.coordinator.run_query(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_failed_explicit_recovery_keeps_blocked_and_admission_closed(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    (tmp_path / "guides" / "candidate.txt").unlink()
    runtime = await _blocked_runtime(environment)
    assert environment.stored is not None
    original_rows = tuple(row.model_dump() for row in environment.rows)
    environment.index.fail_delete_document = DependencyUnavailableError()

    result = await runtime.coordinator.recover()

    assert result.outcome is SyncOutcome.FAILED
    assert result.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert result.failed_deletions == 1
    assert runtime.gate.mode.value == "BLOCKED"
    assert runtime.health.snapshot is not None
    assert runtime.health.snapshot.knowledge_verified is False
    assert await runtime.coordinator.readiness() is False
    assert environment.sync_calls == 1
    assert environment.stored.document_id in environment.database.documents
    assert tuple(row.model_dump() for row in environment.rows) == original_rows
    assert environment.database.query_cache_invalidation_required is False
    with pytest.raises(RuntimeNotReadyError):
        await runtime.coordinator.run_query(object())  # type: ignore[arg-type]
    assert runtime.retrieval.calls == 0


@pytest.mark.asyncio
async def test_successful_explicit_recovery_verifies_knowledge_and_opens_admission(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    (tmp_path / "guides" / "candidate.txt").unlink()
    runtime = await _blocked_runtime(environment)
    assert environment.stored is not None

    result = await runtime.coordinator.recover()

    assert result.outcome is SyncOutcome.SUCCESS
    assert result.serving_safety is ServingSafety.SAFE_TO_SERVE
    assert result.deleted_documents == 1
    assert runtime.gate.mode.value == "SERVING"
    assert await runtime.coordinator.readiness() is True
    assert runtime.health.snapshot is not None
    assert runtime.health.snapshot.knowledge_verified is True
    assert runtime.health.snapshot.hard_healthy is True
    assert runtime.freshness.calls == 1
    assert runtime.freshness.marker_seen is True
    assert environment.sync_calls == 1
    assert environment.database.documents == {}
    assert environment.rows == []
    assert environment.database.query_cache_invalidation_required is True
    assert environment.leases.released == 1
    assert environment.order.index("sync") < environment.order.index("freshness")

    envelope = await runtime.coordinator.run_query(object())  # type: ignore[arg-type]
    assert envelope.response.answer_text == "answer"
    assert runtime.retrieval.calls == 1
    await runtime.coordinator.shutdown()


@pytest.mark.asyncio
async def test_restart_after_interrupted_deletion_never_runs_automatic_sync(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    (tmp_path / "guides" / "candidate.txt").unlink()
    await _blocked_runtime(environment)

    fresh = rebuild_environment(environment)
    runtime = await start_runtime(fresh)

    assert runtime.gate.mode.value == "BLOCKED"
    assert fresh.sync_calls == 0
    assert fresh.verify_calls == 0
    assert fresh.database.query_cache_invalidation_required is False
    assert fresh.rows
    with pytest.raises(RuntimeNotReadyError):
        await runtime.coordinator.run_query(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_runtime_mode_vocabulary_has_no_blocked_to_serving_shortcut() -> None:
    assert {mode.value for mode in RuntimeMode} == {
        "STARTING",
        "SERVING",
        "DRAINING",
        "MAINTENANCE",
        "BLOCKED",
        "SHUTTING_DOWN",
    }
