"""Real component runtime for the offline pilot-ingest acceptance test."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

from pymilvus import MilvusClient  # type: ignore[import-untyped]

from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.providers.index.milvus import MilvusIndexProvider
from atlasrag.repositories.postgres.pool import PostgresPool
from atlasrag.repositories.postgres.uow import PostgresUnitOfWork
from atlasrag.services.sync.models import BuildExecutionSummary, DeletionSummary
from atlasrag.services.sync.verification import GlobalKnowledgeVerificationService
from integration.stage4_helpers import resources, runner
from integration.stage5_helpers import runtime_config


async def build_ingestion_runtime() -> SimpleNamespace:
    pool = PostgresPool(
        os.environ["ATLASRAG_POSTGRES_DSN"], min_size=1, max_size=4, timeout=2.0
    )
    await pool.open()

    def factory() -> PostgresUnitOfWork:
        return PostgresUnitOfWork(pool)

    client = MilvusClient(uri=os.environ["ATLASRAG_MILVUS_URI"], timeout=10)
    index = MilvusIndexProvider(
        client,
        collection_name=os.environ["STAGE9_MILVUS_COLLECTION"],
        embedding_dimension=3,
        timeout_seconds=10,
    )
    await index.initialize()
    runtime = runtime_config()
    bundle = resources(factory, index)
    full_runner = runner(bundle, pool)

    class Backend:
        async def build(self, request: Any) -> Any:
            return await full_runner.build(request)

    verifier = GlobalKnowledgeVerificationService(factory, index, runtime)

    async def verify() -> Any:
        return await verifier.verify(
            deletion_summary=DeletionSummary(),
            build_summary=BuildExecutionSummary(),
        )

    return SimpleNamespace(
        backend=Backend(),
        uow_factory=factory,
        runtime_config=runtime,
        verifier=verify,
        pipeline_fingerprint=pipeline_fingerprint(runtime),
    )
