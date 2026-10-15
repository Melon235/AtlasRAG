"""Controlled source-mutation orchestration for candidate parsing."""

from __future__ import annotations

import importlib
from asyncio import CancelledError
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

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
    SourceFile,
    derive_revision_id,
)
from atlasrag.repositories.postgres.records import (
    IngestionStatus,
    JsonObject,
    StoredDocument,
)
from atlasrag.services.ingestion.identity import build_candidate_context, build_element
from atlasrag.services.ingestion.mapping import (
    DocumentParseMetadataEnvelopeV1,
    build_document_parse_metadata_slot,
    empty_document_parse_metadata_envelope,
    load_document_parse_metadata_envelope,
    replace_document_parse_metadata_slot,
    serialize_document_parse_metadata_envelope,
)
from stage3.fakes import FakeDatabase, FakeUnitOfWorkFactory

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
CREATED_AT = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
PIPELINE_FINGERPRINT = "f" * 64
CURRENT_CONTENT_HASH = "a" * 64
CURRENT_PIPELINE_FINGERPRINT = "b" * 64
ParserCallback = Callable[
    [SourceFile, CandidateBuildContext], Awaitable[DocumentContainer]
]


def _serialize_envelope(envelope: DocumentParseMetadataEnvelopeV1) -> JsonObject:
    return cast(JsonObject, serialize_document_parse_metadata_envelope(envelope))


def _module(name: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"{name} is not implemented", pytrace=False)


def _clock() -> datetime:
    return NOW


def _context(root: Path) -> CandidateBuildContext:
    source_path = root / "manual.txt"
    source_path.write_bytes(b"original source bytes")
    source = SourceFile.from_knowledge_directory(root, "manual.txt")
    return build_candidate_context(source, PIPELINE_FINGERPRINT)


def _document(
    context: CandidateBuildContext,
    *,
    revision_id: str | None = None,
    title: str = "Manual",
) -> DocumentContainer:
    canonical_revision_id = revision_id or context.revision_id
    element = build_element(
        document_id=context.document_id,
        revision_id=canonical_revision_id,
        element_type=ElementType.PARAGRAPH,
        order=0,
        content="Canonical output read from the original source.",
        section_path=("Manual",),
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
            parser_version="1",
        ),
        elements=(element,),
    )


