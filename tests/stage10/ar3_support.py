"""A-R3 deterministic interrupted-deletion and startup-safety harness.

PostgreSQL durable state uses the transaction-aware Stage 3 fake unit of work
(committed state, isolated working state, explicit commit/rollback). Milvus
projections are held independently by a recording Stage 4 index fake. All
business services, reconciliation, source safety and global verification are
the real production implementations.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime_harness import (
    CacheHitRetrieval,
    CloseTracker,
    RecordingOwnership,
    build_health,
)
from stage3.fakes import FakeUnitOfWorkFactory
from stage4.candidate_fakes import config
from stage5.test_current_repair import (
    Index,
    published,
)

from atlasrag.application.index_sync import IndexSyncApplication, SyncServices
from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.providers.index.models import MilvusChunkRecord
from atlasrag.providers.index.projection import MilvusRevisionProjection
from atlasrag.repositories.postgres.records import IngestionStatus, StoredDocument
from atlasrag.runtime.coordinator import (
    ApplicationCoordinator,
    CoordinatorDependencies,
)
from atlasrag.runtime.gate import RuntimeGate
from atlasrag.runtime.health import HealthRegistry
from atlasrag.runtime.maintenance import MaintenanceCoordinator
from atlasrag.runtime.tasks import InFlightTaskRegistry
from atlasrag.services.ingestion.mapping import (
    load_document_parse_metadata_envelope,
    replace_document_parse_metadata_slot,
    serialize_document_parse_metadata_envelope,
)
from atlasrag.services.sync.current_repair import CurrentIndexRepairService
from atlasrag.services.sync.deletion import MissingSourceDeletionService
from atlasrag.services.sync.execution import BuildExecutionService
from atlasrag.services.sync.models import (
    BuildExecutionSummary,
    DeletionSummary,
    GlobalSafetyResult,
    ReconciliationResult,
    SyncAction,
    SyncActionKind,
    SyncRequest,
    SyncResult,
)
from atlasrag.services.sync.planner import SyncPlanner
from atlasrag.services.sync.reconciliation import StartupReconciliationService
from atlasrag.services.sync.scanner import KnowledgeDirectoryScanner
from atlasrag.services.sync.snapshot import CanonicalDatabaseSnapshotService
from atlasrag.services.sync.source_safety import (
    SourceSafetyResult,
    StartupSourceSafetyService,
)
from atlasrag.services.sync.verification import GlobalKnowledgeVerificationService

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class RecordingIndex(Index):
    """Stage 4 index fake with independent projections and fault injection."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []
        self.fail_delete_document: BaseException | None = None
        self.projection_error: BaseException | None = None
        self.partial_delete = False
        self.blocked_revisions: set[tuple[str, str]] = set()

    async def delete_revision(self, document_id: str, revision_id: str) -> int:
        self.events.append("delete_revision")
        if (document_id, revision_id) in self.blocked_revisions:
            return 0
        return await super().delete_revision(document_id, revision_id)

    async def wait_revision_visible(self, document_id: str, revision_id: str) -> None:
        self.events.append("wait_revision")
        await super().wait_revision_visible(document_id, revision_id)

    async def insert_revision_batch(
        self,
        records: Sequence[MilvusChunkRecord],
        *,
        document_id: str,
        revision_id: str,
    ) -> None:
        self.events.append("insert_revision")
        await super().insert_revision_batch(
            records, document_id=document_id, revision_id=revision_id
        )

    async def delete_document(self, document_id: str) -> int:
        self.events.append("delete_document")
        if self.fail_delete_document is not None:
            raise self.fail_delete_document
        if self.partial_delete:
            kept = [row for row in self.rows if row.document_id == document_id]
            self.rows = [
                row for row in self.rows if row.document_id != document_id
            ] + kept[:1]
            return max(0, len(kept) - 1)
        return await super().delete_document(document_id)

    async def iter_document_projection(
        self, document_id: str, *, batch_size: int
    ) -> AsyncGenerator[MilvusRevisionProjection, None]:
        self.events.append("document_projection")
        if self.projection_error is not None:
            raise self.projection_error
        async for row in super().iter_document_projection(
            document_id, batch_size=batch_size
        ):
            yield row

    async def iter_all_projection(
        self, *, batch_size: int
    ) -> AsyncGenerator[MilvusRevisionProjection, None]:
        self.events.append("all_projection")
        async for row in super().iter_all_projection(batch_size=batch_size):
            yield row

    async def iter_revision_projection(
        self, document_id: str, revision_id: str, *, batch_size: int
    ) -> AsyncGenerator[MilvusRevisionProjection, None]:
        self.events.append("revision_projection")
        async for row in super().iter_revision_projection(
            document_id, revision_id, batch_size=batch_size
        ):
            yield row


