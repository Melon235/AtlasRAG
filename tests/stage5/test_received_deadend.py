"""Regression: RECEIVED without a building candidate is a valid empty source."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from stage3.fakes import FakeUnitOfWorkFactory
from stage3.test_candidate_services import _context, _stored_document

from atlasrag.repositories.postgres.records import IngestionStatus
from atlasrag.services.sync.failures import SyncPlanError
from atlasrag.services.sync.models import ReconciliationOutcome, SyncReason
from atlasrag.services.sync.planner import SyncPlanner
from atlasrag.services.sync.reconciliation import StartupReconciliationService
from atlasrag.services.sync.snapshot import CanonicalDatabaseSnapshotService

NOW = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_received_without_building_is_planned_and_reconciled(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    factory = FakeUnitOfWorkFactory()
    stored = _stored_document(context, status=IngestionStatus.RECEIVED, building=False)
    factory.database.documents[stored.document_id] = stored
    snapshot = await CanonicalDatabaseSnapshotService(factory).load()
    assert snapshot.documents[0].building_revision_id is None
    source_entry = __import__("stage5.helpers", fromlist=["entry"]).entry(
        tmp_path, context.source.relative_source_path, "candidate source bytes"
    )
    from stage5.helpers import scan

    plan = SyncPlanner().plan(
        scan(source_entry),
        snapshot,
        pipeline_fingerprint=context.pipeline_fingerprint,
    )
    assert plan.actions[0].reason is SyncReason.NEW_SOURCE
    reconciliation = await StartupReconciliationService(
        factory,
        knowledge_directory=tmp_path,
        pipeline_fingerprint=context.pipeline_fingerprint,
        clock=lambda: NOW,
    ).reconcile()
    assert reconciliation.outcome is ReconciliationOutcome.CLEAN


@pytest.mark.asyncio
async def test_invalid_snapshot_is_classified_not_a_raw_validation_error(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    factory = FakeUnitOfWorkFactory()
    stored = _stored_document(context, status=IngestionStatus.PARSED)
    factory.database.documents[stored.document_id] = stored.model_copy(
        update={"parse_metadata": {"schema_version": 99}}
    )
    with pytest.raises(SyncPlanError) as caught:
        await CanonicalDatabaseSnapshotService(factory).load()
    assert caught.value.code == "DOCUMENT_SNAPSHOT_INVALID"
