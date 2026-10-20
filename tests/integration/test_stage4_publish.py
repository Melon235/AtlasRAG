"""Real PostgreSQL publication/rollback; embedding and Milvus are test doubles.

These tests prove the SQL transaction boundary with the actual T5 verifier.
The later Stage 4 end-to-end gate owns combined real PostgreSQL/Milvus evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import TracebackType

import pytest
from stage3.test_candidate_services import NOW
from stage4.candidate_fakes import Embedding, Index, candidate
from stage4.test_publication import publication

from atlasrag.domain.errors import InvariantViolationError
from atlasrag.domain.ingestion.source import CandidateBuildContext
from atlasrag.repositories.postgres.chunks import ChunkRepository
from atlasrag.repositories.postgres.documents import DocumentRepository
from atlasrag.repositories.postgres.elements import ElementRepository
from atlasrag.repositories.postgres.records import (
    IngestionStatus,
    RuntimeMetadata,
    StoredDocument,
)
from atlasrag.repositories.postgres.runtime_metadata import RuntimeMetadataRepository
from atlasrag.repositories.postgres.uow import PostgresUnitOfWork
from atlasrag.services.indexing.verification import CandidateVerificationAttempt
from atlasrag.services.ingestion.mapping import load_document_parse_metadata_envelope

pytestmark = pytest.mark.integration


async def seed_and_verify(
    factory: Callable[[], PostgresUnitOfWork],
    root: Path,
    *,
    current: bool,
    marker: bool,
) -> tuple[CandidateBuildContext, CandidateVerificationAttempt, Index]:
    fixture, context, runtime = candidate(root)
    stored = fixture.database.documents[context.document_id]
    if not current:
        metadata = stored.model_dump(mode="python")["parse_metadata"]
        metadata["current"] = None
        stored = StoredDocument.model_validate(
            {
                **stored.model_dump(mode="python"),
                "current_revision_id": None,
                "current_content_hash": None,
                "current_pipeline_fingerprint": None,
                "parse_metadata": metadata,
            }
        )
    async with factory() as unit:
        await unit.documents.put(stored)
        for scope, elements in fixture.database.elements.items():
            await unit.elements.replace_revision_set(*scope, elements)
        for scope, chunks in fixture.database.chunks.items():
            await unit.chunks.replace_revision_set(*scope, chunks)
        await unit.runtime_metadata.set_query_cache_invalidation_required(marker)
        await unit.commit()
    embedding = Embedding(fixture, runtime)
    index = Index(fixture, embedding)
    attempt = CandidateVerificationAttempt(
        factory, context, config=runtime, index=index
    )
    await attempt.index(embedding=embedding, clock=lambda: NOW, batch_size=3)
    await attempt.verify(batch_size=2)
    return context, attempt, index


class FailingDocuments:
    def __init__(self, repository: DocumentRepository) -> None:
        self.repository = repository

    async def get_by_id(self, document_id: str) -> StoredDocument | None:
        return await self.repository.get_by_id(document_id)

    async def put(self, document: StoredDocument) -> None:
        await self.repository.put(document)
        # Fail only after PostgreSQL accepted the complete publication row.
        raise TimeoutError("after actual document write")


class FailingMarker:
    def __init__(self, repository: RuntimeMetadataRepository) -> None:
        self.repository = repository

    async def set_query_cache_invalidation_required(
        self, required: bool
    ) -> RuntimeMetadata:
        result = await self.repository.set_query_cache_invalidation_required(required)
        assert result.query_cache_invalidation_required is True
        # Both actual SQL writes happened on the underlying UoW connection.
        raise TimeoutError("after actual marker write")


class FailingUnit:
    """Delegate transaction/rollback to the original, real PostgreSQL UoW."""

    def __init__(self, unit: PostgresUnitOfWork, phase: str) -> None:
        self.unit = unit
        self.phase = phase

    @property
    def documents(self) -> DocumentRepository | FailingDocuments:
        if self.phase == "document":
            return FailingDocuments(self.unit.documents)
        return self.unit.documents

    @property
    def elements(self) -> ElementRepository:
        return self.unit.elements

    @property
    def chunks(self) -> ChunkRepository:
        return self.unit.chunks

    @property
    def runtime_metadata(self) -> FailingMarker | RuntimeMetadataRepository:
        if self.phase == "marker":
            return FailingMarker(self.unit.runtime_metadata)
        return self.unit.runtime_metadata

    async def __aenter__(self) -> FailingUnit:
        await self.unit.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.unit.__aexit__(exc_type, exc_value, traceback)

    async def commit(self) -> None:
        await self.unit.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [False, True])
@pytest.mark.parametrize("marker", [False, True])
async def test_real_postgres_promotes_verified_candidate_and_cache_marker(
    postgres_uow_factory: Callable[[], PostgresUnitOfWork],
    tmp_path: Path,
    current: bool,
    marker: bool,
) -> None:
    factory = postgres_uow_factory
    context, attempt, index = await seed_and_verify(
        factory, tmp_path, current=current, marker=marker
    )
    async with factory() as unit:
        before = await unit.documents.get_by_id(context.document_id)
        assert before is not None
        old_elements = (
            await unit.elements.list_for_revision(
                context.document_id, before.current_revision_id
            )
            if before.current_revision_id
            else ()
        )
        old_chunks = (
            await unit.chunks.list_for_revision(
                context.document_id, before.current_revision_id
            )
            if before.current_revision_id
            else ()
        )
        candidate_elements = await unit.elements.list_for_revision(
            context.document_id, context.revision_id
        )
        candidate_chunks = await unit.chunks.list_for_revision(
            context.document_id, context.revision_id
        )
    events = list(index.events)
    result = await publication().publish_revision(
        factory, context, verification=attempt, clock=lambda: NOW
    )
    assert result.previous_current_revision_id == before.current_revision_id
    assert result.published_revision_id == context.revision_id
    assert result.document_id == context.document_id
    async with factory() as unit:
        after = await unit.documents.get_by_id(context.document_id)
        assert after is not None
        expected = before.model_dump(mode="python")
        expected.update(
            current_revision_id=before.building_revision_id,
            current_content_hash=before.building_content_hash,
            current_pipeline_fingerprint=before.building_pipeline_fingerprint,
            building_revision_id=None,
            building_content_hash=None,
            building_pipeline_fingerprint=None,
            ingestion_status=IngestionStatus.READY,
            failed_stage=None,
            parse_metadata={
                "schema_version": 1,
                "current": before.parse_metadata["building"],
                "building": None,
            },
        )
        assert after == StoredDocument.model_validate(expected)
        load_document_parse_metadata_envelope(after)
        assert (
            await unit.runtime_metadata.get()
        ).query_cache_invalidation_required is True
        assert (
            await unit.elements.list_for_revision(
                context.document_id, context.revision_id
            )
            == candidate_elements
        )
        assert (
            await unit.chunks.list_for_revision(
                context.document_id, context.revision_id
            )
            == candidate_chunks
        )
        if before.current_revision_id:
            assert (
                await unit.elements.list_for_revision(
                    context.document_id, before.current_revision_id
                )
                == old_elements
            )
            assert (
                await unit.chunks.list_for_revision(
                    context.document_id, before.current_revision_id
                )
                == old_chunks
            )
    assert index.events == events


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["document", "marker"])
@pytest.mark.parametrize("current", [False, True])
@pytest.mark.parametrize("marker", [False, True])
async def test_real_postgres_failed_publication_rolls_back_every_field_and_marker(
    postgres_uow_factory: Callable[[], PostgresUnitOfWork],
    tmp_path: Path,
    phase: str,
    current: bool,
    marker: bool,
) -> None:
    factory = postgres_uow_factory
    context, attempt, index = await seed_and_verify(
        factory, tmp_path, current=current, marker=marker
    )
    async with factory() as unit:
        before = await unit.documents.get_by_id(context.document_id)
        assert before is not None
        scopes = [context.revision_id]
        if before.current_revision_id:
            scopes.append(before.current_revision_id)
        elements = {
            revision: await unit.elements.list_for_revision(
                context.document_id, revision
            )
            for revision in scopes
        }
        chunks = {
            revision: await unit.chunks.list_for_revision(context.document_id, revision)
            for revision in scopes
        }
        metadata = await unit.runtime_metadata.get()
    events = list(index.events)
    with pytest.raises(TimeoutError, match="after actual"):
        await publication().publish_revision(
            lambda: FailingUnit(factory(), phase),
            context,
            verification=attempt,
            clock=lambda: NOW,
        )
    # A fresh original UoW proves rollback in PostgreSQL, not in a fake snapshot.
    async with factory() as unit:
        assert await unit.documents.get_by_id(context.document_id) == before
        assert await unit.runtime_metadata.get() == metadata
        for revision in scopes:
            assert (
                await unit.elements.list_for_revision(context.document_id, revision)
                == elements[revision]
            )
            assert (
                await unit.chunks.list_for_revision(context.document_id, revision)
                == chunks[revision]
            )
    assert index.events == events
    with pytest.raises(InvariantViolationError):
        await publication().publish_revision(
            factory, context, verification=attempt, clock=lambda: NOW
        )
    await attempt.verify()
    await publication().publish_revision(
        factory, context, verification=attempt, clock=lambda: NOW
    )
    async with factory() as unit:
        after = await unit.documents.get_by_id(context.document_id)
        assert after is not None and after.current_revision_id == context.revision_id
        assert (
            await unit.runtime_metadata.get()
        ).query_cache_invalidation_required is True
