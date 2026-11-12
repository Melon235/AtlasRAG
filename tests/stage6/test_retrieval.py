"""Shared-scope dense/BM25 branch tests with fake provider boundaries."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from atlasrag.domain.enums import RetrievalBranchStatus, RetrievalChunkType
from atlasrag.domain.errors import CanonicalDataError, DependencyUnavailableError
from atlasrag.domain.indexing.failures import EmbeddingOutputError
from atlasrag.domain.indexing.models import EmbeddingModelIdentity
from atlasrag.domain.ingestion.source import derive_document_id
from atlasrag.domain.requests import LocalRetrievalRequest
from atlasrag.providers.index.models import MilvusSearchHit
from atlasrag.services.local_evidence.profile import LocalEvidenceProfile
from atlasrag.services.local_evidence.retrieval import (
    BM25Retriever,
    DenseRetriever,
    HybridRetrievalService,
)
from atlasrag.services.local_evidence.scope import (
    PublishedRevision,
    PublishedRevisionSet,
    RetrievalFilterCompiler,
    RetrievalScopeBuilder,
)

DOC = derive_document_id("guide.md")
REV = "rev_" + "a" * 64
PARENT = "chunk_" + "b" * 64


def published() -> PublishedRevisionSet:
    return PublishedRevisionSet(
        revisions=(
            PublishedRevision(
                document_id=DOC,
                revision_id=REV,
                file_name="guide.md",
                source_type="MD",
            ),
        )
    )


def hit(
    cid: str = "chunk_" + "c" * 64, content: str = "text", score: float = 0.9
) -> MilvusSearchHit:
    return MilvusSearchHit(
        chunk_id=cid,
        document_id=DOC,
        revision_id=REV,
        chunk_type=RetrievalChunkType.TEXT_CHILD,
        parent_id=PARENT,
        content=content,
        score=score,
    )


class FakeEmbedding:
    identity = EmbeddingModelIdentity(
        model_id="fake",
        model_revision="r1",
        tokenizer_id="fake",
        tokenizer_revision="r1",
        dimension=3,
        device="cpu",
    )

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.error: BaseException | None = None

    async def embed_query(self, query: str) -> tuple[float, ...]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return (1.0, 2.0, 3.0)


class FakeSearch:
    def __init__(self) -> None:
        self.dense_hits = (hit(),)
        self.bm25_hits = (hit(),)
        self.filters: list[str] = []
        self.queries: list[str] = []
        self.fail_dense = False
        self.fail_bm25 = False

    async def search_dense(
        self, vector: Sequence[float], *, filter_expression: str, limit: int
    ) -> tuple[MilvusSearchHit, ...]:
        self.filters.append(filter_expression)
        self.queries.append("DENSE")
        if self.fail_dense:
            raise DependencyUnavailableError
        return self.dense_hits

    async def search_bm25(
        self, query: str, *, filter_expression: str, limit: int
    ) -> tuple[MilvusSearchHit, ...]:
        self.filters.append(filter_expression)
        self.queries.append(query)
        if self.fail_bm25:
            raise DependencyUnavailableError
        return self.bm25_hits


class FakeSnapshot:
    def __init__(self, value: PublishedRevisionSet) -> None:
        self.value = value

    async def load(self) -> PublishedRevisionSet:
        return self.value


def service(
    embedding: FakeEmbedding, search: FakeSearch, snapshot: PublishedRevisionSet
) -> HybridRetrievalService:
    profile = LocalEvidenceProfile(dense_top_k=2, bm25_top_k=2)
    return HybridRetrievalService(
        scope_builder=RetrievalScopeBuilder(),
        snapshot=FakeSnapshot(snapshot),
        compiler=RetrievalFilterCompiler(),
        dense=DenseRetriever(embedding=embedding, search=search, profile=profile),
        bm25=BM25Retriever(search=search, profile=profile),
    )


@pytest.mark.asyncio
async def test_dense_and_bm25_share_one_scope_and_use_retrieval_query() -> None:
    embedding, search = FakeEmbedding(), FakeSearch()
    result = await service(embedding, search, published()).retrieve(
        LocalRetrievalRequest(original_query="original", retrieval_query="rewritten")
    )
    assert embedding.queries == ["rewritten"]
    assert len(search.filters) == 2
    assert search.filters[0] == search.filters[1]
    assert search.queries == ["DENSE", "rewritten"]
    assert result.dense.status is RetrievalBranchStatus.OK
    assert result.bm25.status is RetrievalBranchStatus.OK
    assert result.dense.candidates[0].retrieval_text == "text"


@pytest.mark.asyncio
async def test_whitespace_child_is_preserved_exactly() -> None:
    search = FakeSearch()
    search.dense_hits = (hit(content=" \n\t "),)
    search.bm25_hits = (hit(content=" \n\t "),)
    result = await service(FakeEmbedding(), search, published()).retrieve(
        LocalRetrievalRequest(original_query="q", retrieval_query="r")
    )
    assert result.dense.candidates[0].retrieval_text == " \n\t "


@pytest.mark.asyncio
async def test_one_branch_dependency_failure_is_unavailable_not_global_failure() -> (
    None
):
    search = FakeSearch()
    search.fail_dense = True
    result = await service(FakeEmbedding(), search, published()).retrieve(
        LocalRetrievalRequest(original_query="q", retrieval_query="r")
    )
    assert result.dense.status is RetrievalBranchStatus.UNAVAILABLE
    assert result.bm25.status is RetrievalBranchStatus.OK


@pytest.mark.asyncio
async def test_noncurrent_hit_fails_closed() -> None:
    search = FakeSearch()
    stale = hit().model_copy(update={"revision_id": "rev_" + "f" * 64})
    search.dense_hits = (stale,)
    with pytest.raises(CanonicalDataError):
        await service(FakeEmbedding(), search, published()).retrieve(
            LocalRetrievalRequest(original_query="q", retrieval_query="r")
        )


@pytest.mark.asyncio
async def test_empty_published_manifest_returns_empty_branches_without_search() -> None:
    search = FakeSearch()
    result = await service(FakeEmbedding(), search, PublishedRevisionSet()).retrieve(
        LocalRetrievalRequest(original_query="q", retrieval_query="r")
    )
    assert result.dense.status is RetrievalBranchStatus.EMPTY
    assert result.bm25.status is RetrievalBranchStatus.EMPTY
    assert search.filters == []


@pytest.mark.asyncio
async def test_real_query_embedding_failure_degrades_to_bm25_only() -> None:
    embedding = FakeEmbedding()
    embedding.error = EmbeddingOutputError("invalid hidden state")
    result = await service(embedding, FakeSearch(), published()).retrieve(
        LocalRetrievalRequest(original_query="q", retrieval_query="r")
    )
    assert result.dense.status is RetrievalBranchStatus.UNAVAILABLE
    assert result.bm25.status is RetrievalBranchStatus.OK
