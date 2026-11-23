"""Real pilot ingestion runtime: one isolated build worker per PDF."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from stage4.candidate_fakes import config

from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.config.models import RuntimeConfig
from atlasrag.graphs.indexing.contracts import BuildResult
from atlasrag.repositories.postgres.pool import PostgresPool
from atlasrag.repositories.postgres.uow import PostgresUnitOfWork
from atlasrag.services.sync.models import BuildExecutionSummary, DeletionSummary


def real_runtime() -> RuntimeConfig:
    values = config().model_dump(mode="python")
    values["part_i"]["embedding_dimension"] = 1024
    values["models"]["embedding_model_id"] = "Qwen/Qwen3-Embedding-0.6B"
    values["models"]["embedding_model_revision"] = (
        "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
    )
    values["models"]["embedding_tokenizer_id"] = "Qwen/Qwen3-Embedding-0.6B"
    values["models"]["embedding_tokenizer_revision"] = (
        "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
    )
    return RuntimeConfig.model_validate(values)


async def build_ingestion_runtime() -> SimpleNamespace:
    pool = PostgresPool(
        os.environ["ATLASRAG_POSTGRES_DSN"], min_size=1, max_size=4, timeout=5.0
    )
    await pool.open()

    def factory() -> PostgresUnitOfWork:
        return PostgresUnitOfWork(pool)

    runtime = real_runtime()
    from pymilvus import MilvusClient  # type: ignore[import-untyped]

    from atlasrag.providers.index.milvus import MilvusIndexProvider
    from atlasrag.services.sync.verification import GlobalKnowledgeVerificationService

    client = MilvusClient(uri=os.environ["ATLASRAG_MILVUS_URI"], timeout=30)
    index = MilvusIndexProvider(
        client,
        collection_name=os.environ["STAGE9_MILVUS_COLLECTION"],
        embedding_dimension=1024,
        timeout_seconds=30,
    )
    await index.initialize()

    class Backend:
        async def build(self, request: Any) -> BuildResult:
            source = request.source
            root = source.knowledge_directory
            env = dict(os.environ)
            env["STAGE9_PDF_ROOT"] = str(root)
            env["STAGE9_PDF_RELATIVE"] = source.relative_source_path
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("stage9_real_pilot_single_build.py")),
                ],
                cwd=Path(__file__).resolve().parents[2],
                env=env,
                capture_output=True,
                text=True,
                timeout=600,
            )
            for line in reversed(completed.stdout.splitlines()):
                if line.startswith("{"):
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("outcome") == "READY":
                        return BuildResult.model_validate(payload)
                    raise RuntimeError("single PDF build failed: " + str(payload))
            raise RuntimeError(
                "single PDF build produced no result: " + completed.stderr[-500:]
            )

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
        embedding_identity={"model_id": "Qwen/Qwen3-Embedding-0.6B", "dimension": 1024},
    )
