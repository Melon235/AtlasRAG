"""Candidate persistence capabilities at the Stage 3 transaction boundary."""

from __future__ import annotations

import importlib
from asyncio import CancelledError
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from atlasrag.domain.errors import CanonicalDataError, InvariantViolationError
from atlasrag.domain.ingestion.document import (
    DocumentContainer,
    DocumentMetadata,
    ParseInfo,
)
from atlasrag.domain.ingestion.enums import (
    ElementType,
    ParseOutcome,
    ParseReliability,
)
from atlasrag.domain.ingestion.source import (
    CandidateBuildContext,
    CanonicalSource,
    derive_revision_id,
)
from atlasrag.repositories.postgres.records import (
    FailedStage,
    IngestionStatus,
    JsonObject,
    StoredDocument,
    StoredElement,
)
from atlasrag.services.ingestion.chunking import ChunkingProfile, build_chunks
from atlasrag.services.ingestion.identity import build_candidate_context, build_element
from atlasrag.services.ingestion.mapping import (
    DocumentParseMetadataEnvelopeV1,
    build_document_parse_metadata_slot,
    empty_document_parse_metadata_envelope,
    load_document_parse_metadata_envelope,
    map_chunks_to_stored,
    map_elements_to_stored,
    replace_document_parse_metadata_slot,
    serialize_document_parse_metadata_envelope,
)
from stage3.fakes import FakeDatabase, FakeUnitOfWorkFactory

NOW = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
CREATED_AT = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
PIPELINE_FINGERPRINT = "f" * 64
CURRENT_CONTENT_HASH = "a" * 64
CURRENT_PIPELINE_FINGERPRINT = "b" * 64


def _services() -> ModuleType:
    try:
        return importlib.import_module("atlasrag.services.ingestion.persistence")
    except ModuleNotFoundError:
        pytest.fail("candidate persistence services are not implemented", pytrace=False)


def _clock() -> datetime:
    return NOW


def _context(
    root: Path,
    *,
    relative_path: str = "guides/candidate.txt",
    content: bytes = b"candidate source bytes",
) -> CandidateBuildContext:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    from atlasrag.domain.ingestion.source import SourceFile

    source = SourceFile.from_knowledge_directory(root, relative_path)
    return build_candidate_context(source, PIPELINE_FINGERPRINT)


def _document(
    context: CandidateBuildContext,
    *contents: str,
    revision_id: str | None = None,
    title: str = "Candidate",
    parser_version: str = "1",
) -> DocumentContainer:
    canonical_revision_id = revision_id or context.revision_id
    values = contents or (
        "A canonical paragraph with enough text for deterministic child chunks.",
    )
    elements = tuple(
        build_element(
            document_id=context.document_id,
            revision_id=canonical_revision_id,
            element_type=ElementType.PARAGRAPH,
            order=order,
            content=content,
            section_path=("Candidate",),
        )
        for order, content in enumerate(values)
    )
    return DocumentContainer(
        document_id=context.document_id,
        revision_id=canonical_revision_id,
        source=CanonicalSource.from_source_file(context.source),
        metadata=DocumentMetadata(title=title),
        parse_info=ParseInfo(
            outcome=ParseOutcome.SUCCESS,
            reliability=ParseReliability.RELIABLE,
            parser_name="controlled",
            parser_version=parser_version,
        ),
        elements=elements,
    )


def _current_triple(context: CandidateBuildContext) -> tuple[str, str, str]:
    return (
        derive_revision_id(
            context.document_id,
            CURRENT_CONTENT_HASH,
            CURRENT_PIPELINE_FINGERPRINT,
        ),
        CURRENT_CONTENT_HASH,
        CURRENT_PIPELINE_FINGERPRINT,
    )


def _current_document(context: CandidateBuildContext) -> DocumentContainer:
    current_revision, _, _ = _current_triple(context)
    return _document(
        context,
        "Published current revision.",
        revision_id=current_revision,
        title="Published",
        parser_version="published-v1",
    )


def _typed_envelope(
    context: CandidateBuildContext,
    *,
    current: bool,
    building_document: DocumentContainer | None,
) -> DocumentParseMetadataEnvelopeV1:
    envelope = empty_document_parse_metadata_envelope()
    if current:
        envelope = replace_document_parse_metadata_slot(
            envelope,
            slot_name="current",
            slot=build_document_parse_metadata_slot(_current_document(context)),
        )
    if building_document is not None:
        envelope = replace_document_parse_metadata_slot(
            envelope,
            slot_name="building",
            slot=build_document_parse_metadata_slot(building_document),
        )
    return envelope


def _serialize_envelope(envelope: DocumentParseMetadataEnvelopeV1) -> JsonObject:
    return cast(JsonObject, serialize_document_parse_metadata_envelope(envelope))