class ControlledLease:
    def __init__(self, controller: ControlledLeases) -> None:
        self._controller = controller

    async def probe(self) -> None:
        self._controller.probe_calls += 1
        if self._controller.block_probe == self._controller.probe_calls:
            self._controller.probe_reached.set()
            await self._controller.probe_release.wait()

    async def release(self) -> None:
        self._controller.released += 1


class ControlledLeases:
    def __init__(self) -> None:
        self.available = True
        self.released = 0
        self.probe_calls = 0
        self.block_probe: int | None = None
        self.probe_reached = asyncio.Event()
        self.probe_release = asyncio.Event()

    async def acquire(self, document_id: str) -> ControlledLease | None:
        if not self.available:
            return None
        return ControlledLease(self)


class GatedVerification:
    """Delegate to real global verification, optionally pausing first."""

    def __init__(self, inner: GlobalKnowledgeVerificationService) -> None:
        self.inner = inner
        self.calls = 0
        self.gate: asyncio.Event | None = None
        self.reached = asyncio.Event()

    async def verify(
        self,
        *,
        deletion_summary: DeletionSummary,
        build_summary: BuildExecutionSummary,
    ) -> GlobalSafetyResult:
        self.calls += 1
        if self.gate is not None:
            self.reached.set()
            await self.gate.wait()
        return await self.inner.verify(
            deletion_summary=deletion_summary, build_summary=build_summary
        )


@dataclass
class AR3Environment:
    root: Path
    runtime: RuntimeConfig
    pipeline: str
    factory: FakeUnitOfWorkFactory
    stored: StoredDocument | None
    document: DocumentContainer | None
    index: RecordingIndex
    repair: CurrentIndexRepairService | None
    reconciliation: StartupReconciliationService
    source_safety: StartupSourceSafetyService
    verification_service: GlobalKnowledgeVerificationService
    verification: GatedVerification
    deletion: MissingSourceDeletionService
    leases: ControlledLeases
    sync_app: IndexSyncApplication
    with_reconcile_index: bool = True
    with_current_repair: bool = True
    order: list[str] = field(default_factory=list)
    sync_calls: int = 0
    reconcile_calls: int = 0
    source_safety_calls: int = 0
    verify_calls: int = 0
    sync_results: list[SyncResult] = field(default_factory=list)

    @property
    def database(self) -> Any:
        return self.factory.database

    async def reconcile(self) -> ReconciliationResult:
        self.reconcile_calls += 1
        self.order.append("reconcile")
        return await self.reconciliation.reconcile()

    async def source_safety_check(self) -> SourceSafetyResult:
        self.source_safety_calls += 1
        self.order.append("source_safety")
        return await self.source_safety.check()

    async def verify(self) -> GlobalSafetyResult:
        self.verify_calls += 1
        self.order.append("verify")
        return await self.verification.verify(
            deletion_summary=DeletionSummary(),
            build_summary=BuildExecutionSummary(),
        )

    async def run_sync(self) -> SyncResult:
        self.sync_calls += 1
        self.order.append("sync")
        result = await self.sync_app.run(
            SyncRequest(
                knowledge_directory=self.root,
                expected_pipeline_fingerprint=self.pipeline,
            )
        )
        self.sync_results.append(result)
        return result

    @property
    def rows(self) -> list[MilvusRevisionProjection]:
        return self.index.rows


class RecordingFreshness:
    def __init__(self, environment: AR3Environment) -> None:
        self.environment = environment
        self.calls = 0
        self.marker_seen: bool | None = None
        self.cache_recovery_calls = 0

    async def recover_if_due(self) -> None:
        self.cache_recovery_calls += 1
        return None

    async def enforce(self) -> None:
        self.calls += 1
        self.environment.order.append("freshness")
        self.marker_seen = (
            self.environment.factory.database.query_cache_invalidation_required
        )
        return None


