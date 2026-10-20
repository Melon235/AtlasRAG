"""Atomic publication uses real invocation proof and transactional persistence."""

from __future__ import annotations

import asyncio
import importlib
from asyncio import CancelledError
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from pydantic import ValidationError
from stage3.fakes import FakeDatabase
from stage3.test_candidate_services import NOW

from atlasrag.domain.errors import CanonicalDataError, InvariantViolationError
from atlasrag.domain.indexing.failures import IndexVerificationError
from atlasrag.domain.indexing.models import PublishResult
from atlasrag.domain.ingestion.source import CandidateBuildContext
from atlasrag.providers.index.projection import MilvusRevisionProjection
from atlasrag.repositories.postgres.records import (
    FailedStage,
    IngestionStatus,
    RuntimeMetadata,
    StoredDocument,
)
from atlasrag.services.indexing.verification import CandidateVerificationAttempt
from atlasrag.services.ingestion.mapping import load_document_parse_metadata_envelope
from stage4.candidate_fakes import (
    Embedding,
    Index,
    IndexFactory,
    IndexUnitOfWork,
    candidate,
)


def publication() -> Any:
    try:
        return importlib.import_module("atlasrag.services.indexing.publication")
    except ModuleNotFoundError:
        pytest.fail("atomic publication is not implemented", pytrace=False)


class Marker:
    def __init__(self, factory: IndexFactory, working: FakeDatabase) -> None:
        self.factory = factory
        self.working = working
        self.return_false = False

    async def set_query_cache_invalidation_required(
        self, required: bool
    ) -> RuntimeMetadata:
        self.factory.calls.append("runtime_metadata.set")
        self.working.query_cache_invalidation_required = required
        self.factory.raise_after_if_planned("runtime_metadata.set")
        return RuntimeMetadata(
            query_cache_invalidation_required=required and not self.return_false
        )


class PublicationUnit:
    """Add marker access by composition; the old fake explicitly forbids it."""

    def __init__(self, inner: IndexUnitOfWork, factory: PublicationFactory) -> None:
        self.inner = inner
        self.factory = factory

    @property
    def documents(self) -> Any:
        return self.inner.documents

    @property
    def elements(self) -> Any:
        return self.inner.elements

    @property
    def chunks(self) -> Any:
        return self.inner.chunks

    @property
    def runtime_metadata(self) -> Marker:
        assert self.inner._working is not None
        result = Marker(self.factory.base, self.inner._working)
        result.return_false = self.factory.return_false
        return result

    async def __aenter__(self) -> PublicationUnit:
        self.factory.base.raise_after_if_planned("uow.enter")
        await self.inner.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.inner.__aexit__(exc_type, exc_value, traceback)

    async def commit(self) -> None:
        await self.inner.commit()


class PublicationFactory:
    def __init__(self, base: IndexFactory) -> None:
        self.base = base
        self.return_false = False

    def __call__(self) -> PublicationUnit:
        self.base.raise_after_if_planned("uow.factory")
        return PublicationUnit(self.base(), self)


