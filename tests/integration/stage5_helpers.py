"""Test-only composition of Stage 5 services over real PostgreSQL/Milvus."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stage3.test_candidate_services import NOW
from stage4.candidate_fakes import config

from atlasrag.application.document_build import DocumentBuildRunner
from atlasrag.application.index_sync import (
    DocumentRecoveryRunner,
    IndexSyncApplication,
    SyncServices,
)
from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.graphs.indexing.contracts import BuildRequest
from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager
from atlasrag.repositories.postgres.runtime_metadata import RuntimeMetadataRepository
from atlasrag.services.ingestion.chunking import DEFAULT_CHUNKER_BEHAVIOR_VERSION
from atlasrag.services.sync.current_repair import CurrentIndexRepairService
from atlasrag.services.sync.deletion import MissingSourceDeletionService
from atlasrag.services.sync.execution import BuildExecutionService
from atlasrag.services.sync.models import SyncRequest
from atlasrag.services.sync.planner import SyncPlanner
from atlasrag.services.sync.reconciliation import StartupReconciliationService
from atlasrag.services.sync.recovery import DocumentRecoveryService
from atlasrag.services.sync.scanner import KnowledgeDirectoryScanner
from atlasrag.services.sync.snapshot import CanonicalDatabaseSnapshotService
from atlasrag.services.sync.verification import GlobalKnowledgeVerificationService
from integration.stage4_helpers import Factory, resources, runner


class _DeletionLeases:
    def __init__(self, pool: Any) -> None:
        self._manager = PostgresAdvisoryLockManager(pool)

    async def acquire(self, document_id: str) -> object | None:
        return await self._manager.try_acquire(document_id)


def runtime_config() -> Any:
    values = config().model_dump(mode="python")
    values["part_i"]["chunker_behavior_version"] = DEFAULT_CHUNKER_BEHAVIOR_VERSION
    return type(config()).model_validate(values)


def application(
    factory: Factory, pool: Any, index: Any, root: Path
) -> IndexSyncApplication:
    runtime = runtime_config()
    pipeline = pipeline_fingerprint(runtime)
    bundle = resources(factory, index)
    full_runner: DocumentBuildRunner[Any] = runner(bundle, pool)
    recovery_service = DocumentRecoveryService(
        factory,
        runtime=runtime,
        embedding=bundle.embedding,
        index=index,
        clock=lambda: NOW,
        batch_size=1,
        verification_batch_size=2,
    )
    recovery_runner = DocumentRecoveryRunner(
        recovery=recovery_service,
        pool=pool,
        lock_manager_factory=PostgresAdvisoryLockManager,
        metadata_repository_factory=RuntimeMetadataRepository,
    )

    async def full_build(
        source: Any, fingerprint: str, expected_revision_id: str
    ) -> Any:
        return await full_runner.build(
            BuildRequest(
                source=source,
                expected_pipeline_fingerprint=fingerprint,
                expected_revision_id=expected_revision_id,
            )
        )

    builds = BuildExecutionService(
        full_build=full_build,
        recovery=recovery_runner,  # type: ignore[arg-type]
        max_concurrency=2,
    )
    repair = CurrentIndexRepairService(
        factory,
        runtime=runtime,
        embedding=bundle.embedding,
        index=index,
        clock=lambda: NOW,
        batch_size=1,
    )
    reconciliation = StartupReconciliationService(
        factory,
        knowledge_directory=root,
        pipeline_fingerprint=pipeline,
        clock=lambda: NOW,
        index=index,
        current_repair=repair,
    )
    services = SyncServices(
        reconciliation=reconciliation,
        scanner=KnowledgeDirectoryScanner(),
        database_snapshot=CanonicalDatabaseSnapshotService(factory),
        planner=SyncPlanner(),
        build_execution=builds,
        deletion=MissingSourceDeletionService(
            factory,
            index=index,
            leases=_DeletionLeases(pool),  # type: ignore[arg-type]
            knowledge_directory=root,
            clock=lambda: NOW,
        ),
        verification=GlobalKnowledgeVerificationService(factory, index, runtime),
        pipeline=pipeline,
    )
    return IndexSyncApplication(
        services, runtime=runtime, initialize_index=index.initialize
    )


def request(root: Path, *, pipeline: str | None = None) -> SyncRequest:
    runtime = runtime_config()
    return SyncRequest(
        knowledge_directory=root,
        expected_pipeline_fingerprint=pipeline or pipeline_fingerprint(runtime),
    )
