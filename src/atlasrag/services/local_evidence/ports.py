"""Narrow Stage 6 service/provider protocols."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from atlasrag.domain.indexing.models import EmbeddingModelIdentity
from atlasrag.providers.index.models import MilvusSearchHit
from atlasrag.repositories.postgres.records import StoredChunk
from atlasrag.services.sync.ports import SyncUnitOfWorkFactory


class QueryEmbeddingProvider(Protocol):
    @property
    def identity(self) -> EmbeddingModelIdentity: ...

    async def embed_query(self, query: str) -> tuple[float, ...]: ...


class CandidateSearchProvider(Protocol):
    async def search_dense(
        self,
        query_vector: Sequence[float],
        *,
        filter_expression: str,
        limit: int,
    ) -> tuple[MilvusSearchHit, ...]: ...

    async def search_bm25(
        self,
        query_text: str,
        *,
        filter_expression: str,
        limit: int,
    ) -> tuple[MilvusSearchHit, ...]: ...


class ChunkBatchReader(Protocol):
    async def list_by_ids(
        self, chunk_ids: Sequence[str]
    ) -> tuple[StoredChunk, ...]: ...


LocalEvidenceUnitOfWorkFactory = SyncUnitOfWorkFactory
