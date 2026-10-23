"""Test-only composition and snapshots for combined real PostgreSQL/Milvus gates."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from stage3.test_candidate_services import NOW
from stage4.candidate_fakes import config

from atlasrag.application.document_build import DocumentBuildRunner
from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.indexing.models import (
    EmbeddingModelIdentity,
    RepresentationProfileV1,
)
from atlasrag.domain.ingestion.chunks import ChunkType
from atlasrag.domain.ingestion.source import SourceFile
from atlasrag.graphs.indexing.contracts import BuildRequest
from atlasrag.providers.parsing.docling import DoclingParser
from atlasrag.providers.parsing.markdown import MarkdownParser
from atlasrag.providers.parsing.mineru import MinerUParser
from atlasrag.providers.parsing.routing import ParserRouter
from atlasrag.providers.parsing.text import TextParser
from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager
from atlasrag.repositories.postgres.records import (
    StoredChunk,
    StoredDocument,
    StoredElement,
)
from atlasrag.repositories.postgres.runtime_metadata import RuntimeMetadataRepository
from atlasrag.repositories.postgres.uow import PostgresUnitOfWork
from atlasrag.services.indexing.build import BuildResources
from atlasrag.services.indexing.ports import CandidateIndex
from atlasrag.services.indexing.records import build_expected_projection
from atlasrag.services.indexing.representation import RepresentationBuilder
from atlasrag.services.ingestion.chunking import DEFAULT_CHUNKER_BEHAVIOR_VERSION
from atlasrag.services.ingestion.mapping import (
    map_chunks_from_stored,
    map_document_from_stored,
)

Factory = Callable[[], PostgresUnitOfWork]


class FakeEmbedding:
    """No model assets, downloads, storage fakes, or mutation of canonical text."""

    def __init__(self, runtime: RuntimeConfig) -> None:
        models = runtime.models
        self.identity = EmbeddingModelIdentity(
            model_id=models.embedding_model_id,
            model_revision=models.embedding_model_revision,
            tokenizer_id=models.embedding_tokenizer_id,
            tokenizer_revision=models.embedding_tokenizer_revision,
            dimension=3,
            device="cuda:0",
        )
        self.inputs: list[tuple[str, ...]] = []
        self.error: BaseException | None = None

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        self.inputs.append(tuple(texts))
        if self.error is not None:
            raise self.error
        return tuple((float(len(text)), 1.0, 0.0) for text in texts)


def resources(factory: Factory, index: CandidateIndex) -> BuildResources:
    values = config().model_dump(mode="python")
    values["part_i"]["chunker_behavior_version"] = DEFAULT_CHUNKER_BEHAVIOR_VERSION
    runtime = RuntimeConfig.model_validate(values)
    return BuildResources(
        config=runtime,
        uow_factory=factory,
        parsers=ParserRouter(
            MinerUParser(), DoclingParser(), MarkdownParser(), TextParser()
        ),
        embedding=FakeEmbedding(runtime),
        index=index,
        clock=lambda: NOW,
        index_batch_size=1,
        verification_batch_size=2,
    )


def runner(
    bundle: BuildResources, pool: Any, **overrides: Any
) -> DocumentBuildRunner[Any]:
    options = dict(
        resources=bundle,
        pool=pool,
        lock_manager_factory=PostgresAdvisoryLockManager,
        metadata_repository_factory=RuntimeMetadataRepository,
        max_concurrent_builds=2,
        pool_capacity=4,
    )
    options.update(overrides)
    return DocumentBuildRunner(**options)


def request(
    root: Path, bundle: BuildResources, content: str, suffix: str = "txt"
) -> BuildRequest:
    path = root / f"owned-{uuid4().hex}.{suffix}"
    path.write_text(content, encoding="utf-8")
    return BuildRequest(
        source=SourceFile.from_knowledge_directory(root, path.name),
        expected_pipeline_fingerprint=pipeline_fingerprint(bundle.config),
    )


async def projection(
    index: CandidateIndex, document_id: str, revision_id: str
) -> dict[str, dict[str, Any]]:
    rows = [
        row
        async for row in index.iter_revision_projection(
            document_id, revision_id, batch_size=2
        )
    ]
    result = {row.chunk_id: row.model_dump(mode="json") for row in rows}
    assert len(result) == len(rows), "duplicate scalar projection identity"
    return result


@dataclass(frozen=True)
class PublishedSnapshot:
    document: StoredDocument
    elements: tuple[StoredElement, ...]
    chunks: tuple[StoredChunk, ...]
    rows: dict[str, dict[str, Any]]


async def snapshot(
    factory: Factory, index: CandidateIndex, document_id: str
) -> PublishedSnapshot:
    async with factory() as unit:
        document = await unit.documents.get_by_id(document_id)
        assert document is not None and document.current_revision_id is not None
        revision = document.current_revision_id
        elements = await unit.elements.list_for_revision(document_id, revision)
        chunks = await unit.chunks.list_for_revision(document_id, revision)
    return PublishedSnapshot(
        document, elements, chunks, await projection(index, document_id, revision)
    )


async def assert_last_good(
    factory: Factory, index: CandidateIndex, before: PublishedSnapshot
) -> StoredDocument:
    after = await snapshot(factory, index, before.document.document_id)
    assert after.elements == before.elements
    assert after.chunks == before.chunks
    assert after.rows == before.rows
    for name in (
        "current_revision_id",
        "current_content_hash",
        "current_pipeline_fingerprint",
    ):
        assert getattr(after.document, name) == getattr(before.document, name)
    assert (
        after.document.parse_metadata["current"]
        == before.document.parse_metadata["current"]
    )
    return after.document


def assert_exact_projection(value: PublishedSnapshot) -> None:
    """Full scalar/metadata equality, including parent exclusion and source facts."""
    assert value.document.current_revision_id is not None
    document = map_document_from_stored(
        value.document, value.elements, revision_id=value.document.current_revision_id
    )
    chunks = map_chunks_from_stored(value.chunks)
    builder = RepresentationBuilder(
        RepresentationProfileV1(), representation_version="representation-v1"
    )
    expected = {
        chunk.chunk_id: build_expected_projection(
            document, chunk, builder.build(chunk)
        ).model_dump(mode="json")
        for chunk in chunks
        if chunk.chunk_type is not ChunkType.TEXT_PARENT
    }
    assert value.rows == expected
    assert all(
        chunk.chunk_id not in value.rows
        for chunk in chunks
        if chunk.chunk_type is ChunkType.TEXT_PARENT
    )


async def clear_marker(factory: Factory) -> None:
    async with factory() as unit:
        await unit.runtime_metadata.set_query_cache_invalidation_required(False)
        await unit.commit()


async def marker(factory: Factory) -> bool:
    async with factory() as unit:
        return (await unit.runtime_metadata.get()).query_cache_invalidation_required