async def prepared(
    root: Path, *, current: bool = True, marker: bool = False, verified: bool = True
) -> tuple[
    PublicationFactory, CandidateBuildContext, CandidateVerificationAttempt, Index
]:
    base, context, runtime = candidate(root)
    if not current:
        stored = base.database.documents[context.document_id]
        envelope = stored.model_dump(mode="python")["parse_metadata"]
        envelope["current"] = None
        base.database.documents[context.document_id] = StoredDocument.model_validate(
            {
                **stored.model_dump(mode="python"),
                "current_revision_id": None,
                "current_content_hash": None,
                "current_pipeline_fingerprint": None,
                "parse_metadata": envelope,
            }
        )
    base.database.query_cache_invalidation_required = marker
    factory = PublicationFactory(base)
    embedding = Embedding(base, runtime)
    index = Index(base, embedding)
    attempt = CandidateVerificationAttempt(
        factory, context, config=runtime, index=index
    )
    await attempt.index(embedding=embedding, clock=lambda: NOW)
    if verified:
        await attempt.verify()
    return factory, context, attempt, index


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [False, True])
@pytest.mark.parametrize("marker", [False, True])
async def test_publish_promotes_complete_triple_and_slot_with_one_commit(
    tmp_path: Path, current: bool, marker: bool
) -> None:
    factory, context, attempt, index = await prepared(
        tmp_path, current=current, marker=marker
    )
    base = factory.base
    before = base.database.clone()
    stored = before.documents[context.document_id]
    envelope = load_document_parse_metadata_envelope(stored)
    commits = base.commit_count
    units = len(base.unit_of_works)
    calls = len(base.calls)
    provider_events = list(index.events)
    published_at = NOW + timedelta(seconds=1)
    result = await publication().publish_revision(
        factory, context, verification=attempt, clock=lambda: published_at
    )
    assert result == PublishResult(
        document_id=context.document_id,
        published_revision_id=context.revision_id,
        previous_current_revision_id=stored.current_revision_id,
    )
    expected = stored.model_dump(mode="python")
    expected.update(
        current_revision_id=stored.building_revision_id,
        current_content_hash=stored.building_content_hash,
        current_pipeline_fingerprint=stored.building_pipeline_fingerprint,
        building_revision_id=None,
        building_content_hash=None,
        building_pipeline_fingerprint=None,
        ingestion_status=IngestionStatus.READY,
        failed_stage=None,
        updated_at=published_at,
        parse_metadata={
            "schema_version": 1,
            "current": envelope.building.model_dump(mode="json")
            if envelope.building
            else None,
            "building": None,
        },
    )
    assert base.database.documents[
        context.document_id
    ] == StoredDocument.model_validate(expected)
    assert base.database.query_cache_invalidation_required is True
    assert base.database.elements == before.elements
    assert base.database.chunks == before.chunks
    assert index.events == provider_events
    assert base.commit_count == commits + 1
    assert len(base.unit_of_works) == units + 1
    assert base.calls[calls:] == [
        "documents.get_by_id",
        "elements.list_for_revision",
        "chunks.list_for_revision",
        "documents.put",
        "runtime_metadata.set",
    ]
    with pytest.raises(InvariantViolationError):
        await publication().publish_revision(
            factory, context, verification=attempt, clock=lambda: NOW
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "cross_invocation",
        "cross_context",
        "consumed",
        "stale_document",
        "stale_chunks",
        "stale_elements",
        "missing_document",
        "building_revision_id",
        "building_content_hash",
        "building_pipeline_fingerprint",
        "slot",
        "wrong_building_slot",
        "wrong_current_slot",
        "unknown_metadata",
        "invalid_failure_metadata",
        "status",
        "current_equals_building",
    ],
)
async def test_invalid_proof_or_persisted_candidate_never_publishes(
    tmp_path: Path, fault: str
) -> None:
    factory, context, attempt, index = await prepared(
        tmp_path, verified=fault != "missing"
    )
    base = factory.base
    stored = base.database.documents[context.document_id]
    if fault == "cross_invocation":
        from stage4.candidate_fakes import config

        attempt = CandidateVerificationAttempt(
            factory, context, config=config(), index=index
        )
    elif fault == "cross_context":
        from atlasrag.services.ingestion.identity import build_candidate_context

        context = build_candidate_context(context.source, "0" * 64)
    elif fault == "consumed":
        async with factory() as unit:
            await attempt.consume_verified(unit, context)
    elif fault == "stale_document":
        base.database.documents[context.document_id] = stored.model_copy(
            update={"created_at": NOW + timedelta(seconds=3)}
        )
    elif fault == "stale_chunks":
        scope = context.document_id, context.revision_id
        base.database.chunks[scope] = tuple(
            chunk.model_copy(update={"created_at": NOW + timedelta(seconds=3)})
            for chunk in base.database.chunks[scope]
        )
    elif fault == "stale_elements":
        scope = context.document_id, context.revision_id
        base.database.elements[scope] = tuple(
            element.model_copy(update={"created_at": NOW + timedelta(seconds=3)})
            for element in base.database.elements[scope]
        )
    elif fault == "missing_document":
        del base.database.documents[context.document_id]
    elif fault.startswith("building_"):
        base.database.documents[context.document_id] = stored.model_copy(
            update={fault: "0" * 64}
        )
    elif fault == "slot":
        base.database.documents[context.document_id] = stored.model_copy(
            update={"parse_metadata": {**stored.parse_metadata, "building": None}}
        )
    elif fault in {"wrong_building_slot", "wrong_current_slot", "unknown_metadata"}:
        metadata = stored.model_dump(mode="python")["parse_metadata"]
        if fault == "unknown_metadata":
            metadata["unrecognized"] = "not allowed"
        else:
            slot = "building" if fault == "wrong_building_slot" else "current"
            metadata[slot]["revision_id"] = "0" * 64
        base.database.documents[context.document_id] = stored.model_copy(
            update={"parse_metadata": metadata}
        )
    elif fault == "invalid_failure_metadata":
        base.database.documents[context.document_id] = stored.model_copy(
            update={"failed_stage": FailedStage.INDEX_WRITE}
        )
    elif fault == "status":
        base.database.documents[context.document_id] = stored.model_copy(
            update={"ingestion_status": IngestionStatus.CHUNKED}
        )
    elif fault == "current_equals_building":
        base.database.documents[context.document_id] = stored.model_copy(
            update={
                "current_revision_id": stored.building_revision_id,
                "current_content_hash": stored.building_content_hash,
                "current_pipeline_fingerprint": stored.building_pipeline_fingerprint,
            }
        )
    before = base.database.clone()
    commits = base.commit_count
    with pytest.raises(
        (
            InvariantViolationError,
            IndexVerificationError,
            CanonicalDataError,
            ValidationError,
        )
    ):
        await publication().publish_revision(
            factory, context, verification=attempt, clock=lambda: NOW
        )
    assert base.database == before
    assert base.commit_count == commits
    assert "runtime_metadata.set" not in base.calls
    with pytest.raises(InvariantViolationError):
        await publication().publish_revision(
            factory, context, verification=attempt, clock=lambda: NOW
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [status for status in IngestionStatus if status is not IngestionStatus.INDEXING],
)
async def test_publication_rejects_every_nonindexing_status(
    tmp_path: Path, status: IngestionStatus
) -> None:
    factory, context, attempt, _ = await prepared(tmp_path)
    base = factory.base
    stored = base.database.documents[context.document_id]
    base.database.documents[context.document_id] = stored.model_copy(
        update={
            "ingestion_status": status,
            "failed_stage": FailedStage.INDEX_VERIFY
            if status is IngestionStatus.FAILED
            else None,
        }
    )
    before = base.database.clone()
    commits = base.commit_count
    with pytest.raises(InvariantViolationError):
        await publication().publish_revision(
            factory, context, verification=attempt, clock=lambda: NOW
        )
    assert base.database == before
    assert base.commit_count == commits
    assert "runtime_metadata.set" not in base.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "uow.factory",
        "uow.enter",
        "documents.get_by_id",
        "documents.put",
        "runtime_metadata.set",
        "uow.commit",
        "clock",
    ],
)
@pytest.mark.parametrize("cancel", [False, True])
async def test_every_publication_failure_rolls_back_and_requires_reverification(
    tmp_path: Path, operation: str, cancel: bool
) -> None:
    factory, context, attempt, _ = await prepared(tmp_path)
    before = factory.base.database.clone()
    commits = factory.base.commit_count
    error = CancelledError() if cancel else TimeoutError("controlled failure")

    def clock() -> datetime:
        if operation == "clock":
            raise error
        return NOW

    if operation != "clock":
        factory.base.fail_after(operation, error)
    with pytest.raises(type(error)) as caught:
        await publication().publish_revision(
            factory, context, verification=attempt, clock=clock
        )
    assert caught.value is error
    assert factory.base.database == before
    assert factory.base.commit_count == commits
    with pytest.raises(InvariantViolationError):
        await publication().publish_revision(
            factory, context, verification=attempt, clock=lambda: NOW
        )
    await attempt.verify()
    await publication().publish_revision(
        factory, context, verification=attempt, clock=lambda: NOW
    )
    assert (
        factory.base.database.documents[context.document_id].current_revision_id
        == context.revision_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timestamp",
    [
        NOW.replace(tzinfo=None),
        NOW.astimezone(timezone(timedelta(hours=8))),
        "not a timestamp",
    ],
)
async def test_invalid_clock_consumes_proof_without_writing(
    tmp_path: Path, timestamp: object
) -> None:
    factory, context, attempt, _ = await prepared(tmp_path)
    before = factory.base.database.clone()
    with pytest.raises(ValueError, match="aware UTC"):
        await publication().publish_revision(
            factory, context, verification=attempt, clock=lambda: timestamp
        )
    assert factory.base.database == before
    with pytest.raises(InvariantViolationError):
        await publication().publish_revision(
            factory, context, verification=attempt, clock=lambda: NOW
        )


