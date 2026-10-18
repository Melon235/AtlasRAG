"""Durable candidate fixtures and transaction-aware external capability fakes."""

from __future__ import annotations

import weakref
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any

from stage3.fakes import FakeChunkRepository, FakeUnitOfWork, FakeUnitOfWorkFactory
from stage3.test_candidate_services import (
    NOW,
    _context,
    _current_document,
    _document,
    _stored_document,
)

from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.indexing.models import EmbeddingModelIdentity
from atlasrag.domain.ingestion.chunks import RetrievalRole
from atlasrag.domain.ingestion.source import CandidateBuildContext
from atlasrag.providers.index.models import MilvusChunkRecord
from atlasrag.providers.index.projection import MilvusRevisionProjection
from atlasrag.repositories.postgres.records import IngestionStatus, StoredChunk
from atlasrag.services.ingestion.chunking import ChunkingProfile, build_chunks
from atlasrag.services.ingestion.identity import build_candidate_context
from atlasrag.services.ingestion.mapping import (
    map_chunks_to_stored,
    map_elements_to_stored,
)


def config() -> RuntimeConfig:
    return RuntimeConfig.model_validate(
        {
            "infrastructure": {
                "knowledge_directory": "/test",
                "postgres_host": "localhost",
                "postgres_port": 5432,
                "postgres_database": "test",
                "postgres_user": "test",
                "postgres_password": "test",
                "redis_host": "localhost",
                "redis_port": 6379,
                "milvus_host": "localhost",
                "milvus_port": 19530,
                "searxng_url": "http://localhost:8080",
            },
            "models": {
                "embedding_model_id": "test-embedding",
                "embedding_model_revision": "r1",
                "embedding_tokenizer_id": "test-tokenizer",
                "embedding_tokenizer_revision": "r1",
                "reranker_model_id": "test",
                "reranker_model_revision": "r1",
                "query_model_id": "test",
                "query_model_revision": "r1",
                "generation_model_id": "test",
                "generation_model_revision": "r1",
                "deepseek_api_key": "test",
                "model_device": "cuda",
            },
            "part_i": {
                "loader_behavior_version": "loader-v1",
                "parser_behavior_version": "parser-v1",
                "chunker_behavior_version": "chunker-v1",
                "representation_version": "representation-v1",
                "embedding_dimension": 3,
                "bm25_analyzer": "standard",
                "milvus_schema_version": "schema-v1",
                "milvus_index_version": "index-v1",
            },
            "retrieval": {
                "query_construction_version": "v1",
                "retrieval_semantics_version": "v1",
                "sufficiency_strategy": "test",
                "sufficiency_version": "v1",
                "reranker_strategy": "test",
            },
            "answer": {
                "generation_prompt_version": "v1",
                "web_evidence_strategy": "test",
                "compression_strategy": "test",
                "citation_policy_version": "v1",
                "output_policy_version": "v1",
            },
            "cache": {
                "query_cache_enabled": False,
                "parent_cache_enabled": False,
                "query_ttl_seconds": 1,
                "parent_ttl_seconds": 1,
            },
            "maintenance": {
                "max_document_concurrency": 1,
                "drain_timeout_seconds": 1.0,
                "shutdown_timeout_seconds": 1.0,
            },
            "observability": {
                "trace_sample_rate": 0.0,
                "log_level": "INFO",
                "log_directory": "/test",
                "log_queries": False,
            },
        }
    )


class ReadableChunks(FakeChunkRepository):
    async def list_for_revision(
        self, document_id: str, revision_id: str
    ) -> tuple[StoredChunk, ...]:
        self._factory.calls.append("chunks.list_for_revision")
        self._factory.raise_after_if_planned("chunks.list_for_revision")
        return self._working.chunks.get((document_id, revision_id), ())


class IndexUnitOfWork(FakeUnitOfWork):
    async def __aenter__(self) -> IndexUnitOfWork:
        await super().__aenter__()
        assert self._working is not None
        self._chunks = ReadableChunks(self._factory, self._working)
        return self

    @property
    def chunks(self) -> ReadableChunks:
        result = super().chunks
        assert isinstance(result, ReadableChunks)
        return result


class IndexFactory(FakeUnitOfWorkFactory):
    def __call__(self) -> IndexUnitOfWork:
        unit = IndexUnitOfWork(self)
        self.unit_of_works.append(unit)
        return unit

    def assert_no_transaction(self) -> None:
        assert not any(unit._active for unit in self.unit_of_works)