def _stored_document(
    context: CandidateBuildContext,
    *,
    status: IngestionStatus,
    current: bool = False,
    building: bool = True,
    failed_stage: FailedStage | None = None,
    building_document: DocumentContainer | None = None,
    envelope: DocumentParseMetadataEnvelopeV1 | None = None,
) -> StoredDocument:
    current_revision, current_hash, current_pipeline = _current_triple(context)
    owns_building_parse_metadata = status in {
        IngestionStatus.PARSED,
        IngestionStatus.CHUNKING,
        IngestionStatus.CHUNKED,
        IngestionStatus.INDEXING,
    } or (
        status is IngestionStatus.FAILED
        and failed_stage
        in {
            FailedStage.CHUNK,
            FailedStage.REPRESENTATION,
            FailedStage.EMBEDDING,
            FailedStage.INDEX_WRITE,
            FailedStage.INDEX_VERIFY,
        }
    )
    typed_envelope = envelope or _typed_envelope(
        context,
        current=current,
        building_document=(
            building_document or _document(context)
            if building and owns_building_parse_metadata
            else None
        ),
    )
    return StoredDocument(
        document_id=context.document_id,
        file_name=context.source.file_name,
        relative_source_path=context.source.relative_source_path,
        source_type=context.source.source_type,
        observed_content_hash=context.content_hash,
        current_revision_id=current_revision if current else None,
        current_content_hash=current_hash if current else None,
        current_pipeline_fingerprint=current_pipeline if current else None,
        building_revision_id=context.revision_id if building else None,
        building_content_hash=context.content_hash if building else None,
        building_pipeline_fingerprint=(
            context.pipeline_fingerprint if building else None
        ),
        ingestion_status=status,
        failed_stage=failed_stage,
        parse_metadata=_serialize_envelope(typed_envelope),
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


def _database_with(document: StoredDocument) -> FakeDatabase:
    return FakeDatabase(documents={document.document_id: document})


def _stored(
    factory: FakeUnitOfWorkFactory, context: CandidateBuildContext
) -> StoredDocument:
    return factory.database.documents[context.document_id]


@pytest.mark.asyncio
async def test_prepare_new_candidate_installs_received_identity_and_commits_once(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path)
    factory = FakeUnitOfWorkFactory()

    result = await services.prepare_candidate(factory, context, clock=_clock)

    assert result == _stored(factory, context)
    assert result.model_dump(mode="python") == {
        "document_id": context.document_id,
        "file_name": context.source.file_name,
        "relative_source_path": context.source.relative_source_path,
        "source_type": context.source.source_type,
        "observed_content_hash": context.content_hash,
        "current_revision_id": None,
        "current_content_hash": None,
        "current_pipeline_fingerprint": None,
        "building_revision_id": context.revision_id,
        "building_content_hash": context.content_hash,
        "building_pipeline_fingerprint": context.pipeline_fingerprint,
        "ingestion_status": IngestionStatus.RECEIVED,
        "failed_stage": None,
        "parse_metadata": _serialize_envelope(empty_document_parse_metadata_envelope()),
        "created_at": NOW,
        "updated_at": NOW,
    }
    assert len(factory.unit_of_works) == 1
    assert factory.unit_of_works[0].commit_count == 1
    assert factory.rollback_count == 0
    assert factory.runtime_metadata_accesses == 0


@pytest.mark.asyncio
async def test_prepare_current_r1_preserves_current_triple_and_existing_metadata(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path, content=b"changed R2 bytes")
    existing = _stored_document(
        context,
        status=IngestionStatus.READY,
        current=True,
        building=False,
    )
    existing_payload = existing.model_dump(mode="python")
    existing_payload["observed_content_hash"] = CURRENT_CONTENT_HASH
    existing = StoredDocument.model_validate(existing_payload)
    existing_envelope = load_document_parse_metadata_envelope(existing)
    factory = FakeUnitOfWorkFactory(_database_with(existing))
    expected_current = _current_triple(context)

    prepared = await services.prepare_candidate(factory, context, clock=_clock)

    assert (
        prepared.current_revision_id,
        prepared.current_content_hash,
        prepared.current_pipeline_fingerprint,
    ) == expected_current
    assert prepared.created_at == CREATED_AT
    prepared_envelope = load_document_parse_metadata_envelope(prepared)
    assert prepared.parse_metadata == existing.parse_metadata
    assert prepared_envelope.current == existing_envelope.current
    assert prepared_envelope.building is None
    assert prepared.observed_content_hash == context.content_hash
    assert prepared.ingestion_status is IngestionStatus.RECEIVED
    assert prepared.failed_stage is None
    assert factory.commit_count == 1


@pytest.mark.asyncio
async def test_prepare_same_building_attempt_clears_only_building_metadata(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path, content=b"changed R2 bytes")
    document = _document(context)
    existing = _stored_document(
        context,
        status=IngestionStatus.PARSED,
        current=True,
        building_document=document,
    )
    original_envelope = load_document_parse_metadata_envelope(existing)
    factory = FakeUnitOfWorkFactory(_database_with(existing))

    prepared = await services.prepare_candidate(factory, context, clock=_clock)

    prepared_envelope = load_document_parse_metadata_envelope(prepared)
    assert prepared.ingestion_status is IngestionStatus.RECEIVED
    assert prepared_envelope.current == original_envelope.current
    assert prepared_envelope.building is None
    assert factory.commit_count == 1
    assert factory.rollback_count == 0


@pytest.mark.asyncio
async def test_prepare_rejects_candidate_revision_equal_to_current_before_writes(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path)
    current_slot = build_document_parse_metadata_slot(_document(context))
    envelope = replace_document_parse_metadata_slot(
        empty_document_parse_metadata_envelope(),
        slot_name="current",
        slot=current_slot,
    )
    existing = StoredDocument(
        document_id=context.document_id,
        file_name=context.source.file_name,
        relative_source_path=context.source.relative_source_path,
        source_type=context.source.source_type,
        observed_content_hash=context.content_hash,
        current_revision_id=context.revision_id,
        current_content_hash=context.content_hash,
        current_pipeline_fingerprint=context.pipeline_fingerprint,
        ingestion_status=IngestionStatus.READY,
        parse_metadata=_serialize_envelope(envelope),
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )
    factory = FakeUnitOfWorkFactory(_database_with(existing))

    with pytest.raises(InvariantViolationError):
        await services.prepare_candidate(factory, context, clock=_clock)

    assert _stored(factory, context) == existing
    assert factory.calls == ["documents.get_by_relative_source_path"]
    assert factory.document_writes == []
    assert factory.commit_count == 0
    assert factory.rollback_count == 1


@pytest.mark.asyncio
async def test_prepare_rejects_a_different_existing_building_revision_fail_closed(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path)
    other_hash = "d" * 64
    other_revision = derive_revision_id(
        context.document_id,
        other_hash,
        context.pipeline_fingerprint,
    )
    existing = _stored_document(
        context,
        status=IngestionStatus.PARSING,
        current=True,
    )
    payload = existing.model_dump(mode="python")
    payload.update(
        {
            "observed_content_hash": other_hash,
            "building_revision_id": other_revision,
            "building_content_hash": other_hash,
        }
    )
    existing = StoredDocument.model_validate(payload)
    factory = FakeUnitOfWorkFactory(_database_with(existing))

    with pytest.raises(InvariantViolationError):
        await services.prepare_candidate(factory, context, clock=_clock)

    assert _stored(factory, context) == existing
    assert factory.commit_count == 0
    assert factory.rollback_count == 1


@pytest.mark.asyncio
async def test_later_capability_rejects_corrupt_current_building_alias_before_write(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path)
    document = _document(context)
    slot = build_document_parse_metadata_slot(document)
    envelope = replace_document_parse_metadata_slot(
        empty_document_parse_metadata_envelope(),
        slot_name="current",
        slot=slot,
    )
    envelope = replace_document_parse_metadata_slot(
        envelope,
        slot_name="building",
        slot=slot,
    )
    base = _stored_document(
        context,
        status=IngestionStatus.PARSED,
        building_document=document,
    )
    payload = base.model_dump(mode="python")
    payload.update(
        {
            "current_revision_id": context.revision_id,
            "current_content_hash": context.content_hash,
            "current_pipeline_fingerprint": context.pipeline_fingerprint,
            "parse_metadata": _serialize_envelope(envelope),
        }
    )
    corrupted = StoredDocument.model_validate(payload)
    factory = FakeUnitOfWorkFactory(_database_with(corrupted))

    with pytest.raises(CanonicalDataError):
        await services.mark_chunking(factory, context, clock=_clock)

    assert _stored(factory, context) == corrupted
    assert factory.calls == ["documents.get_by_id"]
    assert factory.document_writes == []
    assert factory.commit_count == 0
    assert factory.rollback_count == 1


@pytest.mark.asyncio
async def test_exact_candidate_lifecycle_uses_one_short_committed_uow_per_capability(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path, content=b"R2 source")
    current = _stored_document(
        context,
        status=IngestionStatus.READY,
        current=True,
        building=False,
    )
    factory = FakeUnitOfWorkFactory(_database_with(current))
    document = _document(
        context,
        "First canonical paragraph with deterministic structure.",
        "Second canonical paragraph with deterministic structure.",
    )
    profile = ChunkingProfile(
        parent_max_chars=90,
        child_max_chars=25,
        table_slice_max_chars=90,
    )
    chunks = build_chunks(document, profile)
    expected_current = _current_triple(context)
    original_current_slot = load_document_parse_metadata_envelope(current).current

    prepared = await services.prepare_candidate(factory, context, clock=_clock)
    parsing = await services.mark_parsing(factory, context, clock=_clock)
    parsed = await services.persist_parsed(
        factory,
        context,
        document,
        clock=_clock,
    )
    chunking = await services.mark_chunking(factory, context, clock=_clock)
    chunked = await services.persist_chunked(
        factory,
        context,
        document,
        chunks,
        chunker_behavior_version="chunker-test-v1",
        profile_fingerprint=profile.profile_fingerprint,
        clock=_clock,
    )

    assert [
        prepared.ingestion_status,
        parsing.ingestion_status,
        parsed.ingestion_status,
        chunking.ingestion_status,
        chunked.ingestion_status,
    ] == [
        IngestionStatus.RECEIVED,
        IngestionStatus.PARSING,
        IngestionStatus.PARSED,
        IngestionStatus.CHUNKING,
        IngestionStatus.CHUNKED,
    ]
    assert all(
        (
            state.current_revision_id,
            state.current_content_hash,
            state.current_pipeline_fingerprint,
        )
        == expected_current
        for state in (prepared, parsing, parsed, chunking, chunked)
    )
    assert len(factory.unit_of_works) == 5
    assert [unit.commit_count for unit in factory.unit_of_works] == [1] * 5
    assert factory.rollback_count == 0
    assert factory.runtime_metadata_accesses == 0
    prepared_envelope = load_document_parse_metadata_envelope(prepared)
    parsed_envelope = load_document_parse_metadata_envelope(parsed)
    chunked_envelope = load_document_parse_metadata_envelope(chunked)
    assert prepared_envelope.current == original_current_slot
    assert prepared_envelope.building is None
    assert parsed_envelope.current == original_current_slot
    assert parsed_envelope.building == build_document_parse_metadata_slot(document)
    assert chunked_envelope == parsed_envelope
    stored_elements = factory.database.elements[
        (context.document_id, context.revision_id)
    ]
    assert [element.element_id for element in stored_elements] == [
        element.element_id for element in document.elements
    ]
    assert all(element.metadata["schema_version"] == 1 for element in stored_elements)
    stored_chunks = factory.database.chunks[(context.document_id, context.revision_id)]
    assert [chunk.chunk_id for chunk in stored_chunks] == [
        chunk.chunk_id for chunk in chunks
    ]
    assert all(chunk.metadata["schema_version"] == 1 for chunk in stored_chunks)
    assert all(
        chunk.strategy_metadata
        == {
            "schema_version": 1,
            "chunking_strategy": canonical.chunking_strategy.value,
            "chunker_behavior_version": "chunker-test-v1",
            "profile_fingerprint": profile.profile_fingerprint,
        }
        for chunk, canonical in zip(stored_chunks, chunks, strict=True)
    )

    discarded = await services.discard_candidate(factory, context, clock=_clock)
    discarded_envelope = load_document_parse_metadata_envelope(discarded)
    assert discarded.ingestion_status is IngestionStatus.READY
    assert discarded_envelope.current == original_current_slot
    assert discarded_envelope.building is None
    assert [unit.commit_count for unit in factory.unit_of_works] == [1] * 6
    assert factory.rollback_count == 0


@pytest.mark.asyncio
async def test_illegal_candidate_status_transitions_fail_without_commit(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path)
    document = _document(context)
    profile = ChunkingProfile(
        parent_max_chars=80,
        child_max_chars=20,
        table_slice_max_chars=80,
    )
    chunks = build_chunks(document, profile)
    cases: tuple[
        tuple[
            IngestionStatus,
            Callable[[FakeUnitOfWorkFactory], Awaitable[object]],
        ],
        ...,
    ] = (
        (
            IngestionStatus.PARSING,
            lambda factory: services.mark_parsing(factory, context, clock=_clock),
        ),
        (
            IngestionStatus.RECEIVED,
            lambda factory: services.persist_parsed(
                factory, context, document, clock=_clock
            ),
        ),
        (
            IngestionStatus.PARSING,
            lambda factory: services.mark_chunking(factory, context, clock=_clock),
        ),
        (
            IngestionStatus.PARSED,
            lambda factory: services.persist_chunked(
                factory,
                context,
                document,
                chunks,
                chunker_behavior_version="chunker-test-v1",
                profile_fingerprint=profile.profile_fingerprint,
                clock=_clock,
            ),
        ),
    )

    for status, invoke in cases:
        existing = _stored_document(context, status=status)
        factory = FakeUnitOfWorkFactory(_database_with(existing))
        with pytest.raises(InvariantViolationError):
            await invoke(factory)
        assert _stored(factory, context) == existing
        assert factory.commit_count == 0
        assert factory.rollback_count == 1


@pytest.mark.asyncio
async def test_parsed_same_candidate_retry_exact_set_replaces_stale_elements(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path)
    first = _document(context, "First output.", "Stale second output.")
    retry = _document(context, "Replacement output after a deterministic retry.")
    factory = FakeUnitOfWorkFactory(
        _database_with(_stored_document(context, status=IngestionStatus.PARSING))
    )

    await services.persist_parsed(factory, context, first, clock=_clock)
    await services.persist_parsed(factory, context, retry, clock=_clock)

    records = factory.database.elements[(context.document_id, context.revision_id)]
    assert tuple(record.element_id for record in records) == tuple(
        element.element_id for element in retry.elements
    )
    assert not {element.element_id for element in first.elements} - {
        element.element_id for element in retry.elements
    } <= {record.element_id for record in records}
    assert _stored(factory, context).ingestion_status is IngestionStatus.PARSED
    assert [unit.commit_count for unit in factory.unit_of_works] == [1, 1]


@pytest.mark.asyncio
async def test_chunked_same_candidate_retry_exact_set_replaces_stale_chunks(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path)
    document = _document(
        context,
        "A long canonical paragraph. " * 12,
    )
    first_profile = ChunkingProfile(
        parent_max_chars=100,
        child_max_chars=35,
        table_slice_max_chars=100,
    )
    retry_profile = ChunkingProfile(
        parent_max_chars=70,
        child_max_chars=18,
        table_slice_max_chars=70,
    )
    first = build_chunks(document, first_profile)
    retry = build_chunks(document, retry_profile)
    stored_document = _stored_document(
        context,
        status=IngestionStatus.CHUNKING,
        building_document=document,
    )
    database = _database_with(stored_document)
    database.elements[(context.document_id, context.revision_id)] = (
        map_elements_to_stored(document.elements, created_at=CREATED_AT)
    )
    factory = FakeUnitOfWorkFactory(database)

    await services.persist_chunked(
        factory,
        context,
        document,
        first,
        chunker_behavior_version="chunker-test-first",
        profile_fingerprint=first_profile.profile_fingerprint,
        clock=_clock,
    )
    await services.persist_chunked(
        factory,
        context,
        document,
        retry,
        chunker_behavior_version="chunker-test-retry",
        profile_fingerprint=retry_profile.profile_fingerprint,
        clock=_clock,
    )

    records = factory.database.chunks[(context.document_id, context.revision_id)]
    assert tuple(record.chunk_id for record in records) == tuple(
        chunk.chunk_id for chunk in retry
    )
    assert _stored(factory, context).ingestion_status is IngestionStatus.CHUNKED
    assert [unit.commit_count for unit in factory.unit_of_works] == [1, 1]


@pytest.mark.asyncio
async def test_chunk_persistence_rejects_semantically_different_caller_document(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path)
    persisted = _document(context, "Persisted canonical element.")
    caller = DocumentContainer.model_validate(
        persisted.model_copy(update={"metadata": DocumentMetadata(title="Different")})
    )
    profile = ChunkingProfile()
    chunks = build_chunks(caller, profile)
    stored_document = _stored_document(
        context,
        status=IngestionStatus.CHUNKING,
        building_document=persisted,
    )
    database = _database_with(stored_document)
    scope = (context.document_id, context.revision_id)
    database.elements[scope] = map_elements_to_stored(
        persisted.elements,
        created_at=CREATED_AT,
    )
    factory = FakeUnitOfWorkFactory(database)

    with pytest.raises(InvariantViolationError):
        await services.persist_chunked(
            factory,
            context,
            caller,
            chunks,
            chunker_behavior_version="chunker-authority-v1",
            profile_fingerprint=profile.profile_fingerprint,
            clock=_clock,
        )

    assert factory.database.documents[context.document_id] == stored_document
    assert scope not in factory.database.chunks
    assert "chunks.replace_revision_set" not in factory.calls
    assert "documents.put" not in factory.calls
    assert factory.commit_count == 0
    assert factory.rollback_count == 1


@pytest.mark.asyncio
async def test_chunk_persistence_propagates_safe_corrupt_persisted_element_error(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path)
    document = _document(context, "Persisted canonical element.")
    profile = ChunkingProfile()
    chunks = build_chunks(document, profile)
    stored_document = _stored_document(
        context,
        status=IngestionStatus.CHUNKING,
        building_document=document,
    )
    valid_element = map_elements_to_stored(
        document.elements,
        created_at=CREATED_AT,
    )[0]
    element_payload = valid_element.model_dump(mode="python")
    element_payload["metadata"] = {
        "schema_version": 2,
        "canonical_metadata": valid_element.metadata["canonical_metadata"],
    }
    corrupted_element = StoredElement.model_construct(**element_payload)
    database = _database_with(stored_document)
    scope = (context.document_id, context.revision_id)
    database.elements[scope] = (corrupted_element,)
    factory = FakeUnitOfWorkFactory(database)

    with pytest.raises(CanonicalDataError):
        await services.persist_chunked(
            factory,
            context,
            document,
            chunks,
            chunker_behavior_version="chunker-authority-v1",
            profile_fingerprint=profile.profile_fingerprint,
            clock=_clock,
        )

    assert factory.database.documents[context.document_id] == stored_document
    assert scope not in factory.database.chunks
    assert "chunks.replace_revision_set" not in factory.calls
    assert "documents.put" not in factory.calls
    assert factory.commit_count == 0
    assert factory.rollback_count == 1


@pytest.mark.asyncio
async def test_fail_closed_validation_and_scope_checks_happen_before_any_write(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path / "first", relative_path="one.txt")
    other = _context(tmp_path / "second", relative_path="two.txt")
    valid = _document(context)
    invalid = valid.model_copy(update={"elements": ()})
    other_document = _document(other)

    invalid_factory = FakeUnitOfWorkFactory(
        _database_with(_stored_document(context, status=IngestionStatus.PARSING))
    )
    with pytest.raises(InvariantViolationError):
        await services.persist_parsed(
            invalid_factory,
            context,
            invalid,
            clock=_clock,
        )
    assert invalid_factory.unit_of_works == []
    assert invalid_factory.calls == []

    wrong_scope_factory = FakeUnitOfWorkFactory(
        _database_with(_stored_document(context, status=IngestionStatus.PARSING))
    )
    with pytest.raises(InvariantViolationError):
        await services.persist_parsed(
            wrong_scope_factory,
            context,
            other_document,
            clock=_clock,
        )
    assert wrong_scope_factory.unit_of_works == []
    assert wrong_scope_factory.calls == []

    chunk_factory = FakeUnitOfWorkFactory(
        _database_with(_stored_document(context, status=IngestionStatus.CHUNKING))
    )
    with pytest.raises(InvariantViolationError):
        await services.persist_chunked(
            chunk_factory,
            context,
            valid,
            (),
            chunker_behavior_version="chunker-test",
            profile_fingerprint="e" * 64,
            clock=_clock,
        )
    assert chunk_factory.unit_of_works == []
    assert chunk_factory.calls == []


@pytest.mark.asyncio
async def test_clock_and_typed_chunk_mapping_fail_before_opening_a_uow(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path)
    document = _document(context)
    profile = ChunkingProfile()
    chunks = build_chunks(document, profile)

    naive_factory = FakeUnitOfWorkFactory(
        _database_with(_stored_document(context, status=IngestionStatus.PARSING))
    )
    with pytest.raises(ValueError, match="UTC"):
        await services.persist_parsed(
            naive_factory,
            context,
            document,
            clock=lambda: NOW.replace(tzinfo=None),
        )
    assert naive_factory.unit_of_works == []

    mapping_factory = FakeUnitOfWorkFactory(
        _database_with(_stored_document(context, status=IngestionStatus.CHUNKING))
    )
    with pytest.raises(ValueError, match="pinned profile"):
        await services.persist_chunked(
            mapping_factory,
            context,
            document,
            chunks,
            chunker_behavior_version="chunker-v7",
            profile_fingerprint="e" * 64,
            clock=_clock,
        )
    assert mapping_factory.unit_of_works == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_stage", "initial_status"),
    [
        (FailedStage.PARSE, IngestionStatus.PARSING),
        (FailedStage.CHUNK, IngestionStatus.CHUNKING),
    ],
)
async def test_record_failure_is_explicit_and_preserves_current_and_candidate(
    tmp_path: Path,
    failed_stage: FailedStage,
    initial_status: IngestionStatus,
) -> None:
    services = _services()
    context = _context(tmp_path)
    existing = _stored_document(
        context,
        status=initial_status,
        current=True,
    )
    factory = FakeUnitOfWorkFactory(_database_with(existing))
    expected_current = _current_triple(context)
    expected_building = (
        context.revision_id,
        context.content_hash,
        context.pipeline_fingerprint,
    )

    failed = await services.record_failure(
        factory,
        context,
        failed_stage,
        clock=_clock,
    )

    assert failed.ingestion_status is IngestionStatus.FAILED
    assert failed.failed_stage is failed_stage
    assert (
        failed.current_revision_id,
        failed.current_content_hash,
        failed.current_pipeline_fingerprint,
    ) == expected_current
    assert (
        failed.building_revision_id,
        failed.building_content_hash,
        failed.building_pipeline_fingerprint,
    ) == expected_building
    assert factory.commit_count == 1
    assert factory.rollback_count == 0


