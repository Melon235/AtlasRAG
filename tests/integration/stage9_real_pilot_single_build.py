"""One-PDF worker for the authorized pilot ingestion; avoids MinerU pool reuse."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, cast

from pymilvus import MilvusClient  # type: ignore[import-untyped]
from stage3.test_candidate_services import NOW
from stage4.candidate_fakes import config

from atlasrag.application.document_build import DocumentBuildRunner
from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.indexing.models import EmbeddingModelIdentity
from atlasrag.domain.ingestion.source import SourceFile
from atlasrag.graphs.indexing.contracts import BuildRequest
from atlasrag.providers.embedding.qwen import (
    QWEN_MODEL_ID,
    QWEN_REVISION,
    QwenEmbeddingProvider,
)
from atlasrag.providers.index.milvus import MilvusIndexProvider
from atlasrag.providers.parsing.docling import DoclingParser
from atlasrag.providers.parsing.markdown import MarkdownParser
from atlasrag.providers.parsing.mineru import MinerUParser
from atlasrag.providers.parsing.routing import ParserRouter
from atlasrag.providers.parsing.text import TextParser
from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager
from atlasrag.repositories.postgres.pool import PostgresPool
from atlasrag.repositories.postgres.runtime_metadata import RuntimeMetadataRepository
from atlasrag.repositories.postgres.uow import PostgresUnitOfWork
from atlasrag.services.indexing.build import BuildResources


def real_runtime() -> RuntimeConfig:
    values = config().model_dump(mode="python")
    values["part_i"]["embedding_dimension"] = 1024
    values["models"]["embedding_model_id"] = QWEN_MODEL_ID
    values["models"]["embedding_model_revision"] = QWEN_REVISION
    values["models"]["embedding_tokenizer_id"] = QWEN_MODEL_ID
    values["models"]["embedding_tokenizer_revision"] = QWEN_REVISION
    return RuntimeConfig.model_validate(values)


async def run() -> dict[str, object]:
    root = Path(os.environ["STAGE9_PDF_ROOT"])
    relative = os.environ["STAGE9_PDF_RELATIVE"]
    pool = PostgresPool(
        os.environ["ATLASRAG_POSTGRES_DSN"], min_size=1, max_size=2, timeout=5.0
    )
    await pool.open()

    def factory() -> PostgresUnitOfWork:
        return PostgresUnitOfWork(pool)

    client = MilvusClient(uri=os.environ["ATLASRAG_MILVUS_URI"], timeout=30)
    index = MilvusIndexProvider(
        client,
        collection_name=os.environ["STAGE9_MILVUS_COLLECTION"],
        embedding_dimension=1024,
        timeout_seconds=30,
    )
    await index.initialize()
    runtime = real_runtime()

    class LazyQwen:
        def __init__(self) -> None:
            self._provider = QwenEmbeddingProvider(device="cuda:0")
            self._ready = False

        @property
        def identity(self) -> EmbeddingModelIdentity:
            return EmbeddingModelIdentity(
                model_id=QWEN_MODEL_ID,
                model_revision=QWEN_REVISION,
                tokenizer_id=QWEN_MODEL_ID,
                tokenizer_revision=QWEN_REVISION,
                dimension=1024,
                device="cuda:0",
            )

        async def embed(self, texts: Any) -> Any:
            if not self._ready:
                await self._provider.initialize()
                self._ready = True
            return await self._provider.embed(texts)

        async def embed_query(self, query: str) -> Any:
            if not self._ready:
                await self._provider.initialize()
                self._ready = True
            return await self._provider.embed_query(query)

    qwen = LazyQwen()
    bundle = BuildResources(
        config=runtime,
        uow_factory=factory,
        parsers=ParserRouter(
            DoclingParser()
            if os.environ.get("STAGE9_PARSER") == "docling"
            else MinerUParser(allow_real_layout=True),
            DoclingParser(),
            MarkdownParser(),
            TextParser(),
        ),
        embedding=qwen,
        index=index,
        clock=lambda: NOW,
        index_batch_size=1,
        verification_batch_size=1,
    )
    runner = DocumentBuildRunner(
        resources=bundle,
        pool=pool,
        lock_manager_factory=PostgresAdvisoryLockManager,
        metadata_repository_factory=RuntimeMetadataRepository,
        max_concurrent_builds=1,
        pool_capacity=2,
    )
    source = SourceFile.from_knowledge_directory(root, relative)
    result = await runner.build(
        BuildRequest(
            source=source, expected_pipeline_fingerprint=pipeline_fingerprint(runtime)
        )
    )
    return cast(dict[str, object], json.loads(result.model_dump_json()))


def main() -> int:
    try:
        result = asyncio.run(run())
    except Exception as error:
        import traceback

        traceback.print_exc()
        print(
            json.dumps(
                {
                    "outcome": "FAILED",
                    "error": type(error).__name__,
                    "detail": repr(error),
                }
            ),
            flush=True,
        )
        return 1
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