def candidate(root: Path) -> tuple[IndexFactory, CandidateBuildContext, RuntimeConfig]:
    runtime = config()
    context = build_candidate_context(
        _context(root).source, pipeline_fingerprint(runtime)
    )
    document = _document(context, "0123456789" * 10)
    profile = ChunkingProfile(parent_max_chars=100, child_max_chars=10)
    chunks = build_chunks(document, profile=profile)
    assert sum(c.retrieval_role is RetrievalRole.RETRIEVABLE for c in chunks) == 10
    factory = IndexFactory()
    factory.database.documents[context.document_id] = _stored_document(
        context,
        status=IngestionStatus.CHUNKED,
        current=True,
        building_document=document,
    )
    scope = context.document_id, context.revision_id
    factory.database.elements[scope] = map_elements_to_stored(
        document.elements, created_at=NOW
    )
    factory.database.chunks[scope] = tuple(
        reversed(
            map_chunks_to_stored(
                chunks,
                chunker_behavior_version=runtime.part_i.chunker_behavior_version,
                profile_fingerprint=profile.profile_fingerprint,
                created_at=NOW,
            )
        )
    )
    published = _current_document(context)
    current_scope = context.document_id, published.revision_id
    factory.database.elements[current_scope] = map_elements_to_stored(
        published.elements, created_at=NOW
    )
    factory.database.chunks[current_scope] = map_chunks_to_stored(
        build_chunks(published),
        chunker_behavior_version="old-version",
        profile_fingerprint=ChunkingProfile().profile_fingerprint,
        created_at=NOW,
    )
    return factory, context, runtime


class RawVectors(list[list[float]]):
    pass


class Embedding:
    def __init__(self, factory: IndexFactory, runtime: RuntimeConfig) -> None:
        self.factory = factory
        models = runtime.models
        self.identity = EmbeddingModelIdentity(
            model_id=models.embedding_model_id,
            model_revision=models.embedding_model_revision,
            tokenizer_id=models.embedding_tokenizer_id,
            tokenizer_revision=models.embedding_tokenizer_revision,
            dimension=runtime.part_i.embedding_dimension,
            device="cuda:0",
        )
        self.inputs: list[tuple[str, ...]] = []
        self.error: BaseException | None = None
        self.output: Any = None
        self.previous_vectors: weakref.ReferenceType[RawVectors] | None = None
        self.record_refs: list[weakref.ReferenceType[MilvusChunkRecord]] = []

    async def embed(self, texts: Sequence[str]) -> Any:
        self.factory.assert_no_transaction()
        assert self.previous_vectors is None or self.previous_vectors() is None
        assert all(reference() is None for reference in self.record_refs)
        self.inputs.append(tuple(texts))
        if self.error is not None:
            raise self.error
        if self.output is not None:
            return self.output
        raw = RawVectors([[float(len(text)), 1.0, 0.0] for text in texts])
        self.previous_vectors = weakref.ref(raw)
        return raw


class Index:
    def __init__(self, factory: IndexFactory, embedding: Embedding) -> None:
        self.factory = factory
        self.embedding = embedding
        self.embedding_dimension = 3
        self.analyzer = "standard"
        self.rows: list[MilvusRevisionProjection] = []
        self.events: list[str] = []
        self.deleted: list[tuple[str, str]] = []
        self.failure_after: int | None = None
        self.inserted = 0
        self.initialization_error: BaseException | None = None
        self.delete_leaves_rows = False
        self.closed = 0

    async def initialize(self) -> None:
        self.factory.assert_no_transaction()
        self.events.append("initialize")
        if self.initialization_error is not None:
            raise self.initialization_error

    async def delete_revision(self, document_id: str, revision_id: str) -> int:
        self.factory.assert_no_transaction()
        self.events.append("delete")
        self.deleted.append((document_id, revision_id))
        if not self.delete_leaves_rows:
            self.rows = [
                r
                for r in self.rows
                if (r.document_id, r.revision_id) != (document_id, revision_id)
            ]
        return 0  # Deliberately uninformative; the service must inspect visibility.

    async def wait_revision_visible(self, document_id: str, revision_id: str) -> None:
        self.factory.assert_no_transaction()
        self.events.append("visible")

    async def iter_revision_projection(
        self, document_id: str, revision_id: str, *, batch_size: int
    ) -> AsyncGenerator[MilvusRevisionProjection, None]:
        self.factory.assert_no_transaction()
        self.events.append("scan")
        try:
            for row in self.rows:
                if (row.document_id, row.revision_id) == (document_id, revision_id):
                    yield row
        finally:
            self.closed += 1

    async def insert_revision_batch(
        self,
        records: Sequence[MilvusChunkRecord],
        *,
        document_id: str,
        revision_id: str,
    ) -> None:
        self.factory.assert_no_transaction()
        self.events.append("insert")
        for record in records:
            if self.failure_after == self.inserted:
                raise TimeoutError("controlled partial insertion")
            assert (record.document_id, record.revision_id) == (
                document_id,
                revision_id,
            )
            assert record.dense_vector == (float(len(record.content)), 1.0, 0.0)
            self.embedding.record_refs.append(weakref.ref(record))
            payload = record.model_dump(mode="json", exclude={"dense_vector"})
            self.rows.append(MilvusRevisionProjection.model_validate(payload))
            self.inserted += 1