@pytest.mark.asyncio
async def test_record_failure_rejects_wrong_stage_origin_and_non_stage3_stage(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path)
    cases = (
        (FailedStage.PARSE, IngestionStatus.CHUNKING),
        (FailedStage.CHUNK, IngestionStatus.PARSING),
        (FailedStage.EMBEDDING, IngestionStatus.CHUNKING),
    )
    for failed_stage, status in cases:
        existing = _stored_document(context, status=status)
        factory = FakeUnitOfWorkFactory(_database_with(existing))
        with pytest.raises(InvariantViolationError):
            await services.record_failure(
                factory,
                context,
                failed_stage,
                clock=_clock,
            )
        assert _stored(factory, context) == existing
        assert factory.commit_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("has_current", [False, True], ids=["new", "current-r1"])
async def test_discard_deletes_only_candidate_outputs_and_restores_approved_status(
    tmp_path: Path,
    has_current: bool,
) -> None:
    services = _services()
    context = _context(tmp_path)
    document = _document(context)
    profile = ChunkingProfile()
    chunks = build_chunks(document, profile)
    existing = _stored_document(
        context,
        status=IngestionStatus.FAILED,
        current=has_current,
        failed_stage=FailedStage.CHUNK,
        building_document=document,
    )
    original_envelope = load_document_parse_metadata_envelope(existing)
    database = _database_with(existing)
    candidate_scope = (context.document_id, context.revision_id)
    other_scope = (context.document_id, "rev_" + "9" * 64)
    database.elements[candidate_scope] = map_elements_to_stored(
        document.elements, created_at=NOW
    )
    database.chunks[candidate_scope] = map_chunks_to_stored(
        chunks,
        chunker_behavior_version="chunker-v7",
        profile_fingerprint=profile.profile_fingerprint,
        created_at=NOW,
    )
    database.elements[other_scope] = ()
    database.chunks[other_scope] = ()
    database.query_cache_invalidation_required = True
    factory = FakeUnitOfWorkFactory(database)
    expected_current = (
        existing.current_revision_id,
        existing.current_content_hash,
        existing.current_pipeline_fingerprint,
    )

    restored = await services.discard_candidate(factory, context, clock=_clock)

    assert context.document_id in factory.database.documents
    assert candidate_scope not in factory.database.elements
    assert candidate_scope not in factory.database.chunks
    assert other_scope in factory.database.elements
    assert other_scope in factory.database.chunks
    assert restored.building_revision_id is None
    assert restored.building_content_hash is None
    assert restored.building_pipeline_fingerprint is None
    assert restored.failed_stage is None
    assert restored.ingestion_status is (
        IngestionStatus.READY if has_current else IngestionStatus.RECEIVED
    )
    assert (
        restored.current_revision_id,
        restored.current_content_hash,
        restored.current_pipeline_fingerprint,
    ) == expected_current
    restored_envelope = load_document_parse_metadata_envelope(restored)
    assert restored_envelope.current == original_envelope.current
    assert restored_envelope.building is None
    assert factory.database.query_cache_invalidation_required is True
    assert factory.runtime_metadata_accesses == 0
    assert factory.commit_count == 1


