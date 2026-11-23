"""Real retrieval-only diagnostic runtime; no DeepSeek, Web, or cache calls."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pymilvus import MilvusClient  # type: ignore[import-untyped]

from atlasrag.application.local_evidence import LocalEvidenceRunner
from atlasrag.domain.ingestion.source import derive_document_id
from atlasrag.providers.embedding.qwen import (
    QwenEmbeddingProvider,
)
from atlasrag.providers.index.milvus import MilvusIndexProvider
from atlasrag.providers.reranker.bge import (
    BgeRerankerProvider,
)
from atlasrag.repositories.postgres.pool import PostgresPool
from atlasrag.repositories.postgres.uow import PostgresUnitOfWork
from atlasrag.services.local_evidence.parent_context import ParentContextService
from atlasrag.services.local_evidence.profile import (
    DeterministicSufficiencyProfile,
    LocalEvidenceProfile,
)
from atlasrag.services.local_evidence.reranking import RerankerService
from atlasrag.services.local_evidence.retrieval import (
    BM25Retriever,
    DenseRetriever,
    HybridRetrievalService,
)
from atlasrag.services.local_evidence.scope import (
    PublishedRevisionSnapshotService,
    RetrievalFilterCompiler,
    RetrievalScopeBuilder,
)
from atlasrag.services.local_evidence.selection import UniqueEvidenceSelector
from atlasrag.services.local_evidence.service import LocalEvidenceServices
from atlasrag.services.local_evidence.sufficiency import (
    DeterministicEvidenceSufficiency,
)
from benchmarks.contracts.models import BenchmarkQuery, Modality
from benchmarks.wiring.quality import CollectingObserver


async def build_runtime() -> Any:
    root = Path(os.environ["STAGE9_SNAPSHOT"])
    selection = json.loads(Path(os.environ["STAGE9_SELECTION"]).read_text())
    queries_raw = json.loads((root / "queries.json").read_text())
    selected = [item for item in selection["selected"] if item["role"] == "TARGET"]
    queries = tuple(
        BenchmarkQuery(
            query_id=item["query_id"],
            text=queries_raw[item["query_id"]]["query"],
            modality=Modality(item["modality"]),
            query_type=str(queries_raw[item["query_id"]].get("type", "unknown")),
            reference_answer=None,
        )
        for item in selected
    )
    gold = {
        item["query_id"]: {derive_document_id(f"{item['official_document_id']}.pdf"): 1}
        for item in selected
    }
    pool = PostgresPool(
        os.environ["ATLASRAG_POSTGRES_DSN"], min_size=1, max_size=4, timeout=5.0
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
    qwen = QwenEmbeddingProvider(device="cuda:0")
    await qwen.initialize()
    bge = BgeRerankerProvider(device="cuda:0")
    await bge.initialize()
    profile = LocalEvidenceProfile(dense_top_k=20, bm25_top_k=20, max_evidence=5)
    local = LocalEvidenceServices(
        retrieval=HybridRetrievalService(
            scope_builder=RetrievalScopeBuilder(),
            snapshot=PublishedRevisionSnapshotService(factory),
            compiler=RetrievalFilterCompiler(),
            dense=DenseRetriever(embedding=qwen, search=index, profile=profile),
            bm25=BM25Retriever(search=index, profile=profile),
        ),
        reranker=RerankerService(provider=bge, profile=profile),
        selector=UniqueEvidenceSelector(profile=profile),
        parent_context=ParentContextService(factory),
        sufficiency=DeterministicEvidenceSufficiency(
            profile=DeterministicSufficiencyProfile(
                sufficient_rerank_score=0.5, ambiguous_rerank_score=0.2
            )
        ),
        profile=profile,
    )
    observer = CollectingObserver()
    local = replace(local, observer=observer)
    runtime = SimpleNamespace(
        local_runner=LocalEvidenceRunner(local),
        observer=observer,
        queries=queries,
        gold=gold,
        pipeline_fingerprint="",
        runtime_fingerprint="",
        web=None,
        cache_gets=lambda: 0,
        cache_sets=lambda: 0,
    )
    return runtime