@dataclass
class AR3Runtime:
    environment: AR3Environment
    root: Path
    gate: RuntimeGate
    tasks: InFlightTaskRegistry
    health: HealthRegistry
    coordinator: ApplicationCoordinator
    ownership: RecordingOwnership
    close: CloseTracker
    freshness: RecordingFreshness
    retrieval: CacheHitRetrieval

    @property
    def order(self) -> list[str]:
        return self.environment.order

    async def start(self) -> None:
        await self.coordinator._start()

    async def query(self, turn: UserTurnRequest) -> object:
        return await self.coordinator.run_query(turn)


async def _unexpected_build(
    source: object, pipeline: str, expected_revision_id: str
) -> object:
    raise AssertionError("builds must not run in interrupted-deletion scenarios")


def _assemble(
    *,
    root: Path,
    runtime: RuntimeConfig,
    pipeline: str,
    factory: FakeUnitOfWorkFactory,
    stored: StoredDocument | None,
    document: DocumentContainer | None,
    index: RecordingIndex,
    repair: CurrentIndexRepairService | None,
    with_reconcile_index: bool,
    with_current_repair: bool,
) -> AR3Environment:
    reconciliation = StartupReconciliationService(
        factory,
        knowledge_directory=root,
        pipeline_fingerprint=pipeline,
        clock=lambda: NOW,
        index=index if with_reconcile_index else None,
        current_repair=repair if with_current_repair else None,
    )
    source_safety = StartupSourceSafetyService(
        factory,
        knowledge_directory=root,
        reconciliation=reconciliation,
    )
    verification_service = GlobalKnowledgeVerificationService(factory, index, runtime)
    verification = GatedVerification(verification_service)
    leases = ControlledLeases()
    deletion = MissingSourceDeletionService(
        factory,
        index=index,
        leases=leases,
        knowledge_directory=root,
        clock=lambda: NOW,
    )
    services = SyncServices(
        reconciliation=reconciliation,
        scanner=KnowledgeDirectoryScanner(),
        database_snapshot=CanonicalDatabaseSnapshotService(factory),
        planner=SyncPlanner(),
        build_execution=BuildExecutionService(
            full_build=_unexpected_build,  # type: ignore[arg-type]
            recovery=object(),  # type: ignore[arg-type]
            max_concurrency=1,
        ),
        deletion=deletion,
        verification=verification,  # type: ignore[arg-type]
        pipeline=pipeline,
    )
    return AR3Environment(
        root=root,
        runtime=runtime,
        pipeline=pipeline,
        factory=factory,
        stored=stored,
        document=document,
        index=index,
        repair=repair,
        reconciliation=reconciliation,
        source_safety=source_safety,
        verification_service=verification_service,
        verification=verification,
        deletion=deletion,
        leases=leases,
        sync_app=IndexSyncApplication(services, runtime=runtime),
        with_reconcile_index=with_reconcile_index,
        with_current_repair=with_current_repair,
    )


async def build_environment(
    root: Path,
    *,
    seed: bool = True,
    index: RecordingIndex | None = None,
    with_reconcile_index: bool = True,
    with_current_repair: bool = True,
    project: bool = True,
) -> AR3Environment:
    runtime = config()
    pipeline = pipeline_fingerprint(runtime)
    if not seed:
        environment = _assemble(
            root=root,
            runtime=runtime,
            pipeline=pipeline,
            factory=FakeUnitOfWorkFactory(),
            stored=None,
            document=None,
            index=index or RecordingIndex(),
            repair=None,
            with_reconcile_index=with_reconcile_index,
            with_current_repair=False,
        )
        return environment
    factory, stored, _, document, _, embedding = published(root)
    recording_index = index or RecordingIndex()
    repair = CurrentIndexRepairService(
        factory,
        runtime=runtime,
        embedding=embedding,
        index=recording_index,
        clock=lambda: NOW,
        batch_size=2,
    )
    environment = _assemble(
        root=root,
        runtime=runtime,
        pipeline=pipeline,
        factory=factory,
        stored=stored,
        document=document,
        index=recording_index,
        repair=repair,
        with_reconcile_index=with_reconcile_index,
        with_current_repair=with_current_repair,
    )
    if project:
        await repair.reconcile_current(stored.document_id)
    return environment


