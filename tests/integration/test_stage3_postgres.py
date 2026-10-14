"""Real PostgreSQL proofs for the Stage 3 candidate transaction boundary."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import socket
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Literal, Self, cast

import pytest
from psycopg import OperationalError, sql

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
from atlasrag.repositories.postgres.pool import PostgresPool
from atlasrag.repositories.postgres.records import (
    FailedStage,
    IngestionStatus,
    JsonObject,
    StoredChunk,
    StoredDocument,
    StoredElement,
)
from atlasrag.repositories.postgres.uow import PostgresUnitOfWork
from atlasrag.services.ingestion.candidate import ParserCallback, parse_candidate
from atlasrag.services.ingestion.chunking import ChunkingProfile, build_chunks
from atlasrag.services.ingestion.identity import build_candidate_context, build_element
from atlasrag.services.ingestion.mapping import (
    DocumentParseMetadataEnvelopeV1,
    DocumentParseMetadataSlotV1,
    build_document_parse_metadata_slot,
    empty_document_parse_metadata_envelope,
    load_document_parse_metadata_envelope,
    replace_document_parse_metadata_slot,
    serialize_document_parse_metadata_envelope,
)
from atlasrag.services.ingestion.persistence import (
    CandidateChunkRepository,
    CandidateDocumentRepository,
    CandidateElementRepository,
    CandidateUnitOfWork,
    discard_candidate,
    mark_chunking,
    mark_parsing,
    persist_chunked,
    persist_parsed,
    prepare_candidate,
    record_failure,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
CREATED_AT = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
PIPELINE_FINGERPRINT = "f" * 64
CURRENT_CONTENT_HASH = "a" * 64
CURRENT_PIPELINE_FINGERPRINT = "b" * 64
UowFactory = Callable[[], PostgresUnitOfWork]
CurrentSnapshot = tuple[
    tuple[str | None, str | None, str | None],
    DocumentParseMetadataSlotV1 | None,
    bool,
]
WAIT_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "wait_for_postgres.py"
)
INTEGRATION_FIXTURE_PATH = Path(__file__).with_name("conftest.py")


def _load_wait_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "atlasrag_stage3_postgres_readiness",
        WAIT_SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_integration_fixture_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "atlasrag_stage3_integration_fixtures",
        INTEGRATION_FIXTURE_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _InjectedWriteError(RuntimeError):
    """Controlled test-only error after a real partial repository write."""


class _FailAfterFirstElementRepository:
    def __init__(self, delegate: CandidateElementRepository) -> None:
        self._delegate = delegate

    async def replace_revision_set(
        self,
        document_id: str,
        revision_id: str,
        elements: Iterable[StoredElement],
    ) -> None:
        records = tuple(elements)
        assert len(records) >= 2
        await self._delegate.replace_revision_set(
            document_id,
            revision_id,
            records[:1],
        )
        raise _InjectedWriteError("controlled element repository failure")

    async def delete_revision(self, document_id: str, revision_id: str) -> int:
        return await self._delegate.delete_revision(document_id, revision_id)

    async def list_for_revision(
        self,
        document_id: str,
        revision_id: str,
    ) -> tuple[StoredElement, ...]:
        return await self._delegate.list_for_revision(document_id, revision_id)


class _FailAfterFirstChunkRepository:
    def __init__(self, delegate: CandidateChunkRepository) -> None:
        self._delegate = delegate

    async def replace_revision_set(
        self,
        document_id: str,
        revision_id: str,
        chunks: Iterable[StoredChunk],
    ) -> None:
        records = tuple(chunks)
        assert len(records) >= 2
        await self._delegate.replace_revision_set(
            document_id,
            revision_id,
            records[:1],
        )
        raise _InjectedWriteError("controlled chunk repository failure")

    async def delete_revision(self, document_id: str, revision_id: str) -> int:
        return await self._delegate.delete_revision(document_id, revision_id)


class _PartialWriteFailureUnitOfWork:
    """Delegate to the real UoW but fault one exact-set repository call."""

    def __init__(
        self,
        pool: PostgresPool,
        target: Literal["elements", "chunks"],
    ) -> None:
        self._inner = PostgresUnitOfWork(pool)
        self._target = target
        self._elements: CandidateElementRepository | None = None
        self._chunks: CandidateChunkRepository | None = None

    @property
    def documents(self) -> CandidateDocumentRepository:
        return self._inner.documents

    @property
    def elements(self) -> CandidateElementRepository:
        assert self._elements is not None
        return self._elements

    @property
    def chunks(self) -> CandidateChunkRepository:
        assert self._chunks is not None
        return self._chunks

    async def __aenter__(self) -> Self:
        await self._inner.__aenter__()
        element_repository: CandidateElementRepository = self._inner.elements
        chunk_repository: CandidateChunkRepository = self._inner.chunks
        self._elements = (
            _FailAfterFirstElementRepository(element_repository)
            if self._target == "elements"
            else element_repository
        )
        self._chunks = (
            _FailAfterFirstChunkRepository(chunk_repository)
            if self._target == "chunks"
            else chunk_repository
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._inner.__aexit__(exc_type, exc_value, traceback)

    async def commit(self) -> None:
        await self._inner.commit()


class _PartialWriteFailureFactory:
    def __init__(
        self,
        pool: PostgresPool,
        target: Literal["elements", "chunks"],
    ) -> None:
        self._pool = pool
        self._target = target

    def __call__(self) -> CandidateUnitOfWork:
        return _PartialWriteFailureUnitOfWork(self._pool, self._target)


def _clock() -> datetime:
    return NOW


def _context(
    root: Path,
    *,
    relative_path: str = "guides/postgres-candidate.txt",
    content: bytes = b"candidate R2 source bytes",
) -> CandidateBuildContext:
    source_path = root / relative_path
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(content)
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
        elements=tuple(
            build_element(
                document_id=context.document_id,
                revision_id=canonical_revision_id,
                element_type=ElementType.PARAGRAPH,
                order=order,
                content=content,
                section_path=("Candidate",),
            )
            for order, content in enumerate(values)
        ),
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


def _serialize_envelope(envelope: DocumentParseMetadataEnvelopeV1) -> JsonObject:
    return cast(JsonObject, serialize_document_parse_metadata_envelope(envelope))


def _published_document(context: CandidateBuildContext) -> StoredDocument:
    current_revision, current_hash, current_pipeline = _current_triple(context)
    current_canonical = _document(
        context,
        "Published current revision.",
        revision_id=current_revision,
        title="Published",
        parser_version="published-v1",
    )
    envelope = replace_document_parse_metadata_slot(
        empty_document_parse_metadata_envelope(),
        slot_name="current",
        slot=build_document_parse_metadata_slot(current_canonical),
    )
    return StoredDocument(
        document_id=context.document_id,
        file_name=context.source.file_name,
        relative_source_path=context.source.relative_source_path,
        source_type=context.source.source_type,
        observed_content_hash=current_hash,
        current_revision_id=current_revision,
        current_content_hash=current_hash,
        current_pipeline_fingerprint=current_pipeline,
        ingestion_status=IngestionStatus.READY,
        parse_metadata=_serialize_envelope(envelope),
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


async def _load_current_snapshot(
    factory: UowFactory,
    document_id: str,
) -> CurrentSnapshot:
    async with factory() as unit_of_work:
        document = await unit_of_work.documents.get_by_id(document_id)
        runtime_metadata = await unit_of_work.runtime_metadata.get()
    assert document is not None
    return (
        (
            document.current_revision_id,
            document.current_content_hash,
            document.current_pipeline_fingerprint,
        ),
        load_document_parse_metadata_envelope(document).current,
        runtime_metadata.query_cache_invalidation_required,
    )


async def _seed_published_snapshot(
    factory: UowFactory,
    context: CandidateBuildContext,
) -> CurrentSnapshot:
    async with factory() as unit_of_work:
        await unit_of_work.documents.put(_published_document(context))
        await unit_of_work.runtime_metadata.set_query_cache_invalidation_required(True)
        await unit_of_work.commit()
    return await _load_current_snapshot(factory, context.document_id)


async def _load_document(
    factory: UowFactory,
    document_id: str,
) -> StoredDocument:
    async with factory() as unit_of_work:
        document = await unit_of_work.documents.get_by_id(document_id)
    assert document is not None
    return document


async def _load_revision_sets(
    factory: UowFactory,
    context: CandidateBuildContext,
) -> tuple[tuple[StoredElement, ...], tuple[StoredChunk, ...]]:
    async with factory() as unit_of_work:
        elements = await unit_of_work.elements.list_for_revision(
            context.document_id,
            context.revision_id,
        )
        chunks = await unit_of_work.chunks.list_for_revision(
            context.document_id,
            context.revision_id,
        )
    return elements, chunks


async def test_prepare_and_parsing_preserve_current_r1_and_install_building_r2(
    tmp_path: Path,
    postgres_uow_factory: UowFactory,
) -> None:
    context = _context(tmp_path)
    current_snapshot = await _seed_published_snapshot(postgres_uow_factory, context)

    await prepare_candidate(postgres_uow_factory, context, clock=_clock)
    await mark_parsing(postgres_uow_factory, context, clock=_clock)

    stored = await _load_document(postgres_uow_factory, context.document_id)
    assert stored.ingestion_status is IngestionStatus.PARSING
    assert (
        stored.current_revision_id,
        stored.current_content_hash,
        stored.current_pipeline_fingerprint,
    ) == _current_triple(context)
    assert (
        stored.building_revision_id,
        stored.building_content_hash,
        stored.building_pipeline_fingerprint,
    ) == (
        context.revision_id,
        context.content_hash,
        context.pipeline_fingerprint,
    )
    envelope = load_document_parse_metadata_envelope(stored)
    assert (
        await _load_current_snapshot(
            postgres_uow_factory,
            context.document_id,
        )
        == current_snapshot
    )
    assert envelope.building is None


async def test_parsed_exact_set_retry_and_partial_write_failure_are_atomic(
    tmp_path: Path,
    postgres_pool: PostgresPool,
    postgres_uow_factory: UowFactory,
) -> None:
    context = _context(tmp_path)
    initial = _document(
        context,
        "Original first paragraph.",
        "Original second paragraph.",
    )
    replacement = _document(
        context,
        "Replacement first paragraph.",
        "Replacement second paragraph.",
        parser_version="2",
    )
    current_snapshot = await _seed_published_snapshot(postgres_uow_factory, context)
    await prepare_candidate(postgres_uow_factory, context, clock=_clock)
    await mark_parsing(postgres_uow_factory, context, clock=_clock)
    await persist_parsed(postgres_uow_factory, context, initial, clock=_clock)
    before_elements, _ = await _load_revision_sets(postgres_uow_factory, context)

    with pytest.raises(_InjectedWriteError):
        await persist_parsed(
            _PartialWriteFailureFactory(postgres_pool, "elements"),
            context,
            replacement,
            clock=_clock,
        )

    rolled_back = await _load_document(postgres_uow_factory, context.document_id)
    rolled_back_elements, _ = await _load_revision_sets(
        postgres_uow_factory,
        context,
    )
    assert rolled_back.ingestion_status is IngestionStatus.PARSED
    assert rolled_back_elements == before_elements
    assert load_document_parse_metadata_envelope(rolled_back).building == (
        build_document_parse_metadata_slot(initial)
    )
    assert (
        await _load_current_snapshot(
            postgres_uow_factory,
            context.document_id,
        )
        == current_snapshot
    )

    await persist_parsed(
        postgres_uow_factory,
        context,
        replacement,
        clock=_clock,
    )
    retried = await _load_document(postgres_uow_factory, context.document_id)
    retried_elements, _ = await _load_revision_sets(postgres_uow_factory, context)
    assert retried.ingestion_status is IngestionStatus.PARSED
    assert [element.content for element in retried_elements] == [
        element.content for element in replacement.elements
    ]
    assert {element.element_id for element in retried_elements}.isdisjoint(
        {element.element_id for element in before_elements}
    )
    assert load_document_parse_metadata_envelope(retried).building == (
        build_document_parse_metadata_slot(replacement)
    )
    assert (
        await _load_current_snapshot(
            postgres_uow_factory,
            context.document_id,
        )
        == current_snapshot
    )


async def test_chunked_exact_set_retry_and_partial_write_failure_are_atomic(
    tmp_path: Path,
    postgres_pool: PostgresPool,
    postgres_uow_factory: UowFactory,
) -> None:
    context = _context(tmp_path)
    document = _document(
        context,
        "First paragraph is long enough to produce deterministic child records.",
        "Second paragraph is also long enough for another deterministic child.",
    )
    initial_profile = ChunkingProfile(
        parent_max_chars=200,
        child_max_chars=40,
        table_slice_max_chars=200,
    )
    replacement_profile = ChunkingProfile(
        parent_max_chars=75,
        child_max_chars=25,
        table_slice_max_chars=75,
    )
    initial_chunks = build_chunks(document, initial_profile)
    replacement_chunks = build_chunks(document, replacement_profile)
    assert len(initial_chunks) >= 2
    assert len(replacement_chunks) >= 2
    assert {chunk.chunk_id for chunk in initial_chunks} != {
        chunk.chunk_id for chunk in replacement_chunks
    }

    current_snapshot = await _seed_published_snapshot(postgres_uow_factory, context)
    await prepare_candidate(postgres_uow_factory, context, clock=_clock)
    await mark_parsing(postgres_uow_factory, context, clock=_clock)
    await persist_parsed(postgres_uow_factory, context, document, clock=_clock)
    await mark_chunking(postgres_uow_factory, context, clock=_clock)
    await persist_chunked(
        postgres_uow_factory,
        context,
        document,
        initial_chunks,
        chunker_behavior_version="chunker-integration-initial-v1",
        profile_fingerprint=initial_profile.profile_fingerprint,
        clock=_clock,
    )
    _, before_chunks = await _load_revision_sets(postgres_uow_factory, context)

    with pytest.raises(_InjectedWriteError):
        await persist_chunked(
            _PartialWriteFailureFactory(postgres_pool, "chunks"),
            context,
            document,
            replacement_chunks,
            chunker_behavior_version="chunker-integration-replacement-v1",
            profile_fingerprint=replacement_profile.profile_fingerprint,
            clock=_clock,
        )

    rolled_back = await _load_document(postgres_uow_factory, context.document_id)
    _, rolled_back_chunks = await _load_revision_sets(
        postgres_uow_factory,
        context,
    )
    assert rolled_back.ingestion_status is IngestionStatus.CHUNKED
    assert rolled_back_chunks == before_chunks
    assert (
        rolled_back.current_revision_id,
        rolled_back.current_content_hash,
        rolled_back.current_pipeline_fingerprint,
    ) == _current_triple(context)
    assert (
        await _load_current_snapshot(
            postgres_uow_factory,
            context.document_id,
        )
        == current_snapshot
    )

    await persist_chunked(
        postgres_uow_factory,
        context,
        document,
        replacement_chunks,
        chunker_behavior_version="chunker-integration-replacement-v1",
        profile_fingerprint=replacement_profile.profile_fingerprint,
        clock=_clock,
    )
    retried = await _load_document(postgres_uow_factory, context.document_id)
    _, retried_chunks = await _load_revision_sets(postgres_uow_factory, context)
    assert retried.ingestion_status is IngestionStatus.CHUNKED
    assert (
        retried.current_revision_id,
        retried.current_content_hash,
        retried.current_pipeline_fingerprint,
    ) == _current_triple(context)
    assert (
        await _load_current_snapshot(
            postgres_uow_factory,
            context.document_id,
        )
        == current_snapshot
    )
    assert {chunk.chunk_id for chunk in retried_chunks} == {
        chunk.chunk_id for chunk in replacement_chunks
    }
    assert {chunk.chunk_id for chunk in retried_chunks}.isdisjoint(
        {chunk.chunk_id for chunk in before_chunks}
    )


@pytest.mark.parametrize("has_current", [False, True], ids=["new", "current-r1"])
async def test_source_mutation_discards_candidate_and_retains_empty_identity(
    tmp_path: Path,
    postgres_uow_factory: UowFactory,
    has_current: bool,
) -> None:
    context = _context(tmp_path)
    current_snapshot: CurrentSnapshot | None = None
    if has_current:
        current_snapshot = await _seed_published_snapshot(
            postgres_uow_factory,
            context,
        )
    await prepare_candidate(postgres_uow_factory, context, clock=_clock)
    parsed = _document(context)

    async def mutating_parser(
        source: SourceFile,
        candidate_context: CandidateBuildContext,
    ) -> DocumentContainer:
        assert source == context.source
        assert candidate_context == context
        source.absolute_path.write_bytes(b"source changed during parsing")
        return parsed

    result = await parse_candidate(
        postgres_uow_factory,
        context,
        cast(ParserCallback, mutating_parser),
        clock=_clock,
    )

    assert result is None
    stored = await _load_document(postgres_uow_factory, context.document_id)
    elements, chunks = await _load_revision_sets(postgres_uow_factory, context)
    assert elements == ()
    assert chunks == ()
    assert stored.building_revision_id is None
    assert stored.building_content_hash is None
    assert stored.building_pipeline_fingerprint is None
    assert stored.failed_stage is None
    assert stored.ingestion_status is (
        IngestionStatus.READY if has_current else IngestionStatus.RECEIVED
    )
    envelope = load_document_parse_metadata_envelope(stored)
    assert envelope.building is None
    if has_current:
        assert stored.current_revision_id == _current_triple(context)[0]
        assert envelope.current is not None
        assert current_snapshot is not None
        assert (
            await _load_current_snapshot(
                postgres_uow_factory,
                context.document_id,
            )
            == current_snapshot
        )
    else:
        assert stored.current_revision_id is None
        assert envelope.current is None


@pytest.mark.parametrize(
    ("failed_stage", "expected_origin"),
    [
        (FailedStage.PARSE, IngestionStatus.PARSING),
        (FailedStage.CHUNK, IngestionStatus.CHUNKING),
    ],
)
async def test_parse_and_chunk_business_failures_preserve_candidate_identity(
    tmp_path: Path,
    postgres_uow_factory: UowFactory,
    failed_stage: FailedStage,
    expected_origin: IngestionStatus,
) -> None:
    context = _context(
        tmp_path,
        relative_path=f"guides/failure-{failed_stage.value.casefold()}.txt",
    )
    current_snapshot = await _seed_published_snapshot(postgres_uow_factory, context)
    await prepare_candidate(postgres_uow_factory, context, clock=_clock)
    await mark_parsing(postgres_uow_factory, context, clock=_clock)
    if expected_origin is IngestionStatus.CHUNKING:
        await persist_parsed(
            postgres_uow_factory,
            context,
            _document(context),
            clock=_clock,
        )
        await mark_chunking(postgres_uow_factory, context, clock=_clock)

    await record_failure(
        postgres_uow_factory,
        context,
        failed_stage,
        clock=_clock,
    )

    stored = await _load_document(postgres_uow_factory, context.document_id)
    assert stored.ingestion_status is IngestionStatus.FAILED
    assert stored.failed_stage is failed_stage
    assert stored.building_revision_id == context.revision_id
    assert stored.building_content_hash == context.content_hash
    assert stored.building_pipeline_fingerprint == context.pipeline_fingerprint
    assert (
        await _load_current_snapshot(
            postgres_uow_factory,
            context.document_id,
        )
        == current_snapshot
    )


async def test_explicit_discard_removes_real_candidate_rows_atomically(
    tmp_path: Path,
    postgres_uow_factory: UowFactory,
) -> None:
    context = _context(tmp_path)
    document = _document(context, "Candidate output to discard.")
    profile = ChunkingProfile()
    chunks = build_chunks(document, profile)
    await prepare_candidate(postgres_uow_factory, context, clock=_clock)
    await mark_parsing(postgres_uow_factory, context, clock=_clock)
    await persist_parsed(postgres_uow_factory, context, document, clock=_clock)
    await mark_chunking(postgres_uow_factory, context, clock=_clock)
    await persist_chunked(
        postgres_uow_factory,
        context,
        document,
        chunks,
        chunker_behavior_version="chunker-integration-discard-v1",
        profile_fingerprint=profile.profile_fingerprint,
        clock=_clock,
    )

    await discard_candidate(postgres_uow_factory, context, clock=_clock)

    stored = await _load_document(postgres_uow_factory, context.document_id)
    elements, stored_chunks = await _load_revision_sets(
        postgres_uow_factory,
        context,
    )
    assert elements == ()
    assert stored_chunks == ()
    assert stored.ingestion_status is IngestionStatus.RECEIVED
    assert stored.current_revision_id is None
    assert stored.building_revision_id is None
    assert load_document_parse_metadata_envelope(stored) == (
        empty_document_parse_metadata_envelope()
    )


async def test_postgres_wait_script_needs_no_other_service(
    postgres_pool: PostgresPool,
) -> None:
    del postgres_pool
    repository_root = Path(__file__).resolve().parents[2]
    script = repository_root / "scripts" / "wait_for_postgres.py"
    environment = dict(os.environ)
    for unrelated_name in (
        "ATLASRAG_REDIS_URL",
        "ATLASRAG_TEST_REDIS_URL",
        "ATLASRAG_MILVUS_URI",
        "ATLASRAG_SEARXNG_URL",
    ):
        environment.pop(unrelated_name, None)
    environment.update(
        {
            "ATLASRAG_POSTGRES_WAIT_TIMEOUT_SECONDS": "5",
            "ATLASRAG_POSTGRES_PROBE_TIMEOUT_SECONDS": "1",
            "ATLASRAG_POSTGRES_POLL_SECONDS": "0.05",
        }
    )

    completed = await asyncio.to_thread(
        subprocess.run,
        (sys.executable, str(script)),
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "PostgreSQL ready\n"
    assert completed.stderr == ""


async def test_postgres_wait_enforces_total_deadline_on_slow_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness = _load_wait_module()
    settled: list[bool] = []
    probe_tasks: list[asyncio.Task[object]] = []

    async def slow_success(config: object) -> None:
        del config
        probe_task = asyncio.current_task()
        assert probe_task is not None
        probe_tasks.append(probe_task)
        try:
            await asyncio.sleep(0.20)
        finally:
            settled.append(True)

    monkeypatch.setattr(readiness, "_probe", slow_success)
    config = readiness.PostgresWaitConfig(
        dsn="postgresql://ignored@127.0.0.1:5432/ignored",
        wait_timeout_seconds=0.05,
        probe_timeout_seconds=1.0,
        poll_seconds=0.01,
    )

    ready = await readiness.wait_for_postgres(config)

    assert ready is False
    assert settled == [True]
    assert len(probe_tasks) == 1
    assert probe_tasks[0].done()


async def test_postgres_wait_subprocess_times_out_without_credential_leak() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    script = repository_root / "scripts" / "wait_for_postgres.py"
    credential_sentinel = "DO_NOT_LEAK_FAKE_POSTGRES_CREDENTIAL"
    environment = dict(os.environ)
    for unrelated_name in (
        "ATLASRAG_REDIS_URL",
        "ATLASRAG_TEST_REDIS_URL",
        "ATLASRAG_MILVUS_URI",
        "ATLASRAG_SEARXNG_URL",
    ):
        environment.pop(unrelated_name, None)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved_socket:
        reserved_socket.bind(("127.0.0.1", 0))
        unused_port = reserved_socket.getsockname()[1]
        environment.update(
            {
                "ATLASRAG_POSTGRES_DSN": (
                    "postgresql://atlasrag:"
                    f"{credential_sentinel}@127.0.0.1:{unused_port}/atlasrag"
                ),
                "ATLASRAG_POSTGRES_WAIT_TIMEOUT_SECONDS": "0.20",
                "ATLASRAG_POSTGRES_PROBE_TIMEOUT_SECONDS": "5",
                "ATLASRAG_POSTGRES_POLL_SECONDS": "0.01",
            }
        )
        started = time.monotonic()
        completed = await asyncio.to_thread(
            subprocess.run,
            (sys.executable, str(script)),
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        elapsed = time.monotonic() - started

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "PostgreSQL unavailable before timeout\n"
    assert credential_sentinel not in completed.stdout
    assert credential_sentinel not in completed.stderr
    assert elapsed < 3.0


async def test_admin_fixture_redacts_psycopg_errors_without_exception_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = _load_integration_fixture_module()
    credential_sentinel = "DO_NOT_LEAK_FAKE_ADMIN_PASSWORD"

    async def leaking_connect(*_args: object, **_kwargs: object) -> object:
        raise OperationalError(
            "connection failed for "
            f"postgresql://atlasrag:{credential_sentinel}@127.0.0.1/postgres"
        )

    monkeypatch.setattr(
        fixtures.AsyncConnection,
        "connect",
        staticmethod(leaking_connect),
    )

    with pytest.raises(RuntimeError) as captured:
        await fixtures._execute_admin_statement(
            "postgresql://atlasrag:unused@127.0.0.1/atlasrag",
            sql.SQL("SELECT 1"),
        )

    error = captured.value
    rendered = "".join(traceback.format_exception(error))
    assert str(error) == "PostgreSQL integration administrator operation failed"
    assert credential_sentinel not in str(error)
    assert credential_sentinel not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


async def test_admin_fixture_preserves_non_psycopg_programming_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = _load_integration_fixture_module()
    programming_error = ValueError("fixture programming defect")

    async def broken_connect(*_args: object, **_kwargs: object) -> object:
        raise programming_error

    monkeypatch.setattr(
        fixtures.AsyncConnection,
        "connect",
        staticmethod(broken_connect),
    )

    with pytest.raises(ValueError) as captured:
        await fixtures._execute_admin_statement(
            "postgresql://atlasrag:unused@127.0.0.1/atlasrag",
            sql.SQL("SELECT 1"),
        )

    assert captured.value is programming_error
