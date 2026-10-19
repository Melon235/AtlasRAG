"""Independent exact projection verification and invocation-owned publication proof."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncGenerator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from stage3.test_candidate_services import NOW, _document, _stored_document

from atlasrag.domain.errors import (
    CanonicalDataError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    InvariantViolationError,
    ProviderResponseError,
)
from atlasrag.domain.ingestion.source import CandidateBuildContext
from atlasrag.providers.index.projection import MilvusRevisionProjection
from atlasrag.repositories.postgres.records import IngestionStatus
from atlasrag.services.ingestion.chunking import ChunkingProfile, build_chunks
from atlasrag.services.ingestion.mapping import (
    map_chunks_to_stored,
    map_elements_to_stored,
)
from stage4.candidate_fakes import Embedding, Index, IndexFactory, candidate


def service() -> ModuleType:
    try:
        return importlib.import_module("atlasrag.services.indexing.verification")
    except ModuleNotFoundError:
        pytest.fail("T5 independent candidate verification is missing", pytrace=False)


def change_canonical(factory: IndexFactory, context: CandidateBuildContext) -> None:
    document = _document(context, "changed but valid canonical contents")
    profile = ChunkingProfile()
    factory.database.documents[context.document_id] = _stored_document(
        context,
        status=IngestionStatus.INDEXING,
        current=True,
        building_document=document,
    )
    scope = context.document_id, context.revision_id
    factory.database.elements[scope] = map_elements_to_stored(
        document.elements, created_at=NOW
    )
    factory.database.chunks[scope] = map_chunks_to_stored(
        build_chunks(document, profile),
        chunker_behavior_version="chunker-v1",
        profile_fingerprint=profile.profile_fingerprint,
        created_at=NOW,
    )


async def indexed(
    tmp_path: Path,
) -> tuple[Any, IndexFactory, CandidateBuildContext, Embedding, Index]:
    verification = service()
    factory, context, runtime = candidate(tmp_path)
    embedding = Embedding(factory, runtime)
    index = Index(factory, embedding)
    attempt = verification.CandidateVerificationAttempt(
        factory, context, config=runtime, index=index
    )
    await attempt.index(embedding=embedding, clock=lambda: NOW, batch_size=3)
    return attempt, factory, context, embedding, index


@pytest.mark.asyncio
async def test_exact_persisted_projection_verifies_without_embedding_and_consumes_once(
    tmp_path: Path,
) -> None:
    attempt, factory, context, embedding, index = await indexed(tmp_path)
    embedding.error = AssertionError("verification must never embed")
    before = len(embedding.inputs)
    await attempt.verify(batch_size=2)
    assert len(embedding.inputs) == before
    assert index.closed == 2
    async with factory() as unit:
        canonical = await attempt.consume_verified(unit, context)
        assert canonical.context == context
        assert len(canonical.chunks) == 11
    async with factory() as unit:
        with pytest.raises(InvariantViolationError):
            await attempt.consume_verified(unit, context)
    assert factory.commit_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "extra",
        "duplicate",
        "parent",
        "document_id",
        "revision_id",
        "chunk_type",
        "parent_id",
        "file_name",
        "source_type",
        "content",
        "sparse_text",
        "schema",
        "boolean_schema",
        "string_schema",
        "metadata",
        "source_span",
        "row_range",
    ],
)
async def test_correct_ids_or_counts_cannot_hide_projection_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    attempt, factory, context, _, index = await indexed(tmp_path)
    verification = service()
    row = index.rows[0]
    payload = row.model_dump(mode="json")
    if fault == "missing":
        index.rows.pop()
    elif fault == "extra":
        index.rows.append(row.model_copy(update={"chunk_id": "extra"}))
    elif fault == "duplicate":
        index.rows.append(row)
    elif fault == "parent":
        payload.update(chunk_id=row.parent_id, chunk_type="TEXT_PARENT", parent_id=None)
        index.rows[0] = MilvusRevisionProjection.model_validate(payload)
    elif fault in {
        "schema",
        "boolean_schema",
        "string_schema",
        "metadata",
        "source_span",
        "row_range",
    }:
        metadata = payload["structured_metadata"]
        if fault in {"schema", "boolean_schema", "string_schema"}:
            metadata["schema_version"] = {
                "schema": 2,
                "boolean_schema": True,
                "string_schema": "1",
            }[fault]
        elif fault == "metadata":
            metadata["section_path"] = ["wrong"]
        elif fault == "source_span":
            metadata["provenance"]["source_spans"][0]["char_start"] = False
        else:
            metadata["provenance"]["parent_char_end"] = 99
        index.rows[0] = MilvusRevisionProjection.model_validate(payload)
    else:
        payload[fault] = "wrong"
        index.rows[0] = MilvusRevisionProjection.model_validate(payload)

    async def observe_all(
        *args: object, **kwargs: object
    ) -> AsyncGenerator[MilvusRevisionProjection, None]:
        factory.assert_no_transaction()
        try:
            for observed in index.rows:
                yield observed
        finally:
            index.closed += 1

    monkeypatch.setattr(index, "iter_revision_projection", observe_all)
    with pytest.raises(verification.IndexVerificationError):
        await attempt.verify()
    assert index.closed == 2
    async with factory() as unit:
        with pytest.raises(InvariantViolationError):
            await attempt.consume_verified(unit, context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("offline"),
        DependencyTimeoutError(),
        DependencyUnavailableError(),
        RuntimeError("unknown"),
        ProviderResponseError(),
    ],
)
async def test_technical_read_failures_propagate_and_invalidate_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    attempt, factory, context, _, index = await indexed(tmp_path)
    await attempt.verify()

    async def unavailable(
        *args: object, **kwargs: object
    ) -> AsyncGenerator[MilvusRevisionProjection, None]:
        raise error
        yield  # pragma: no cover

    monkeypatch.setattr(index, "iter_revision_projection", unavailable)
    with pytest.raises(type(error)) as caught:
        await attempt.verify()
    assert caught.value is error
    async with factory() as unit:
        with pytest.raises(InvariantViolationError):
            await attempt.consume_verified(unit, context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "when", ["after_index", "during_index", "during_verify", "before_consume"]
)
async def test_valid_canonical_changes_never_establish_a_new_expected_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    attempt, factory, context, embedding, index = await indexed(tmp_path)
    verification = service()
    if when == "during_index":
        original_wait = index.wait_revision_visible

        async def mutate(document_id: str, revision_id: str) -> None:
            await original_wait(document_id, revision_id)
            if index.rows:
                change_canonical(factory, context)

        monkeypatch.setattr(index, "wait_revision_visible", mutate)
        with pytest.raises(verification.IndexVerificationError):
            await attempt.index(embedding=embedding, clock=lambda: NOW)
    elif when == "during_verify":
        original_iter = index.iter_revision_projection

        async def mutate_read(
            *args: Any, **kwargs: Any
        ) -> AsyncGenerator[MilvusRevisionProjection, None]:
            async for row in original_iter(*args, **kwargs):
                yield row
            change_canonical(factory, context)

        monkeypatch.setattr(index, "iter_revision_projection", mutate_read)
        with pytest.raises(verification.IndexVerificationError):
            await attempt.verify()
    else:
        if when == "before_consume":
            await attempt.verify()
        change_canonical(factory, context)
        if when == "after_index":
            with pytest.raises(verification.IndexVerificationError):
                await attempt.verify()
        else:
            async with factory() as unit:
                with pytest.raises(verification.IndexVerificationError):
                    await attempt.consume_verified(unit, context)
    async with factory() as unit:
        with pytest.raises(InvariantViolationError):
            await attempt.consume_verified(unit, context)


@pytest.mark.asyncio
async def test_missing_parent_is_canonical_corruption_not_business_mismatch(
    tmp_path: Path,
) -> None:
    attempt, factory, context, _, _ = await indexed(tmp_path)
    scope = context.document_id, context.revision_id
    factory.database.chunks[scope] = tuple(
        c for c in factory.database.chunks[scope] if c.parent_id is not None
    )
    with pytest.raises(CanonicalDataError):
        await attempt.verify()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "missing",
        "cross_invocation",
        "cross_context",
        "new_index",
        "failed_index",
        "failed_reverify",
        "invalid_reverify",
    ],
)
async def test_missing_stale_and_foreign_receipts_fail_closed(
    tmp_path: Path, reason: str
) -> None:
    attempt, factory, context, embedding, index = await indexed(tmp_path)
    verification = service()
    if reason != "missing":
        await attempt.verify()
    if reason == "cross_invocation":
        attempt = verification.CandidateVerificationAttempt(
            factory, context, config=candidate(tmp_path)[2], index=index
        )
    elif reason == "cross_context":
        from atlasrag.services.ingestion.identity import build_candidate_context

        context = build_candidate_context(context.source, "0" * 64)
    elif reason in {"new_index", "failed_index"}:
        if reason == "failed_index":
            embedding.error = TimeoutError()
            with pytest.raises(TimeoutError):
                await attempt.index(embedding=embedding, clock=lambda: NOW)
        else:
            await attempt.index(embedding=embedding, clock=lambda: NOW)
    elif reason == "failed_reverify":
        index.rows.pop()
        with pytest.raises(verification.IndexVerificationError):
            await attempt.verify()
    elif reason == "invalid_reverify":
        from atlasrag.domain.errors import ConfigurationError

        with pytest.raises(ConfigurationError):
            await attempt.verify(batch_size=0)
    async with factory() as unit:
        with pytest.raises((InvariantViolationError, ValueError)):
            await attempt.consume_verified(unit, context)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_pending_verify_invalidates_previous_receipt_and_cannot_rearm_after_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    attempt, factory, context, _, index = await indexed(tmp_path)
    await attempt.verify()
    started, release = asyncio.Event(), asyncio.Event()
    original_iter = index.iter_revision_projection

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
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        async with factory() as unit:
            with pytest.raises(InvariantViolationError):
                await attempt.consume_verified(unit, context)
        release.set()
        with pytest.raises(InvariantViolationError):
            await task
    async with factory() as unit:
        with pytest.raises(InvariantViolationError):
            await attempt.consume_verified(unit, context)
    monkeypatch.setattr(index, "iter_revision_projection", original_iter)
    await attempt.verify()  # The owning operation released its busy guard.


@pytest.mark.asyncio
async def test_index_return_contains_the_snapshot_used_before_external_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlasrag.services.indexing.candidate import index_candidate

    factory, context, runtime = candidate(tmp_path)
    embedding = Embedding(factory, runtime)
    index = Index(factory, embedding)
    old_chunks = factory.database.chunks[(context.document_id, context.revision_id)]
    original_initialize = index.initialize

    async def mutate() -> None:
        await original_initialize()
        change_canonical(factory, context)

    monkeypatch.setattr(index, "initialize", mutate)
    snapshot = await index_candidate(
        factory,
        context,
        config=runtime,
        embedding=embedding,
        index=index,
        clock=lambda: NOW,
    )
    assert snapshot is not None, "T4 must return its authoritative indexed snapshot"
    assert len(snapshot.chunks) == len(old_chunks)
    assert snapshot.canonical_digest


@pytest.mark.asyncio
async def test_malformed_reachable_projection_has_narrow_business_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlasrag.providers.index import projection

    attempt, _, _, _, index = await indexed(tmp_path)
    malformed = getattr(projection, "IndexProjectionMalformedError", None)
    assert malformed is not None, (
        "reachable malformed projection needs a precise subtype"
    )

    async def invalid(
        *args: object, **kwargs: object
    ) -> AsyncGenerator[MilvusRevisionProjection, None]:
        raise malformed()
        yield  # pragma: no cover

    monkeypatch.setattr(index, "iter_revision_projection", invalid)
    with pytest.raises(service().IndexVerificationError) as caught:
        await attempt.verify()
    assert caught.value.reason == "malformed_projection"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [ProviderResponseError(), DependencyUnavailableError()]
)
async def test_cleanup_failure_during_mismatch_stays_technical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    attempt, _, _, _, index = await indexed(tmp_path)

    async def invalid(
        *args: object, **kwargs: object
    ) -> AsyncGenerator[MilvusRevisionProjection, None]:
        try:
            yield index.rows[0].model_copy(update={"content": "wrong"})
        finally:
            raise error

    monkeypatch.setattr(index, "iter_revision_projection", invalid)
    with pytest.raises(type(error)) as caught:
        await attempt.verify()
    assert caught.value is error


@pytest.mark.asyncio
async def test_stored_strategy_identity_change_is_detected_even_with_identical_chunks(
    tmp_path: Path,
) -> None:
    attempt, factory, context, _, _ = await indexed(tmp_path)
    scope = context.document_id, context.revision_id
    factory.database.chunks[scope] = tuple(
        chunk.model_copy(
            update={
                "strategy_metadata": {
                    **chunk.strategy_metadata,
                    "profile_fingerprint": "f" * 64,
                }
            }
        )
        for chunk in factory.database.chunks[scope]
    )
    with pytest.raises(service().IndexVerificationError) as caught:
        await attempt.verify()
    assert caught.value.reason == "canonical_changed"


@pytest.mark.asyncio
async def test_forged_complete_insert_acknowledgement_is_not_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt, _, _, embedding, index = await indexed(tmp_path)

    async def acknowledge(records: Any, **kwargs: object) -> dict[str, object]:
        return {"ids": [row.chunk_id for row in records], "insert_count": len(records)}

    monkeypatch.setattr(index, "insert_revision_batch", acknowledge)
    await attempt.index(embedding=embedding, clock=lambda: NOW)
    with pytest.raises(service().IndexVerificationError) as caught:
        await attempt.verify()
    assert caught.value.reason == "missing_ids"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [None, "sheet", "row_start", "row_end", "anchor", "bool_int", "unknown_field"],
)
async def test_typed_table_metadata_preserves_sheet_ranges_and_anchor(
    tmp_path: Path, fault: str | None
) -> None:
    from stage3.test_table_chunking import table_data

    from atlasrag.domain.ingestion.elements import render_table_data
    from atlasrag.domain.ingestion.enums import ElementType
    from atlasrag.services.ingestion.identity import build_element

    verification = service()
    factory, context, runtime = candidate(tmp_path)
    data = table_data()
    element = build_element(
        document_id=context.document_id,
        revision_id=context.revision_id,
        element_type=ElementType.TABLE,
        order=0,
        content=render_table_data(data),
        section_path=("Metrics",),
        source_anchor=data.source_anchor,
        table_data=data,
    )
    document = _document(context).model_copy(update={"elements": (element,)})
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
    factory.database.chunks[scope] = map_chunks_to_stored(
        build_chunks(document),
        chunker_behavior_version="chunker-v1",
        profile_fingerprint=ChunkingProfile().profile_fingerprint,
        created_at=NOW,
    )
    embedding = Embedding(factory, runtime)
    index = Index(factory, embedding)
    attempt = verification.CandidateVerificationAttempt(
        factory, context, config=runtime, index=index
    )
    await attempt.index(embedding=embedding, clock=lambda: NOW)
    assert len(index.rows) == 1 and index.rows[0].chunk_type == "TABLE"
    if fault is None:
        await attempt.verify()
        return
    payload = index.rows[0].model_dump(mode="json")
    metadata = payload["structured_metadata"]
    if fault == "sheet":
        metadata["provenance"]["sheet_name"] = "Wrong"
    elif fault in {"row_start", "row_end"}:
        metadata["provenance"][fault] += 1
    elif fault == "anchor":
        metadata["source_anchor"]["cell_range"] = "A1:B9"
    elif fault == "bool_int":
        metadata["provenance"]["row_start"] = False
    else:
        metadata["extra"] = "unrecognized"
    index.rows[0] = MilvusRevisionProjection.model_validate(payload)
    with pytest.raises(verification.IndexVerificationError):
        await attempt.verify()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["verify", "consume"])
async def test_indexed_attempt_requires_indexing_status_at_verify_and_consume(
    tmp_path: Path,
    operation: str,
) -> None:
    attempt, factory, context, _, _ = await indexed(tmp_path)
    await attempt.verify()
    stored = factory.database.documents[context.document_id]
    factory.database.documents[context.document_id] = stored.model_copy(
        update={"ingestion_status": IngestionStatus.CHUNKED}
    )
    if operation == "verify":
        with pytest.raises(InvariantViolationError):
            await attempt.verify()
    else:
        async with factory() as unit:
            with pytest.raises(InvariantViolationError):
                await attempt.consume_verified(unit, context)
    async with factory() as unit:
        with pytest.raises(InvariantViolationError):
            await attempt.consume_verified(unit, context)
