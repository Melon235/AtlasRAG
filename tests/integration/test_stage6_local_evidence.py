"""Real PostgreSQL/Milvus Stage 6 local evidence integration."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from atlasrag.application.local_evidence import LocalEvidenceRunner
from atlasrag.domain.enums import (
    EvidenceType,
    LocalEvidenceStatus,
    LocalExecutionStatus,
    SourceType,
)
from atlasrag.domain.errors import DependencyUnavailableError
from atlasrag.domain.indexing.models import EmbeddingModelIdentity
from atlasrag.domain.requests import LocalRetrievalRequest, QueryFilters
from atlasrag.providers.reranker.base import RerankerModelIdentity
from atlasrag.repositories.postgres.pool import PostgresPool
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
from integration.stage4_helpers import Factory
from integration.stage5_helpers import application as sync_application
from integration.stage5_helpers import request as sync_request
from integration.stage5_helpers import runtime_config
from integration.test_milvus import IsolatedMilvus
from integration.test_milvus import isolated_milvus as isolated_milvus

pytestmark = pytest.mark.integration

MARKDOWN = """# Retrieval guide

AtlasRAG hybrid retrieval combines dense vectors with BM25 lexical evidence.
The canonical parent context must be returned without child contamination.

| Name | Value |
| --- | --- |
| Alpha | 1 |
| Beta | 2 |
"""


class FakeQueryEmbedding:
    def __init__(self, dimension: int) -> None:
        models = runtime_config().models
        self.identity = EmbeddingModelIdentity(
            model_id=models.embedding_model_id,
            model_revision=models.embedding_model_revision,
            tokenizer_id=models.embedding_tokenizer_id,
            tokenizer_revision=models.embedding_tokenizer_revision,
            dimension=dimension,
            device="cpu",
        )
        self.queries: list[str] = []

    async def embed_query(self, query: str) -> tuple[float, ...]:
        self.queries.append(query)
        return (1.0, 0.0, 0.0)


class FakeReranker:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.fail = False

    @property
    def identity(self) -> RerankerModelIdentity:
        return RerankerModelIdentity(
            model_id="fake",
            model_revision="r1",
            tokenizer_id="fake",
            tokenizer_revision="r1",
            device="cpu",
            dtype="float32",
            max_input_tokens=8192,
        )

    async def initialize(self, *, allow_download: bool = False) -> None:
        return None

    async def score(self, query: str, passages: Sequence[str]) -> tuple[float, ...]:
        self.queries.append(query)
        if self.fail:
            raise DependencyUnavailableError
        terms = {term.casefold() for term in query.split() if term}
        return tuple(
            0.95 if terms & {term.casefold() for term in passage.split()} else 0.05
            for passage in passages
        )


def services(
    factory: Factory,
    index: Any,
    *,
    reranker: FakeReranker | None = None,
) -> tuple[LocalEvidenceServices, FakeQueryEmbedding, FakeReranker]:
    profile = LocalEvidenceProfile(
        dense_top_k=20,
        bm25_top_k=20,
        max_rerank_candidates=20,
        max_evidence=5,
    )
    embedding = FakeQueryEmbedding(index.embedding_dimension)
    fake_reranker = reranker or FakeReranker()
    snapshot = PublishedRevisionSnapshotService(factory)
    return (
        LocalEvidenceServices(
            retrieval=HybridRetrievalService(
                scope_builder=RetrievalScopeBuilder(),
                snapshot=snapshot,
                compiler=RetrievalFilterCompiler(),
                dense=DenseRetriever(
                    embedding=embedding, search=index, profile=profile
                ),
                bm25=BM25Retriever(search=index, profile=profile),
            ),
            reranker=RerankerService(provider=fake_reranker, profile=profile),
            selector=UniqueEvidenceSelector(profile=profile),
            parent_context=ParentContextService(factory),
            sufficiency=DeterministicEvidenceSufficiency(
                profile=DeterministicSufficiencyProfile(
                    sufficient_rerank_score=0.5,
                    ambiguous_rerank_score=0.2,
                )
            ),
            profile=profile,
        ),
        embedding,
        fake_reranker,
    )


async def _synced(
    factory: Factory,
    pool: PostgresPool,
    index: Any,
    root: Path,
) -> None:
    app = sync_application(factory, pool, index, root)
    result = await app.run(sync_request(root))
    assert result.ready_builds == 1, result.model_dump()
    assert result.serving_safety.value == "SAFE_TO_SERVE", result.model_dump()


@pytest.mark.asyncio
async def test_real_hybrid_local_evidence_graph(
    postgres_uow_factory: Factory,
    postgres_pool: PostgresPool,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    (tmp_path / "guide.md").write_text(MARKDOWN, encoding="utf-8")
    await _synced(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    bundle, embedding, reranker = services(
        postgres_uow_factory, isolated_milvus.provider
    )
    runner = LocalEvidenceRunner(bundle)
    result = await runner.run(
        LocalRetrievalRequest(
            original_query="AtlasRAG hybrid retrieval evidence",
            retrieval_query="AtlasRAG hybrid retrieval evidence",
        )
    )
    assert result.execution_status is LocalExecutionStatus.OK
    assert result.evidence_status is LocalEvidenceStatus.SUFFICIENT
    assert result.selected_evidence
    assert any("AtlasRAG" in evidence.content for evidence in result.selected_evidence)
    assert embedding.queries == ["AtlasRAG hybrid retrieval evidence"]
    assert reranker.queries == ["AtlasRAG hybrid retrieval evidence"]
    assert any(
        evidence.evidence_type is EvidenceType.TEXT_PARENT
        for evidence in result.selected_evidence
    )


@pytest.mark.asyncio
async def test_real_table_evidence_and_file_name_scope(
    postgres_uow_factory: Factory,
    postgres_pool: PostgresPool,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    (tmp_path / "guide.md").write_text(MARKDOWN, encoding="utf-8")
    await _synced(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    bundle, _, _ = services(postgres_uow_factory, isolated_milvus.provider)
    result = await LocalEvidenceRunner(bundle).run(
        LocalRetrievalRequest(
            original_query="Alpha Beta Name Value table",
            retrieval_query="Alpha Beta Name Value table",
            filters=QueryFilters(file_name="guide.md"),
        )
    )
    assert result.evidence_status is LocalEvidenceStatus.SUFFICIENT
    table = [
        evidence
        for evidence in result.selected_evidence
        if evidence.evidence_type is EvidenceType.TABLE
    ]
    assert table
    assert any(
        "Alpha" in evidence.content and "Beta" in evidence.content for evidence in table
    )


@pytest.mark.asyncio
async def test_real_missing_file_filter_returns_insufficient_not_unavailable(
    postgres_uow_factory: Factory,
    postgres_pool: PostgresPool,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    (tmp_path / "guide.md").write_text(MARKDOWN, encoding="utf-8")
    await _synced(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    bundle, _, _ = services(postgres_uow_factory, isolated_milvus.provider)
    result = await LocalEvidenceRunner(bundle).run(
        LocalRetrievalRequest(
            original_query="AtlasRAG",
            retrieval_query="AtlasRAG",
            filters=QueryFilters(file_name="missing.md"),
        )
    )
    assert result.execution_status is LocalExecutionStatus.OK
    assert result.evidence_status is LocalEvidenceStatus.INSUFFICIENT
    assert result.selected_evidence == ()
    assert result.best_available_evidence is None


@pytest.mark.asyncio
async def test_real_reranker_fallback_preserves_candidates(
    postgres_uow_factory: Factory,
    postgres_pool: PostgresPool,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    (tmp_path / "guide.md").write_text(MARKDOWN, encoding="utf-8")
    await _synced(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    failing = FakeReranker()
    failing.fail = True
    bundle, _, _ = services(
        postgres_uow_factory, isolated_milvus.provider, reranker=failing
    )
    result = await LocalEvidenceRunner(bundle).run(
        LocalRetrievalRequest(
            original_query="AtlasRAG hybrid retrieval",
            retrieval_query="AtlasRAG hybrid retrieval",
        )
    )
    assert result.execution_status is LocalExecutionStatus.DEGRADED
    assert result.evidence_status in {
        LocalEvidenceStatus.SUFFICIENT,
        LocalEvidenceStatus.INSUFFICIENT,
    }


@pytest.mark.asyncio
async def test_missing_canonical_parent_fails_closed(
    postgres_uow_factory: Factory,
    postgres_pool: PostgresPool,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    from atlasrag.domain.errors import CanonicalDataError

    (tmp_path / "guide.md").write_text(MARKDOWN, encoding="utf-8")
    await _synced(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    async with postgres_uow_factory() as unit:
        document = (await unit.documents.list())[0]
        assert document.current_revision_id is not None
        await unit.chunks.delete_revision(
            document.document_id, document.current_revision_id
        )
        await unit.commit()
    bundle, _, _ = services(postgres_uow_factory, isolated_milvus.provider)
    with pytest.raises(CanonicalDataError):
        await LocalEvidenceRunner(bundle).run(
            LocalRetrievalRequest(
                original_query="AtlasRAG",
                retrieval_query="AtlasRAG",
            )
        )


@pytest.mark.asyncio
async def test_real_source_type_and_sheet_name_filter_compilation(
    postgres_uow_factory: Factory,
    postgres_pool: PostgresPool,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    (tmp_path / "guide.md").write_text(MARKDOWN, encoding="utf-8")
    await _synced(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    source_typed, _, _ = services(postgres_uow_factory, isolated_milvus.provider)
    source_result = await LocalEvidenceRunner(source_typed).run(
        LocalRetrievalRequest(
            original_query="AtlasRAG",
            retrieval_query="AtlasRAG",
            filters=QueryFilters(source_type=SourceType.MD),
        )
    )
    assert source_result.execution_status is LocalExecutionStatus.OK
    assert source_result.evidence_status is LocalEvidenceStatus.SUFFICIENT

    sheet_typed, _, _ = services(postgres_uow_factory, isolated_milvus.provider)
    sheet_result = await LocalEvidenceRunner(sheet_typed).run(
        LocalRetrievalRequest(
            original_query="AtlasRAG",
            retrieval_query="AtlasRAG",
            filters=QueryFilters(sheet_name="NoSuchSheet"),
        )
    )
    assert sheet_result.execution_status is LocalExecutionStatus.OK
    assert sheet_result.evidence_status is LocalEvidenceStatus.INSUFFICIENT


@pytest.mark.asyncio
async def test_real_whitespace_parent_expands_without_truncation(
    postgres_uow_factory: Factory,
    postgres_pool: PostgresPool,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    (tmp_path / "blank.txt").write_text(" " * 9000 + "a", encoding="utf-8")
    await _synced(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    bundle, _, _ = services(postgres_uow_factory, isolated_milvus.provider)
    result = await LocalEvidenceRunner(bundle).run(
        LocalRetrievalRequest(original_query="a", retrieval_query="a")
    )
    assert result.execution_status is LocalExecutionStatus.OK
    assert result.evidence_status is LocalEvidenceStatus.SUFFICIENT
    assert any(
        evidence.content and not evidence.content.strip()
        for evidence in result.selected_evidence
    )


@pytest.mark.asyncio
async def test_corrupt_current_parse_metadata_fails_closed(
    postgres_uow_factory: Factory,
    postgres_pool: PostgresPool,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    from atlasrag.domain.errors import CanonicalDataError

    (tmp_path / "guide.md").write_text(MARKDOWN, encoding="utf-8")
    await _synced(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    async with postgres_uow_factory() as unit:
        document = (await unit.documents.list())[0]
        await unit.documents.put(document.model_copy(update={"parse_metadata": {}}))
        await unit.commit()
    bundle, _, _ = services(postgres_uow_factory, isolated_milvus.provider)
    with pytest.raises(CanonicalDataError):
        await LocalEvidenceRunner(bundle).run(
            LocalRetrievalRequest(original_query="AtlasRAG", retrieval_query="AtlasRAG")
        )
