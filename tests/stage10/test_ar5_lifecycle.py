"""A-R5 UoW resource lifetime and complete lifecycle regressions (AR5-11, AR5-12)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from cache_support import SAFE, MarkerFactory
from parent_cache_support import (
    build_parent_cache_rig,
    build_stored_document,
    build_stored_table,
    build_stored_text_parent,
    ref_for,
)
from runtime_harness import CloseTracker, RecordingOwnership, build_health, request

from atlasrag.domain.enums import EvidenceType
from atlasrag.domain.errors import CanonicalDataError
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalLocalCitation, FinalResponse
from atlasrag.providers.cache.keys import parent_cache_key, query_cache_key
from atlasrag.providers.cache.models import CachedQueryResult
from atlasrag.runtime.cache_freshness import CacheFreshnessBarrier
from atlasrag.runtime.coordinator import (
    ApplicationCoordinator,
    CoordinatorDependencies,
)
from atlasrag.runtime.gate import RuntimeGate
from atlasrag.runtime.maintenance import MaintenanceCoordinator
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.runtime.tasks import InFlightTaskRegistry
from atlasrag.services.sync.models import (
    GlobalSafetyResult,
    ReconciliationOutcome,
    ReconciliationResult,
    ServingSafety,
)
from atlasrag.services.sync.source_safety import SourceSafetyResult


def _response_for(content: str, file_name: str, rel_path: str) -> FinalResponse:
    return FinalResponse(
        answer_text=f"{content} [L1].",
        citations=(
            FinalLocalCitation(
                citation_id="L1",
                file_name=file_name,
                relative_source_path=rel_path,
                evidence_type=EvidenceType.TEXT_PARENT,
            ),
        ),
    )


# AR5-11 — UoW and resource lifetime
@pytest.mark.asyncio
async def test_ar5_11_delayed_redis_get_and_set_hold_zero_postgres_uows() -> None:
    rig = build_parent_cache_rig()
    doc, doc_id, rev_id = build_stored_document()
    parent_hit = build_stored_text_parent(
        document_id=doc_id, revision_id=rev_id, content="Hit parent", order=0
    )
    parent_miss = build_stored_text_parent(
        document_id=doc_id, revision_id=rev_id, content="Miss parent", order=1
    )
    rig.db.documents[doc_id] = doc
    rig.db.chunks[(doc_id, rev_id)] = (parent_hit, parent_miss)
    hit_ref = ref_for(parent_hit, EvidenceType.TEXT_PARENT)
    miss_ref = ref_for(parent_miss, EvidenceType.TEXT_PARENT)

    # Seed parent_hit first.
    await rig.service.expand((hit_ref,))
    rig.uow_active_during_redis.clear()
    rig.factory.calls.clear()
    rig.factory.list_by_ids_calls.clear()
    rig.factory.batch_header_calls.clear()

    # Delay both Redis GET (on hit_ref) and Redis SET (for miss_ref).
    rig.raw.get_modes = ["delay", "ok"]
    rig.raw.set_mode = "delay"

    task = asyncio.create_task(rig.service.expand((miss_ref, hit_ref)))
    await rig.raw.get_started.wait()
    assert rig.factory.active_uows == 0
    assert rig.factory.max_active_uows <= 1
    rig.raw.get_release.set()

    await rig.raw.set_started.wait()
    assert rig.factory.active_uows == 0
    rig.raw.set_release.set()

    result = await task
    assert [item.context_id for item in result] == [
        miss_ref.context_id,
        hit_ref.context_id,
    ]
    assert rig.factory.active_uows == 0
    assert rig.factory.calls.count("documents.get_by_id") == 1
    assert rig.factory.batch_header_calls == [(hit_ref.context_id,)]
    assert rig.factory.list_by_ids_calls == [(miss_ref.context_id,)]
    assert all(not active for _, active in rig.uow_active_during_redis)


@pytest.mark.asyncio
async def test_ar5_11_cancellation_during_delayed_expansion_never_leaks_uow_or_lease() -> (
    None
):
    rig = build_parent_cache_rig()
    doc, doc_id, rev_id = build_stored_document()
    parent = build_stored_text_parent(document_id=doc_id, revision_id=rev_id)
    rig.db.documents[doc_id] = doc
    rig.db.chunks[(doc_id, rev_id)] = (parent,)
    rig.raw.get_mode = "delay"

    gate = RuntimeGate()
    tasks = InFlightTaskRegistry()
    marker = MarkerFactory(required=False)
    freshness = CacheFreshnessBarrier(
        redis=rig.provider,
        uow_factory=cast(Any, marker),
    )

    async def _retrieval(turn: UserTurnRequest) -> FinalResponse:
        expanded = await rig.service.expand(
            (ref_for(parent, EvidenceType.TEXT_PARENT),)
        )
        return _response_for(
            expanded[0].content, doc.file_name, doc.relative_source_path
        )

    log: list[str] = []
    coordinator = await ApplicationCoordinator.start(
        CoordinatorDependencies(
            ownership=RecordingOwnership(log),  # type: ignore[arg-type]
            gate=gate,
            tasks=tasks,
            health=build_health(gate),
            freshness=freshness,
            maintenance=MaintenanceCoordinator(
                gate=gate,
                run_sync=lambda: asyncio.sleep(0, result=SAFE),
                freshness=freshness,
                drain_timeout_seconds=1.0,
            ),
            retrieval=_retrieval,
            reconcile=lambda: asyncio.sleep(
                0, result=ReconciliationResult(outcome=ReconciliationOutcome.CLEAN)
            ),
            source_safety=lambda: asyncio.sleep(
                0, result=SourceSafetyResult(safe=True)
            ),
            verify=lambda: asyncio.sleep(
                0, result=GlobalSafetyResult(serving_safety=ServingSafety.SAFE_TO_SERVE)
            ),
            close_resources=CloseTracker(log),
        )
    )
    query_task = asyncio.create_task(coordinator.run_query(request("s1", "q")))
    await rig.raw.get_started.wait()
    assert gate.active_queries == 1
    assert rig.factory.active_uows == 0

    query_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await query_task
    assert gate.active_queries == 0
    assert rig.factory.active_uows == 0
    assert tasks.snapshot() == ()
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_ar5_11_maintenance_cannot_mutate_while_admitted_parent_expansion_runs() -> (
    None
):
    rig = build_parent_cache_rig()
    doc, doc_id, rev_id = build_stored_document()
    parent = build_stored_text_parent(document_id=doc_id, revision_id=rev_id)
    rig.db.documents[doc_id] = doc
    rig.db.chunks[(doc_id, rev_id)] = (parent,)
    rig.raw.get_mode = "delay"

    gate = RuntimeGate()
    tasks = InFlightTaskRegistry()
    marker = MarkerFactory(required=False)
    freshness = CacheFreshnessBarrier(
        redis=rig.provider,
        uow_factory=cast(Any, marker),
    )
    events: list[str] = []
    maintenance_entered = asyncio.Event()

    async def _retrieval(turn: UserTurnRequest) -> FinalResponse:
        events.append("query_enter")
        expanded = await rig.service.expand(
            (ref_for(parent, EvidenceType.TEXT_PARENT),)
        )
        events.append("query_exit")
        return _response_for(
            expanded[0].content, doc.file_name, doc.relative_source_path
        )

    async def _sync() -> Any:
        events.append("maintenance_sync")
        maintenance_entered.set()
        return SAFE

    log: list[str] = []
    coordinator = await ApplicationCoordinator.start(
        CoordinatorDependencies(
            ownership=RecordingOwnership(log),  # type: ignore[arg-type]
            gate=gate,
            tasks=tasks,
            health=build_health(gate),
            freshness=freshness,
            maintenance=MaintenanceCoordinator(
                gate=gate,
                run_sync=_sync,
                freshness=freshness,
                drain_timeout_seconds=2.0,
            ),
            retrieval=_retrieval,
            reconcile=lambda: asyncio.sleep(
                0, result=ReconciliationResult(outcome=ReconciliationOutcome.CLEAN)
            ),
            source_safety=lambda: asyncio.sleep(
                0, result=SourceSafetyResult(safe=True)
            ),
            verify=lambda: asyncio.sleep(
                0, result=GlobalSafetyResult(serving_safety=ServingSafety.SAFE_TO_SERVE)
            ),
            close_resources=CloseTracker(log),
        )
    )
    query_task = asyncio.create_task(coordinator.run_query(request("s1", "q")))
    await rig.raw.get_started.wait()

    maint_task = asyncio.create_task(coordinator.run_maintenance())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert gate.mode is RuntimeMode.DRAINING
    assert not maintenance_entered.is_set()
    assert "maintenance_sync" not in events

    rig.raw.get_release.set()
    envelope = await query_task
    await maint_task
    assert envelope.response.answer_text.startswith(parent.content)
    assert events == ["query_enter", "query_exit", "maintenance_sync"]
    await coordinator.shutdown()


# AR5-12 — Complete Parent Cache lifecycle
@pytest.mark.asyncio
async def test_ar5_12_revision_upgrade_deletion_and_query_cache_invalidation_lifecycle() -> (
    None
):
    rig = build_parent_cache_rig()
    doc_r1, doc_id, rev_1 = build_stored_document(content_seed="r1")
    parent_r1 = build_stored_text_parent(
        document_id=doc_id, revision_id=rev_1, content="Revision 1 Parent"
    )
    table_r1 = build_stored_table(document_id=doc_id, revision_id=rev_1, order=1)
    rig.db.documents[doc_id] = doc_r1
    rig.db.chunks[(doc_id, rev_1)] = (parent_r1, table_r1)

    doc_r2, _, rev_2 = build_stored_document(content_seed="r2")
    parent_r2 = build_stored_text_parent(
        document_id=doc_id, revision_id=rev_2, content="Revision 2 Parent"
    )

    marker = MarkerFactory(required=False)
    freshness = CacheFreshnessBarrier(
        redis=rig.provider,
        uow_factory=cast(Any, marker),
    )
    gate = RuntimeGate()
    tasks = InFlightTaskRegistry()

    query_key = query_cache_key("s1", "runtime", "req1")
    rig.raw.seed(
        {
            query_key: CachedQueryResult(
                created_at=datetime(2026, 11, 26, tzinfo=UTC),
                final_response=_response_for(
                    "stale query", "guide.md", "docs/guide.md"
                ),
            ).model_dump_json()
        }
    )

    async def _sync_to_r2() -> Any:
        rig.db.documents[doc_id] = doc_r2
        rig.db.chunks[(doc_id, rev_2)] = (parent_r2,)
        marker.required = True
        return SAFE

    log: list[str] = []
    coordinator = await ApplicationCoordinator.start(
        CoordinatorDependencies(
            ownership=RecordingOwnership(log),  # type: ignore[arg-type]
            gate=gate,
            tasks=tasks,
            health=build_health(gate),
            freshness=freshness,
            maintenance=MaintenanceCoordinator(
                gate=gate,
                run_sync=_sync_to_r2,
                freshness=freshness,
                drain_timeout_seconds=1.0,
            ),
            retrieval=lambda turn: asyncio.sleep(
                0,
                result=_response_for("ok", "guide.md", "docs/guide.md"),
            ),
            reconcile=lambda: asyncio.sleep(
                0, result=ReconciliationResult(outcome=ReconciliationOutcome.CLEAN)
            ),
            source_safety=lambda: asyncio.sleep(
                0, result=SourceSafetyResult(safe=True)
            ),
            verify=lambda: asyncio.sleep(
                0, result=GlobalSafetyResult(serving_safety=ServingSafety.SAFE_TO_SERVE)
            ),
            close_resources=CloseTracker(log),
        )
    )

    ref_r1 = ref_for(parent_r1, EvidenceType.TEXT_PARENT)
    ref_r1_table = ref_for(table_r1, EvidenceType.TABLE)
    key_r1 = parent_cache_key(rev_1, parent_r1.chunk_id)
    key_r2 = parent_cache_key(rev_2, parent_r2.chunk_id)

    # 1. R1 miss + fill, then R1 hit (equivalent to disabled path).
    r1_miss = await rig.service.expand((ref_r1, ref_r1_table))
    assert r1_miss == await rig.disabled_service.expand((ref_r1, ref_r1_table))
    assert key_r1 in rig.raw.values

    rig.factory.list_by_ids_calls.clear()
    r1_hit = await rig.service.expand((ref_r1,))
    assert r1_hit[0].content == "Revision 1 Parent"
    assert rig.factory.list_by_ids_calls == []

    # 2. Maintenance publishes R2 and invalidates Query Cache only; R1 Parent entry survives
    #    in Redis yet R1 requests are rejected as noncurrent.
    await coordinator.run_maintenance()
    assert query_key not in rig.raw.values
    assert key_r1 in rig.raw.values
    with pytest.raises(CanonicalDataError):
        await rig.service.expand((ref_r1,))

    # 3. R2 miss + fill, then R2 hit (equivalent to disabled path).
    ref_r2 = ref_for(parent_r2, EvidenceType.TEXT_PARENT)
    r2_miss = await rig.service.expand((ref_r2,))
    assert r2_miss == await rig.disabled_service.expand((ref_r2,))
    assert key_r2 in rig.raw.values

    rig.factory.list_by_ids_calls.clear()
    r2_hit = await rig.service.expand((ref_r2,))
    assert r2_hit[0].content == "Revision 2 Parent"
    assert rig.factory.list_by_ids_calls == []

    # 4. Deleted canonical document cannot be resurrected from Redis even while R2 exists.
    del rig.db.documents[doc_id]
    del rig.db.chunks[(doc_id, rev_2)]
    assert key_r2 in rig.raw.values
    with pytest.raises(CanonicalDataError):
        await rig.service.expand((ref_r2,))

    await coordinator.shutdown()