@pytest.mark.asyncio
async def test_marker_must_confirm_invalidation_before_commit(tmp_path: Path) -> None:
    factory, context, attempt, _ = await prepared(tmp_path)
    before = factory.base.database.clone()
    factory.return_false = True
    with pytest.raises(InvariantViolationError):
        await publication().publish_revision(
            factory, context, verification=attempt, clock=lambda: NOW
        )
    assert factory.base.database == before
    with pytest.raises(InvariantViolationError):
        await publication().publish_revision(
            factory, context, verification=attempt, clock=lambda: NOW
        )


@pytest.mark.asyncio
async def test_invalidation_prevents_pending_verification_from_rearming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, context, attempt, index = await prepared(tmp_path)
    invalidate = getattr(attempt, "invalidate_verified", None)
    assert invalidate is not None, (
        "publication needs a fail-only proof invalidation capability"
    )
    started, release = asyncio.Event(), asyncio.Event()
    original = index.iter_revision_projection

    async def pending(
        *args: object, **kwargs: object
    ) -> AsyncGenerator[MilvusRevisionProjection, None]:
        started.set()
        await release.wait()
        for row in index.rows:
            yield row

    monkeypatch.setattr(index, "iter_revision_projection", pending)
    task = asyncio.create_task(attempt.verify())
    await started.wait()
    invalidate()
    with pytest.raises(InvariantViolationError):
        await attempt.verify()  # Invalidation must not release the active owner.
    release.set()
    with pytest.raises(InvariantViolationError):
        await task
    async with factory() as unit:
        with pytest.raises(InvariantViolationError):
            await attempt.consume_verified(unit, context)
    monkeypatch.setattr(index, "iter_revision_projection", original)
    await attempt.verify()
    async with factory() as unit:
        assert (await attempt.consume_verified(unit, context)).context == context