@pytest.mark.asyncio
async def test_unexpected_repository_exception_rolls_back_and_propagates_unchanged(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path)
    document = _document(context, "Original output.")
    retry = _document(context, "Replacement output.")
    existing = _stored_document(context, status=IngestionStatus.PARSING)
    database = _database_with(existing)
    scope = (context.document_id, context.revision_id)
    original_elements = map_elements_to_stored(document.elements, created_at=CREATED_AT)
    database.elements[scope] = original_elements
    factory = FakeUnitOfWorkFactory(database)
    error = RuntimeError("injected repository failure")
    factory.fail_after("documents.put", error)

    with pytest.raises(RuntimeError) as exc_info:
        await services.persist_parsed(
            factory,
            context,
            retry,
            clock=_clock,
        )

    assert exc_info.value is error
    assert factory.database.elements[scope] == original_elements
    assert _stored(factory, context) == existing
    assert factory.commit_count == 0
    assert factory.rollback_count == 1
    assert all(
        write.ingestion_status is not IngestionStatus.FAILED
        for write in factory.document_writes
    )


@pytest.mark.asyncio
async def test_cancellation_rolls_back_chunk_set_and_propagates_unchanged(
    tmp_path: Path,
) -> None:
    services = _services()
    context = _context(tmp_path)
    document = _document(context, "Canonical source paragraph. " * 8)
    old_profile = ChunkingProfile(
        parent_max_chars=100,
        child_max_chars=30,
        table_slice_max_chars=100,
    )
    retry_profile = ChunkingProfile(
        parent_max_chars=70,
        child_max_chars=18,
        table_slice_max_chars=70,
    )
    old_chunks = build_chunks(document, old_profile)
    retry_chunks = build_chunks(document, retry_profile)
    existing = _stored_document(
        context,
        status=IngestionStatus.CHUNKING,
        building_document=document,
    )
    database = _database_with(existing)
    scope = (context.document_id, context.revision_id)
    database.elements[scope] = map_elements_to_stored(
        document.elements,
        created_at=CREATED_AT,
    )
    original_chunks = map_chunks_to_stored(
        old_chunks,
        chunker_behavior_version="chunker-old",
        profile_fingerprint=old_profile.profile_fingerprint,
        created_at=CREATED_AT,
    )
    database.chunks[scope] = original_chunks
    factory = FakeUnitOfWorkFactory(database)
    cancellation = CancelledError()
    factory.fail_after("documents.put", cancellation)

    with pytest.raises(CancelledError) as exc_info:
        await services.persist_chunked(
            factory,
            context,
            document,
            retry_chunks,
            chunker_behavior_version="chunker-retry",
            profile_fingerprint=retry_profile.profile_fingerprint,
            clock=_clock,
        )

    assert exc_info.value is cancellation
    assert factory.database.chunks[scope] == original_chunks
    assert _stored(factory, context) == existing
    assert factory.commit_count == 0
    assert factory.rollback_count == 1
    assert all(
        write.ingestion_status is not IngestionStatus.FAILED
        for write in factory.document_writes
    )