def _current_document(context: CandidateBuildContext) -> StoredDocument:
    current_revision = derive_revision_id(
        context.document_id,
        CURRENT_CONTENT_HASH,
        CURRENT_PIPELINE_FINGERPRINT,
    )
    current_metadata = replace_document_parse_metadata_slot(
        empty_document_parse_metadata_envelope(),
        slot_name="current",
        slot=build_document_parse_metadata_slot(
            _document(
                context,
                revision_id=current_revision,
                title="Published manual",
            )
        ),
    )
    return StoredDocument(
        document_id=context.document_id,
        file_name=context.source.file_name,
        relative_source_path=context.source.relative_source_path,
        source_type=context.source.source_type,
        observed_content_hash=CURRENT_CONTENT_HASH,
        current_revision_id=current_revision,
        current_content_hash=CURRENT_CONTENT_HASH,
        current_pipeline_fingerprint=CURRENT_PIPELINE_FINGERPRINT,
        ingestion_status=IngestionStatus.READY,
        parse_metadata=_serialize_envelope(current_metadata),
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("has_current", [False, True], ids=["new", "current-r1"])
async def test_source_mutation_discards_candidate_without_persist_or_failure(
    tmp_path: Path,
    has_current: bool,
) -> None:
    persistence = _module("atlasrag.services.ingestion.persistence")
    candidate = _module("atlasrag.services.ingestion.candidate")
    context = _context(tmp_path)
    parsed = _document(context)
    database = FakeDatabase()
    if has_current:
        database.documents[context.document_id] = _current_document(context)
    factory = FakeUnitOfWorkFactory(database)
    expected_current = (
        (
            database.documents[context.document_id].current_revision_id,
            database.documents[context.document_id].current_content_hash,
            database.documents[context.document_id].current_pipeline_fingerprint,
        )
        if has_current
        else (None, None, None)
    )
    expected_current_slot = (
        load_document_parse_metadata_envelope(
            database.documents[context.document_id]
        ).current
        if has_current
        else None
    )

    async def mutating_parser(
        source: SourceFile,
        candidate_context: CandidateBuildContext,
    ) -> DocumentContainer:
        assert source == context.source
        assert candidate_context == context
        source.absolute_path.write_bytes(b"mutated after controlled parse")
        return parsed

    await persistence.prepare_candidate(factory, context, clock=_clock)
    result = await candidate.parse_candidate(
        factory,
        context,
        cast(ParserCallback, mutating_parser),
        clock=_clock,
    )

    stored = factory.database.documents[context.document_id]
    scope = (context.document_id, context.revision_id)
    assert result is None
    assert scope not in factory.database.elements
    assert scope not in factory.database.chunks
    assert "elements.replace_revision_set" not in factory.calls
    assert "chunks.replace_revision_set" not in factory.calls
    assert factory.calls.count("elements.delete_revision") == 1
    assert factory.calls.count("chunks.delete_revision") == 1
    assert stored.ingestion_status is (
        IngestionStatus.READY if has_current else IngestionStatus.RECEIVED
    )
    assert stored.failed_stage is None
    assert stored.building_revision_id is None
    assert (
        stored.current_revision_id,
        stored.current_content_hash,
        stored.current_pipeline_fingerprint,
    ) == expected_current
    stored_envelope = load_document_parse_metadata_envelope(stored)
    assert stored_envelope.current == expected_current_slot
    assert stored_envelope.building is None
    assert all(
        write.ingestion_status is not IngestionStatus.FAILED
        for write in factory.document_writes
    )
    assert factory.runtime_metadata_accesses == 0
    assert [unit.commit_count for unit in factory.unit_of_works] == [1, 1, 1]


@pytest.mark.asyncio
async def test_unchanged_source_is_persisted_only_after_the_guard(
    tmp_path: Path,
) -> None:
    persistence = _module("atlasrag.services.ingestion.persistence")
    candidate = _module("atlasrag.services.ingestion.candidate")
    context = _context(tmp_path)
    parsed = _document(context)
    factory = FakeUnitOfWorkFactory()
    callback_finished = False

    async def parser(
        source: SourceFile,
        candidate_context: CandidateBuildContext,
    ) -> DocumentContainer:
        nonlocal callback_finished
        assert source == context.source
        assert candidate_context == context
        callback_finished = True
        return parsed

    await persistence.prepare_candidate(factory, context, clock=_clock)
    result = await candidate.parse_candidate(
        factory,
        context,
        cast(ParserCallback, parser),
        clock=_clock,
    )

    assert callback_finished
    assert result == parsed
    assert (
        factory.database.documents[context.document_id].ingestion_status
        is IngestionStatus.PARSED
    )
    assert (context.document_id, context.revision_id) in factory.database.elements
    envelope = load_document_parse_metadata_envelope(
        factory.database.documents[context.document_id]
    )
    assert envelope.current is None
    assert envelope.building == build_document_parse_metadata_slot(parsed)
    assert factory.calls.index("elements.replace_revision_set") > factory.calls.index(
        "documents.get_by_id"
    )
    assert [unit.commit_count for unit in factory.unit_of_works] == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True], ids=["exception", "cancelled"])
async def test_unexpected_parser_failure_propagates_without_failed_conversion(
    tmp_path: Path,
    cancelled: bool,
) -> None:
    persistence = _module("atlasrag.services.ingestion.persistence")
    candidate = _module("atlasrag.services.ingestion.candidate")
    context = _context(tmp_path)
    factory = FakeUnitOfWorkFactory()
    error: BaseException = (
        CancelledError() if cancelled else RuntimeError("unexpected parser defect")
    )

    async def broken_parser(
        source: SourceFile,
        candidate_context: CandidateBuildContext,
    ) -> DocumentContainer:
        assert source == context.source
        assert candidate_context == context
        raise error

    await persistence.prepare_candidate(factory, context, clock=_clock)
    with pytest.raises(type(error)) as exc_info:
        await candidate.parse_candidate(
            factory,
            context,
            cast(ParserCallback, broken_parser),
            clock=_clock,
        )

    assert exc_info.value is error
    stored = factory.database.documents[context.document_id]
    assert stored.ingestion_status is IngestionStatus.PARSING
    assert stored.failed_stage is None
    assert stored.building_revision_id == context.revision_id
    assert "elements.replace_revision_set" not in factory.calls
    assert "elements.delete_revision" not in factory.calls
    assert all(
        write.ingestion_status is not IngestionStatus.FAILED
        for write in factory.document_writes
    )
    assert [unit.commit_count for unit in factory.unit_of_works] == [1, 1]
