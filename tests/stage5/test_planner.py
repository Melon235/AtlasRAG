"""Pure deterministic SyncPlanner rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlasrag.domain.ingestion.source import derive_document_id
from atlasrag.repositories.postgres.records import FailedStage, IngestionStatus
from atlasrag.services.sync.failures import SyncPlanError
from atlasrag.services.sync.models import (
    SyncAction,
    SyncActionKind,
    SyncPlan,
    SyncReason,
)
from atlasrag.services.sync.planner import SyncPlanner
from stage5.helpers import PIPELINE, database, document, entry, scan

PLANNER = SyncPlanner()
OTHER_PIPELINE = "b" * 64


def action_for(plan: SyncPlan, path: str) -> SyncAction:
    actions = [action for action in plan.actions if action.relative_source_path == path]
    assert len(actions) == 1
    return actions[0]


def test_new_source_builds(tmp_path: Path) -> None:
    source = entry(tmp_path, "a.txt", "a")
    plan = PLANNER.plan(scan(source), database(), pipeline_fingerprint=PIPELINE)
    action = action_for(plan, "a.txt")
    assert action.kind is SyncActionKind.BUILD
    assert action.reason is SyncReason.NEW_SOURCE


def test_unchanged_published_source_is_unchanged(tmp_path: Path) -> None:
    source = entry(tmp_path, "a.txt", "a")
    doc = document(source)
    plan = PLANNER.plan(scan(source), database(doc), pipeline_fingerprint=PIPELINE)
    action = action_for(plan, "a.txt")
    assert action.kind is SyncActionKind.UNCHANGED
    assert action.current_revision_id == doc.current_revision_id


def test_content_change_beats_pipeline_change(tmp_path: Path) -> None:
    source = entry(tmp_path, "a.txt", "new")
    doc = document(source, current_hash="f" * 64, current_pipeline=OTHER_PIPELINE)
    plan = PLANNER.plan(scan(source), database(doc), pipeline_fingerprint=PIPELINE)
    assert action_for(plan, "a.txt").reason is SyncReason.CONTENT_CHANGED


def test_pipeline_change_builds(tmp_path: Path) -> None:
    source = entry(tmp_path, "a.txt", "a")
    doc = document(source, current_pipeline=OTHER_PIPELINE)
    plan = PLANNER.plan(scan(source), database(doc), pipeline_fingerprint=PIPELINE)
    assert action_for(plan, "a.txt").reason is SyncReason.PIPELINE_CHANGED


def test_failed_initial_build_is_retried(tmp_path: Path) -> None:
    source = entry(tmp_path, "a.txt", "a")
    doc = document(
        source,
        current=False,
        status=IngestionStatus.FAILED,
        failed_stage=FailedStage.PARSE,
    )
    plan = PLANNER.plan(scan(source), database(doc), pipeline_fingerprint=PIPELINE)
    assert action_for(plan, "a.txt").reason is SyncReason.RETRY_FAILED_BUILD


def test_failed_candidate_with_healthy_current_is_not_retried(tmp_path: Path) -> None:
    source = entry(tmp_path, "a.txt", "a")
    doc = document(
        source, status=IngestionStatus.FAILED, failed_stage=FailedStage.INDEX_VERIFY
    )
    plan = PLANNER.plan(scan(source), database(doc), pipeline_fingerprint=PIPELINE)
    assert action_for(plan, "a.txt").kind is SyncActionKind.UNCHANGED


def test_missing_source_is_deleted(tmp_path: Path) -> None:
    source = entry(tmp_path, "a.txt", "a")
    doc = document(source)
    (tmp_path / "a.txt").unlink()
    plan = PLANNER.plan(scan(), database(doc), pipeline_fingerprint=PIPELINE)
    action = action_for(plan, "a.txt")
    assert action.kind is SyncActionKind.DELETE
    assert action.current_revision_id == doc.current_revision_id


def test_rename_is_delete_plus_build(tmp_path: Path) -> None:
    old = entry(tmp_path, "a.txt", "same")
    new = entry(tmp_path, "archive/a.txt", "same")
    plan = PLANNER.plan(
        scan(new), database(document(old)), pipeline_fingerprint=PIPELINE
    )
    assert [(a.relative_source_path, a.kind) for a in plan.actions] == [
        ("archive/a.txt", SyncActionKind.BUILD),
        ("a.txt", SyncActionKind.DELETE),
    ]


def test_plan_is_deterministic_and_has_no_filesystem_dependency(tmp_path: Path) -> None:
    a = entry(tmp_path, "a.txt", "a")
    b = entry(tmp_path, "b.txt", "b")
    snapshot = scan(b, a)
    (tmp_path / "a.txt").unlink()
    (tmp_path / "b.txt").unlink()
    first = PLANNER.plan(snapshot, database(), pipeline_fingerprint=PIPELINE)
    second = PLANNER.plan(snapshot, database(), pipeline_fingerprint=PIPELINE)
    assert first == second


def test_stale_non_failed_building_candidate_is_not_planned_over(
    tmp_path: Path,
) -> None:
    source = entry(tmp_path, "a.txt", "new")
    doc = document(source, current=False, building_hash="f" * 64)
    with pytest.raises(SyncPlanError) as caught:
        PLANNER.plan(scan(source), database(doc), pipeline_fingerprint=PIPELINE)
    assert caught.value.code == "STALE_BUILDING_CANDIDATE"


def test_plan_document_ids_match_paths(tmp_path: Path) -> None:
    source = entry(tmp_path, "a.txt", "a")
    plan = PLANNER.plan(scan(source), database(), pipeline_fingerprint=PIPELINE)
    assert plan.actions[0].document_id == derive_document_id("a.txt")
