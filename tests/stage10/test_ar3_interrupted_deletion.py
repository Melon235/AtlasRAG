"""A-R3 interrupted required deletion, durable safety and restart evidence."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from ar3_support import (
    AR3Environment,
    build_environment,
    rebuild_environment,
    start_runtime,
)

from atlasrag.domain.errors import (
    DependencyUnavailableError,
    ProviderResponseError,
    RuntimeNotReadyError,
)
from atlasrag.services.sync.models import (
    BuildExecutionSummary,
    DeletionSummary,
    ServingSafety,
    SyncOutcome,
)


class SimulatedInterruption(RuntimeError):
    """Controlled process-interruption boundary inside a transaction."""


async def _interrupt_sync_at_probe(
    environment: AR3Environment, probe_number: int
) -> None:
    environment.leases.block_probe = probe_number
    task = asyncio.create_task(environment.run_sync())
    await asyncio.wait_for(environment.leases.probe_reached.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)


def _durable_document_present(environment: AR3Environment) -> bool:
    assert environment.stored is not None
    return environment.stored.document_id in environment.database.documents


@pytest.mark.asyncio
async def test_interruption_before_milvus_deletion_preserves_both_stores(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    original_rows = tuple(row.model_dump() for row in environment.rows)
    (tmp_path / "guides" / "candidate.txt").unlink()
    await _interrupt_sync_at_probe(environment, 2)

    assert environment.sync_calls == 1
    assert environment.sync_results == []
    assert _durable_document_present(environment)
    assert environment.rows
    assert tuple(row.model_dump() for row in environment.rows) == original_rows
    assert environment.database.query_cache_invalidation_required is False
    assert environment.leases.released == 1
    assert environment.leases.probe_calls == 2

    fresh = rebuild_environment(environment)
    runtime = await start_runtime(fresh)

    assert runtime.gate.mode.value == "BLOCKED"
    assert fresh.source_safety_calls == 1
    assert fresh.verify_calls == 0
    assert fresh.sync_calls == 0
    assert _durable_document_present(fresh)
    assert fresh.rows
    with pytest.raises(RuntimeNotReadyError):
        await runtime.coordinator.run_query(object())  # type: ignore[arg-type]
    assert runtime.retrieval.calls == 0

    verification = await fresh.verification_service.verify(
        deletion_summary=DeletionSummary(),
        build_summary=BuildExecutionSummary(),
    )
    assert verification.serving_safety is ServingSafety.SAFE_TO_SERVE


@pytest.mark.asyncio
async def test_milvus_delete_failure_keeps_canonical_state_and_blocks_serving(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    (tmp_path / "guides" / "candidate.txt").unlink()
    environment.index.fail_delete_document = DependencyUnavailableError()

    result = await environment.run_sync()

    assert result.outcome is SyncOutcome.FAILED
    assert result.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "REQUIRED_DELETION_FAILED" in result.unsafe_reasons
    assert result.failed_deletions == 1
    assert result.deleted_documents == 0
    assert _durable_document_present(environment)
    assert environment.rows
    assert environment.database.query_cache_invalidation_required is False
    assert environment.leases.released == 1

    fresh = rebuild_environment(environment)
    runtime = await start_runtime(fresh)
    assert runtime.gate.mode.value == "BLOCKED"
    assert fresh.verify_calls == 0
    with pytest.raises(RuntimeNotReadyError):
        await runtime.coordinator.run_query(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_remaining_milvus_projection_is_unsafe_and_not_serving(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    (tmp_path / "guides" / "candidate.txt").unlink()
    environment.index.partial_delete = True

    result = await environment.run_sync()

    assert result.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert result.failed_deletions == 1
    assert "REQUIRED_DELETION_FAILED" in result.unsafe_reasons
    assert _durable_document_present(environment)
    assert environment.rows
    assert environment.database.query_cache_invalidation_required is False
    assert environment.leases.released == 1

    fresh = rebuild_environment(environment)
    runtime = await start_runtime(fresh)
    assert runtime.gate.mode.value == "BLOCKED"
    assert fresh.verify_calls == 0
    verification = await fresh.verification_service.verify(
        deletion_summary=DeletionSummary(),
        build_summary=BuildExecutionSummary(),
    )
    assert verification.serving_safety is ServingSafety.SAFE_TO_SERVE
    assert "REQUIRED_DELETION_FAILED" not in verification.unsafe_reasons
    with pytest.raises(RuntimeNotReadyError):
        await runtime.coordinator.run_query(object())  # type: ignore[arg-type]
    assert runtime.retrieval.calls == 0


@pytest.mark.asyncio
async def test_failed_absence_verification_keeps_postgres_and_blocks_serving(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    (tmp_path / "guides" / "candidate.txt").unlink()
    environment.index.projection_error = ProviderResponseError()

    result = await environment.run_sync()

    assert result.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert result.failed_deletions == 1
    assert result.deleted_documents == 0
    assert _durable_document_present(environment)
    assert environment.rows == []
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
    assert "MISSING_RECORDS" in verification.unsafe_reasons


@pytest.mark.asyncio
async def test_interruption_before_postgres_transaction_keeps_canonical_row(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    (tmp_path / "guides" / "candidate.txt").unlink()
    await _interrupt_sync_at_probe(environment, 3)

    assert environment.sync_results == []
    assert _durable_document_present(environment)
    assert environment.rows == []
    assert environment.database.query_cache_invalidation_required is False
    assert environment.leases.released == 1
    assert environment.leases.probe_calls == 3

    fresh = rebuild_environment(environment)
    runtime = await start_runtime(fresh)

    assert runtime.gate.mode.value == "BLOCKED"
    assert fresh.verify_calls == 0
    assert _durable_document_present(fresh)
    assert fresh.rows == []
    verification = await fresh.verification_service.verify(
        deletion_summary=DeletionSummary(),
        build_summary=BuildExecutionSummary(),
    )
    assert verification.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "MISSING_RECORDS" in verification.unsafe_reasons
    with pytest.raises(RuntimeNotReadyError):
        await runtime.coordinator.run_query(object())  # type: ignore[arg-type]
    assert runtime.retrieval.calls == 0


@pytest.mark.asyncio
async def test_interruption_inside_postgres_transaction_rolls_back(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    (tmp_path / "guides" / "candidate.txt").unlink()
    scene = environment.factory
    scene.fail_after("chunks.delete_document", SimulatedInterruption())

    with pytest.raises(SimulatedInterruption):
        await asyncio.wait_for(environment.run_sync(), timeout=2)

    assert _durable_document_present(environment)
    assert environment.rows == []
    assert environment.database.query_cache_invalidation_required is False
    assert environment.leases.released == 1
    assert environment.factory.rollback_count >= 1

    fresh = rebuild_environment(environment)
    runtime = await start_runtime(fresh)
    assert runtime.gate.mode.value == "BLOCKED"
    verification = await fresh.verification_service.verify(
        deletion_summary=DeletionSummary(),
        build_summary=BuildExecutionSummary(),
    )
    assert verification.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "MISSING_RECORDS" in verification.unsafe_reasons


@pytest.mark.asyncio
async def test_postgres_commit_failure_rolls_back_deletion_and_marker(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    (tmp_path / "guides" / "candidate.txt").unlink()
    environment.factory.fail_after("uow.commit", DependencyUnavailableError())

    result = await environment.run_sync()

    assert result.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert result.failed_deletions == 1
    assert _durable_document_present(environment)
    assert environment.rows == []
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
    assert "MISSING_RECORDS" in verification.unsafe_reasons


@pytest.mark.asyncio
async def test_successful_deletion_commits_marker_with_canonical_state(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    (tmp_path / "guides" / "candidate.txt").unlink()

    result = await environment.run_sync()

    assert result.outcome is SyncOutcome.SUCCESS
    assert result.serving_safety is ServingSafety.SAFE_TO_SERVE
    assert result.deleted_documents == 1
    assert result.failed_deletions == 0
    assert environment.database.documents == {}
    assert environment.database.elements == {}
    assert environment.database.chunks == {}
    assert environment.database.query_cache_invalidation_required is True
    assert environment.rows == []
    assert environment.leases.released == 1
    assert "document_projection" in environment.index.events
    assert environment.factory.calls.index("documents.delete") > (
        environment.index.events.index("document_projection")
    )
    assert "runtime_metadata.set" in environment.factory.calls


@pytest.mark.asyncio
async def test_interruption_after_deletion_commit_restarts_to_serving(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    (tmp_path / "guides" / "candidate.txt").unlink()
    environment.verification.gate = asyncio.Event()
    task = asyncio.create_task(environment.run_sync())
    await asyncio.wait_for(environment.verification.reached.wait(), timeout=2)

    assert environment.database.documents == {}
    assert environment.rows == []
    assert environment.database.query_cache_invalidation_required is True

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    fresh = rebuild_environment(environment)
    runtime = await start_runtime(fresh)

    assert runtime.gate.mode.value == "SERVING"
    assert fresh.verify_calls == 1
    assert fresh.sync_calls == 0
    assert runtime.freshness.calls == 1
    assert runtime.freshness.marker_seen is True
    assert await runtime.coordinator.readiness() is True
    envelope = await runtime.coordinator.run_query(object())  # type: ignore[arg-type]
    assert envelope.response.answer_text == "answer"
    assert fresh.order.index("verify") < fresh.order.index("freshness")
    await runtime.coordinator.shutdown()


@pytest.mark.asyncio
async def test_failed_required_deletion_chain_blocks_runtime_before_restart(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    runtime = await start_runtime(environment)
    assert runtime.gate.mode.value == "SERVING"
    assert environment.stored is not None
    (tmp_path / "guides" / "candidate.txt").unlink()
    environment.index.fail_delete_document = DependencyUnavailableError()

    result = await runtime.coordinator.run_maintenance()

    assert result.outcome is SyncOutcome.FAILED
    assert result.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert result.failed_deletions == 1
    assert "REQUIRED_DELETION_FAILED" in result.unsafe_reasons
    assert runtime.gate.mode.value == "BLOCKED"
    assert runtime.health.snapshot is not None
    assert runtime.health.snapshot.knowledge_verified is False
    assert await runtime.coordinator.readiness() is False
    with pytest.raises(RuntimeNotReadyError):
        await runtime.coordinator.run_query(object())  # type: ignore[arg-type]
    assert runtime.retrieval.calls == 0
    assert _durable_document_present(environment)
    assert environment.rows
    assert environment.database.query_cache_invalidation_required is False

    fresh = rebuild_environment(environment)
    fresh_runtime = await start_runtime(fresh)
    assert fresh_runtime.gate.mode.value == "BLOCKED"
    assert fresh.sync_calls == 0
    assert fresh.verify_calls == 0
    verification = await fresh.verification_service.verify(
        deletion_summary=DeletionSummary(),
        build_summary=BuildExecutionSummary(),
    )
    assert verification.serving_safety is ServingSafety.SAFE_TO_SERVE


@pytest.mark.asyncio
async def test_restart_with_mismatched_projection_is_unsafe_with_empty_summaries(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    (tmp_path / "guides" / "candidate.txt").unlink()
    await _interrupt_sync_at_probe(environment, 3)

    fresh = rebuild_environment(environment)
    runtime = await start_runtime(fresh)

    assert runtime.gate.mode.value == "BLOCKED"
    assert fresh.verify_calls == 0
    verification = await fresh.verification_service.verify(
        deletion_summary=DeletionSummary(),
        build_summary=BuildExecutionSummary(),
    )
    assert verification.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "MISSING_RECORDS" in verification.unsafe_reasons
    assert verification.required_deletion_failures == 0
    with pytest.raises(RuntimeNotReadyError):
        await runtime.coordinator.run_query(object())  # type: ignore[arg-type]
