"""A-R3 startup source availability, path classification and root safety."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from ar3_support import (
    build_environment,
    delete_action,
    empty_document_without_current,
    rebuild_environment,
    start_runtime,
)

from atlasrag.domain.errors import (
    RuntimeBootstrapFailedError,
    RuntimeNotReadyError,
)
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.services.sync.models import (
    BuildExecutionSummary,
    DeletionOutcome,
    DeletionSummary,
    ScanSnapshot,
    ServingSafety,
    SourceAvailability,
    SyncAction,
    SyncActionKind,
)


@pytest.mark.asyncio
async def test_startup_serves_available_unchanged_source_without_sync(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    runtime = await start_runtime(environment)

    assert runtime.gate.mode.value == "SERVING"
    assert await runtime.coordinator.readiness() is True
    assert environment.sync_calls == 0
    assert environment.verify_calls == 1
    assert runtime.freshness.calls == 1
    envelope = await runtime.coordinator.run_query(object())  # type: ignore[arg-type]
    assert envelope.response.answer_text == "answer"
    assert runtime.retrieval.calls == 1
    assert environment.stored is not None
    assert runtime.gate.active_queries == 0
    await runtime.coordinator.shutdown()


@pytest.mark.asyncio
async def test_changed_available_source_keeps_last_good_revision_without_rebuild(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    original_revision = environment.stored.current_revision_id
    original_rows = tuple(row.model_dump() for row in environment.rows)
    (tmp_path / "guides" / "candidate.txt").write_bytes(b"changed source bytes")

    runtime = await start_runtime(environment)

    assert runtime.gate.mode.value == "SERVING"
    assert environment.sync_calls == 0
    assert runtime.coordinator is not None
    stored = environment.database.documents[environment.stored.document_id]
    assert stored.current_revision_id == original_revision
    assert tuple(row.model_dump() for row in environment.rows) == original_rows


@pytest.mark.asyncio
async def test_confirmed_missing_published_source_blocks_startup(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    (tmp_path / "guides" / "candidate.txt").unlink()

    runtime = await start_runtime(environment)

    assert runtime.gate.mode.value == "BLOCKED"
    assert environment.source_safety_calls == 1
    assert environment.verify_calls == 0
    assert environment.sync_calls == 0
    assert environment.stored is not None
    assert environment.stored.document_id in environment.database.documents
    assert environment.rows
    assert await runtime.coordinator.readiness() is False
    with pytest.raises(RuntimeNotReadyError):
        await runtime.coordinator.run_query(object())  # type: ignore[arg-type]
    assert runtime.retrieval.calls == 0
    assert runtime.gate.active_queries == 0


@pytest.mark.asyncio
async def test_document_without_current_revision_does_not_block_startup(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    empty_document_without_current(environment)

    runtime = await start_runtime(environment)

    assert runtime.gate.mode.value == "SERVING"
    assert environment.sync_calls == 0
    assert environment.source_safety is not None
    result = await environment.source_safety.check()
    assert result.safe
    assert result.checked_documents == 0


@pytest.mark.asyncio
async def test_empty_consistent_kb_serves(tmp_path: Path) -> None:
    environment = await build_environment(tmp_path, seed=False)
    runtime = await start_runtime(environment)

    assert runtime.gate.mode.value == "SERVING"
    assert await runtime.coordinator.readiness() is True
    assert environment.verify_calls == 1
    envelope = await runtime.coordinator.run_query(object())  # type: ignore[arg-type]
    assert envelope.response.answer_text == "answer"
    assert environment.sync_calls == 0


@pytest.mark.asyncio
async def test_missing_knowledge_root_is_a_bootstrap_failure(tmp_path: Path) -> None:
    environment = await build_environment(tmp_path)
    broken = rebuild_environment(environment, root=tmp_path / "missing-root")

    with pytest.raises(RuntimeBootstrapFailedError):
        await start_runtime(broken)

    assert broken.reconcile_calls == 1
    assert broken.source_safety_calls == 0
    assert broken.order.count("release") == 1
    assert broken.order.count("close") == 1


@pytest.mark.asyncio
async def test_non_directory_knowledge_root_is_a_bootstrap_failure(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    root_file = tmp_path / "not-a-directory"
    root_file.write_text("file", encoding="utf-8")
    broken = rebuild_environment(environment, root=root_file)

    with pytest.raises(RuntimeBootstrapFailedError):
        await start_runtime(broken)
    assert broken.reconcile_calls == 1
    assert broken.verify_calls == 0


@pytest.mark.asyncio
async def test_unreadable_knowledge_root_is_a_bootstrap_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = await build_environment(tmp_path, seed=False)

    def denied(path: object) -> object:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(RuntimeBootstrapFailedError):
        await start_runtime(environment)
    assert environment.reconcile_calls == 1
    assert environment.source_safety_calls == 1
    assert environment.verify_calls == 0


@pytest.mark.asyncio
async def test_blocked_reconciliation_is_initialized_blocked_not_bootstrap_failure(
    tmp_path: Path,
) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    broken = environment.stored.model_copy(
        update={"parse_metadata": {"schema_version": 99}}
    )
    environment.database.documents[broken.document_id] = broken

    runtime = await start_runtime(environment)

    assert runtime.gate.mode.value == "BLOCKED"
    assert environment.source_safety_calls == 0
    assert environment.verify_calls == 0
    with pytest.raises(RuntimeNotReadyError):
        await runtime.coordinator.run_query(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_global_verification_failure_is_not_overridden_by_source_safety(
    tmp_path: Path,
) -> None:
    environment = await build_environment(
        tmp_path, with_current_repair=False, with_reconcile_index=False
    )
    assert environment.rows
    environment.rows.pop()

    runtime = await start_runtime(environment)

    assert runtime.gate.mode.value == "BLOCKED"
    assert environment.source_safety_calls == 1
    assert environment.verify_calls == 1
    verification = await environment.verification_service.verify(
        deletion_summary=DeletionSummary(), build_summary=BuildExecutionSummary()
    )
    assert verification.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "MISSING_RECORDS" in verification.unsafe_reasons
    with pytest.raises(RuntimeNotReadyError):
        await runtime.coordinator.run_query(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_permission_denied_source_is_unavailable_not_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = await build_environment(tmp_path)
    _deny_source_existence(monkeypatch)

    assert environment.stored is not None
    availability, digest = environment.reconciliation.classify_source(
        environment.stored
    )
    assert availability is SourceAvailability.UNAVAILABLE
    assert digest is None


@pytest.mark.asyncio
async def test_io_error_source_is_unavailable(tmp_path: Path) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None

    import pathlib

    original = pathlib.Path.lstat

    def failing(self: Path) -> os.stat_result:
        if self.name == "candidate.txt":
            raise OSError(5, "Input/output error")
        return original(self)

    pathlib.Path.lstat = failing  # type: ignore[method-assign]
    try:
        availability, _ = environment.reconciliation.classify_source(environment.stored)
    finally:
        pathlib.Path.lstat = original  # type: ignore[method-assign]
    assert availability is SourceAvailability.UNAVAILABLE


@pytest.mark.asyncio
async def test_invalid_source_identity_is_unavailable(tmp_path: Path) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    unsafe = environment.stored.model_copy(
        update={"relative_source_path": "../escape.txt"}
    )
    environment.database.documents[environment.stored.document_id] = unsafe

    availability, digest = environment.reconciliation.classify_source(unsafe)

    assert availability is SourceAvailability.UNAVAILABLE
    assert digest is None


@pytest.mark.asyncio
async def test_deletion_refuses_unreadable_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = await build_environment(tmp_path)
    _deny_source_existence(monkeypatch)
    assert environment.stored is not None

    summary = await environment.deletion.delete(
        (delete_action(environment.stored),), scan=ScanSnapshot()
    )

    item = summary.items[0]
    assert item.outcome is DeletionOutcome.FAILED
    assert item.failure_code == "SOURCE_UNAVAILABLE"
    assert environment.stored.document_id in environment.database.documents
    assert environment.rows
    assert environment.leases.released == 1
    assert environment.database.query_cache_invalidation_required is False


@pytest.mark.asyncio
async def test_deletion_refuses_unavailable_knowledge_root(tmp_path: Path) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    broken = rebuild_environment(environment, root=tmp_path / "missing-root")

    summary = await broken.deletion.delete(
        (delete_action(environment.stored),), scan=ScanSnapshot()
    )

    assert summary.items[0].outcome is DeletionOutcome.FAILED
    assert summary.items[0].failure_code == "SOURCE_UNAVAILABLE"
    assert environment.stored.document_id in environment.database.documents
    assert environment.rows
    assert broken.leases.released == 1


@pytest.mark.asyncio
async def test_deletion_refuses_invalid_source_identity(tmp_path: Path) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    unsafe_action = SyncAction.model_construct(
        kind=SyncActionKind.DELETE,
        document_id=environment.stored.document_id,
        relative_source_path="../escape.txt",
        reason=None,
        content_hash=None,
        revision_id=None,
        current_revision_id=environment.stored.current_revision_id,
    )

    summary = await environment.deletion.delete((unsafe_action,), scan=ScanSnapshot())

    assert summary.items[0].outcome is DeletionOutcome.FAILED
    assert summary.items[0].failure_code == "SOURCE_UNAVAILABLE"
    assert environment.stored.document_id in environment.database.documents


def _deny_source_existence(monkeypatch: pytest.MonkeyPatch) -> None:
    import pathlib

    original_exists = pathlib.Path.exists
    original_lstat = pathlib.Path.lstat

    def fake_exists(self: Path) -> bool:
        if self.name == "candidate.txt":
            return False
        return original_exists(self)

    def fake_lstat(self: Path) -> os.stat_result:
        if self.name == "candidate.txt":
            raise PermissionError(13, "Permission denied")
        return original_lstat(self)

    monkeypatch.setattr(pathlib.Path, "exists", fake_exists)
    monkeypatch.setattr(pathlib.Path, "lstat", fake_lstat)


def test_runtime_modes_are_unchanged() -> None:
    assert {mode.value for mode in RuntimeMode} == {
        "STARTING",
        "SERVING",
        "DRAINING",
        "MAINTENANCE",
        "BLOCKED",
        "SHUTTING_DOWN",
    }


@pytest.mark.asyncio
async def test_orphan_projection_is_removed_before_serving(tmp_path: Path) -> None:
    from ar3_support import add_orphan_projection

    environment = await build_environment(tmp_path)
    orphan = add_orphan_projection(
        environment,
        document_id="d" * 64,
        revision_id="rev_" + "c" * 64,
    )

    runtime = await start_runtime(environment)

    assert runtime.gate.mode.value == "SERVING"
    assert orphan not in {
        (row.document_id, row.revision_id) for row in environment.rows
    }
    assert "delete_revision" in environment.index.events
    assert await runtime.coordinator.readiness() is True


@pytest.mark.asyncio
async def test_unremoved_orphan_projection_blocks_serving(tmp_path: Path) -> None:
    from ar3_support import add_orphan_projection

    environment = await build_environment(tmp_path)
    orphan = add_orphan_projection(
        environment,
        document_id="d" * 64,
        revision_id="rev_" + "c" * 64,
    )
    environment.index.blocked_revisions.add(orphan)

    runtime = await start_runtime(environment)

    assert runtime.gate.mode.value == "BLOCKED"
    assert environment.source_safety_calls == 0
    assert environment.verify_calls == 0
    assert orphan in {(row.document_id, row.revision_id) for row in environment.rows}
    with pytest.raises(RuntimeNotReadyError):
        await runtime.coordinator.run_query(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unreadable_published_source_blocks_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = await build_environment(tmp_path)
    _deny_source_existence(monkeypatch)
    assert environment.stored is not None

    safety = await environment.source_safety.check()
    assert safety.safe is False
    assert safety.unreadable_documents == 1
    assert safety.missing_documents == 0
    assert safety.findings[0].availability is SourceAvailability.UNAVAILABLE

    runtime = await start_runtime(environment)
    assert runtime.gate.mode.value == "BLOCKED"
    assert environment.verify_calls == 0
    with pytest.raises(RuntimeNotReadyError):
        await runtime.coordinator.run_query(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unavailable_parent_directory_is_unavailable(tmp_path: Path) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None

    import pathlib

    original = pathlib.Path.lstat

    def failing(self: Path) -> os.stat_result:
        if self.name == "candidate.txt":
            raise NotADirectoryError(20, "Not a directory")
        return original(self)

    pathlib.Path.lstat = failing  # type: ignore[method-assign]
    try:
        availability, _ = environment.reconciliation.classify_source(environment.stored)
    finally:
        pathlib.Path.lstat = original  # type: ignore[method-assign]
    assert availability is SourceAvailability.UNAVAILABLE


@pytest.mark.asyncio
async def test_confirmed_missing_source_is_classified_missing(tmp_path: Path) -> None:
    environment = await build_environment(tmp_path)
    assert environment.stored is not None
    (tmp_path / "guides" / "candidate.txt").unlink()

    availability, digest = environment.reconciliation.classify_source(
        environment.stored
    )
    assert availability is SourceAvailability.MISSING
    assert digest is None