def rebuild_environment(
    environment: AR3Environment,
    *,
    index: RecordingIndex | None = None,
    root: Path | None = None,
) -> AR3Environment:
    """Fresh services/runtime over the same surviving durable state."""

    return _assemble(
        root=environment.root if root is None else root,
        runtime=environment.runtime,
        pipeline=environment.pipeline,
        factory=environment.factory,
        stored=environment.stored,
        document=environment.document,
        index=index or environment.index,
        repair=environment.repair,
        with_reconcile_index=environment.with_reconcile_index,
        with_current_repair=environment.with_current_repair,
    )


def build_runtime(
    environment: AR3Environment,
    *,
    refresh_interval: float = 60.0,
    drain_timeout: float = 1.0,
    shutdown_timeout: float = 1.0,
) -> AR3Runtime:
    gate = RuntimeGate()
    tasks = InFlightTaskRegistry()
    health = build_health(gate, refresh_interval=refresh_interval)
    freshness = RecordingFreshness(environment)
    maintenance = MaintenanceCoordinator(
        gate=gate,
        run_sync=environment.run_sync,
        freshness=freshness,  # type: ignore[arg-type]
        drain_timeout_seconds=drain_timeout,
    )
    retrieval = CacheHitRetrieval(gate)
    ownership = RecordingOwnership(environment.order)
    close = CloseTracker(environment.order)
    deps = CoordinatorDependencies(
        ownership=ownership,  # type: ignore[arg-type]
        gate=gate,
        tasks=tasks,
        health=health,
        freshness=freshness,  # type: ignore[arg-type]
        maintenance=maintenance,
        retrieval=retrieval,
        reconcile=environment.reconcile,
        source_safety=environment.source_safety_check,
        verify=environment.verify,
        close_resources=close,
        shutdown_timeout_seconds=shutdown_timeout,
        cache_recovery_inside_lease=False,
        ownership_acquired=False,
    )
    return AR3Runtime(
        environment=environment,
        root=environment.root,
        gate=gate,
        tasks=tasks,
        health=health,
        coordinator=ApplicationCoordinator(deps),
        ownership=ownership,
        close=close,
        freshness=freshness,
        retrieval=retrieval,
    )


async def start_runtime(
    environment: AR3Environment,
    *,
    refresh_interval: float = 60.0,
    drain_timeout: float = 1.0,
    shutdown_timeout: float = 1.0,
) -> AR3Runtime:
    runtime = build_runtime(
        environment,
        refresh_interval=refresh_interval,
        drain_timeout=drain_timeout,
        shutdown_timeout=shutdown_timeout,
    )
    await runtime.start()
    return runtime


def delete_action(stored: StoredDocument) -> SyncAction:
    return SyncAction(
        kind=SyncActionKind.DELETE,
        document_id=stored.document_id,
        relative_source_path=stored.relative_source_path,
        current_revision_id=stored.current_revision_id,
    )


def empty_document_without_current(environment: AR3Environment) -> None:
    """Turn the seeded published row into an unbuilt RECEIVED row."""

    assert environment.stored is not None
    envelope = load_document_parse_metadata_envelope(environment.stored)
    envelope = replace_document_parse_metadata_slot(
        envelope, slot_name="current", slot=None
    )
    document = environment.stored.model_copy(
        update={
            "current_revision_id": None,
            "current_content_hash": None,
            "current_pipeline_fingerprint": None,
            "ingestion_status": IngestionStatus.RECEIVED,
            "parse_metadata": serialize_document_parse_metadata_envelope(envelope),
            "updated_at": NOW,
        }
    )
    environment.factory.database.documents[document.document_id] = document
    environment.index.rows = []


def add_orphan_projection(
    environment: AR3Environment, *, document_id: str, revision_id: str
) -> tuple[str, str]:
    row = environment.index.rows[0]
    environment.index.rows.append(
        MilvusRevisionProjection.model_validate(
            row.model_dump()
            | {
                "chunk_id": "chunk_" + "f" * 64,
                "document_id": document_id,
                "revision_id": revision_id,
            }
        )
    )
    return document_id, revision_id