@pytest.mark.asyncio
async def test_interleaved_fake_uow_commits_merge_distinct_documents_and_scopes(
    tmp_path: Path,
) -> None:
    first_context = _context(
        tmp_path / "first",
        relative_path="first.txt",
        content=b"first source",
    )
    second_context = _context(
        tmp_path / "second",
        relative_path="second.txt",
        content=b"second source",
    )
    first_document = _document(first_context, "First canonical element.")
    second_document = _document(second_context, "Second canonical element.")
    profile = ChunkingProfile()
    first_chunks = build_chunks(first_document, profile)
    second_chunks = build_chunks(second_document, profile)
    factory = FakeUnitOfWorkFactory(
        FakeDatabase(query_cache_invalidation_required=True)
    )
    first_uow = factory()
    second_uow = factory()

    async with first_uow as first_transaction, second_uow as second_transaction:
        await first_transaction.documents.put(
            _stored_document(
                first_context,
                status=IngestionStatus.PARSED,
                building_document=first_document,
            )
        )
        await first_transaction.elements.replace_revision_set(
            first_context.document_id,
            first_context.revision_id,
            map_elements_to_stored(first_document.elements, created_at=CREATED_AT),
        )
        await first_transaction.chunks.replace_revision_set(
            first_context.document_id,
            first_context.revision_id,
            map_chunks_to_stored(
                first_chunks,
                chunker_behavior_version="chunker-interleaved-v1",
                profile_fingerprint=profile.profile_fingerprint,
                created_at=CREATED_AT,
            ),
        )
        await second_transaction.documents.put(
            _stored_document(
                second_context,
                status=IngestionStatus.PARSED,
                building_document=second_document,
            )
        )
        await second_transaction.elements.replace_revision_set(
            second_context.document_id,
            second_context.revision_id,
            map_elements_to_stored(second_document.elements, created_at=CREATED_AT),
        )
        await second_transaction.chunks.replace_revision_set(
            second_context.document_id,
            second_context.revision_id,
            map_chunks_to_stored(
                second_chunks,
                chunker_behavior_version="chunker-interleaved-v1",
                profile_fingerprint=profile.profile_fingerprint,
                created_at=CREATED_AT,
            ),
        )
        await first_transaction.commit()
        await second_transaction.commit()

    expected_scopes = {
        (first_context.document_id, first_context.revision_id),
        (second_context.document_id, second_context.revision_id),
    }
    assert set(factory.database.documents) == {
        first_context.document_id,
        second_context.document_id,
    }
    assert set(factory.database.elements) == expected_scopes
    assert set(factory.database.chunks) == expected_scopes
    assert factory.database.query_cache_invalidation_required is True
    assert factory.commit_count == 2
    assert factory.rollback_count == 0
