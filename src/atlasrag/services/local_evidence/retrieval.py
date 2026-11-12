"""Dense and BM25 retrieval over one shared published retrieval scope."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atlasrag.domain.enums import (
    RetrievalBranch,
    RetrievalBranchStatus,
    RetrievalChunkType,
)
from atlasrag.domain.errors import (
    CanonicalDataError,
    DependencyError,
    ResourceExhaustedError,
)
from atlasrag.domain.indexing.failures import (
    EmbeddingInputLimitError,
    EmbeddingOutputError,
)
from atlasrag.domain.requests import LocalRetrievalRequest
from atlasrag.domain.retrieval import (
    CandidateScores,
    RetrievalBranchResult,
    RetrievalCandidate,
)
from atlasrag.providers.embedding.base import validate_embedding_output
from atlasrag.providers.index.models import MilvusSearchHit
from atlasrag.services.local_evidence.ports import (
    CandidateSearchProvider,
    QueryEmbeddingProvider,
)
from atlasrag.services.local_evidence.profile import LocalEvidenceProfile
from atlasrag.services.local_evidence.scope import (
    PublishedRevisionSet,
    RetrievalFilterCompiler,
    RetrievalScopeBuilder,
)


@dataclass(frozen=True)
class HybridRetrieval:
    dense: RetrievalBranchResult
    bm25: RetrievalBranchResult


class PublishedSnapshot(Protocol):
    async def load(self) -> PublishedRevisionSet: ...


def _candidate(
    hit: MilvusSearchHit,
    *,
    dense_score: float | None = None,
    bm25_score: float | None = None,
) -> RetrievalCandidate:
    if hit.chunk_type not in {
        RetrievalChunkType.TEXT_CHILD,
        RetrievalChunkType.TABLE,
    }:
        raise CanonicalDataError
    return RetrievalCandidate(
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        revision_id=hit.revision_id,
        chunk_type=hit.chunk_type,
        parent_id=hit.parent_id,
        retrieval_text=hit.content,
        scores=CandidateScores(
            dense_score=dense_score,
            bm25_score=bm25_score,
        ),
    )


def _map_hits(
    hits: tuple[MilvusSearchHit, ...],
    published: PublishedRevisionSet,
    *,
    branch: RetrievalBranch,
) -> tuple[RetrievalCandidate, ...]:
    seen: set[str] = set()
    candidates: list[RetrievalCandidate] = []
    for hit in hits:
        if not published.contains(hit.document_id, hit.revision_id):
            raise CanonicalDataError
        if hit.chunk_id in seen:
            raise CanonicalDataError
        seen.add(hit.chunk_id)
        candidates.append(
            _candidate(
                hit,
                dense_score=hit.score if branch is RetrievalBranch.DENSE else None,
                bm25_score=hit.score if branch is RetrievalBranch.BM25 else None,
            )
        )
    return tuple(candidates)


class DenseRetriever:
    def __init__(
        self,
        *,
        embedding: QueryEmbeddingProvider,
        search: CandidateSearchProvider,
        profile: LocalEvidenceProfile,
    ) -> None:
        self._embedding = embedding
        self._search = search
        self._profile = profile

    async def retrieve(
        self,
        query: str,
        *,
        filter_expression: str,
        published: PublishedRevisionSet,
    ) -> RetrievalBranchResult:
        try:
            vector = await self._embedding.embed_query(query)
        except (
            DependencyError,
            ResourceExhaustedError,
            EmbeddingInputLimitError,
            EmbeddingOutputError,
            RuntimeError,
            OSError,
        ):
            return RetrievalBranchResult(
                branch=RetrievalBranch.DENSE,
                status=RetrievalBranchStatus.UNAVAILABLE,
                candidates=(),
            )
        identity = self._embedding.identity
        checked = validate_embedding_output(
            (vector,), expected_count=1, dimension=identity.dimension
        )[0]
        try:
            hits = await self._search.search_dense(
                checked,
                filter_expression=filter_expression,
                limit=self._profile.dense_top_k,
            )
        except DependencyError:
            return RetrievalBranchResult(
                branch=RetrievalBranch.DENSE,
                status=RetrievalBranchStatus.UNAVAILABLE,
                candidates=(),
            )
        candidates = _map_hits(hits, published, branch=RetrievalBranch.DENSE)
        return RetrievalBranchResult(
            branch=RetrievalBranch.DENSE,
            status=(
                RetrievalBranchStatus.OK if candidates else RetrievalBranchStatus.EMPTY
            ),
            candidates=candidates,
        )


class BM25Retriever:
    def __init__(
        self,
        *,
        search: CandidateSearchProvider,
        profile: LocalEvidenceProfile,
    ) -> None:
        self._search = search
        self._profile = profile

    async def retrieve(
        self,
        query: str,
        *,
        filter_expression: str,
        published: PublishedRevisionSet,
    ) -> RetrievalBranchResult:
        try:
            hits = await self._search.search_bm25(
                query,
                filter_expression=filter_expression,
                limit=self._profile.bm25_top_k,
            )
        except DependencyError:
            return RetrievalBranchResult(
                branch=RetrievalBranch.BM25,
                status=RetrievalBranchStatus.UNAVAILABLE,
                candidates=(),
            )
        candidates = _map_hits(hits, published, branch=RetrievalBranch.BM25)
        return RetrievalBranchResult(
            branch=RetrievalBranch.BM25,
            status=(
                RetrievalBranchStatus.OK if candidates else RetrievalBranchStatus.EMPTY
            ),
            candidates=candidates,
        )


class HybridRetrievalService:
    """Resolve scope once, then execute both branches against that exact scope."""

    def __init__(
        self,
        *,
        scope_builder: RetrievalScopeBuilder,
        snapshot: PublishedSnapshot,
        compiler: RetrievalFilterCompiler,
        dense: DenseRetriever,
        bm25: BM25Retriever,
    ) -> None:
        self._scope_builder = scope_builder
        self._snapshot = snapshot
        self._compiler = compiler
        self._dense = dense
        self._bm25 = bm25

    async def retrieve(self, request: LocalRetrievalRequest) -> HybridRetrieval:
        scope = self._scope_builder.build(request)
        published = await self._snapshot.load()
        compiled = self._compiler.compile(scope, published)
        if compiled.empty:
            return HybridRetrieval(
                dense=RetrievalBranchResult(
                    branch=RetrievalBranch.DENSE,
                    status=RetrievalBranchStatus.EMPTY,
                    candidates=(),
                ),
                bm25=RetrievalBranchResult(
                    branch=RetrievalBranch.BM25,
                    status=RetrievalBranchStatus.EMPTY,
                    candidates=(),
                ),
            )
        query = request.retrieval_query
        dense = await self._dense.retrieve(
            query,
            filter_expression=compiled.expression,
            published=published,
        )
        bm25 = await self._bm25.retrieve(
            query,
            filter_expression=compiled.expression,
            published=published,
        )
        return HybridRetrieval(dense=dense, bm25=bm25)
