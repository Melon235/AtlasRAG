"""Narrow deterministic failure admission and last-good preservation."""

import importlib
from asyncio import CancelledError
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import NoReturn, cast

import pytest
from stage3.test_candidate_services import NOW, _stored_document
from stage3.test_chunking import document, element, small_profile
from stage3.test_table_chunking import document as table_document
from stage3.test_table_chunking import profile, table_data, table_element

from atlasrag._canonical import canonical_sha256
from atlasrag.domain.enums import SourceType
from atlasrag.domain.errors import (
    CanonicalDataError,
    ConfigurationError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    InvariantViolationError,
    ProviderResponseError,
    ResourceExhaustedError,
)
from atlasrag.domain.indexing.failures import (
    EmbeddingInputLimitError,
    EmbeddingOutputError,
    IndexVerificationError,
    RepresentationInputLimitError,
)
from atlasrag.domain.ingestion.elements import TableData, render_table_data
from atlasrag.providers.index.projection import (
    IndexProjectionMalformedError,
    IndexWriteRejectedError,
)
from atlasrag.repositories.postgres.records import (
    FailedStage,
    IngestionStatus,
    StoredDocument,
)
from atlasrag.services.ingestion import chunking
from atlasrag.services.ingestion.chunking import ChunkingProfile, build_chunks
from atlasrag.services.ingestion.persistence import record_failure
from stage4.candidate_fakes import candidate


def service() -> ModuleType:
    try:
        return importlib.import_module("atlasrag.services.indexing.failure")
    except ModuleNotFoundError:
        pytest.fail("T6 classified indexing failure service is missing", pytrace=False)


def chunk_error_type() -> type[ValueError]:
    try:
        return cast(
            type[ValueError],
            importlib.import_module(
                "atlasrag.domain.ingestion.failures"
            ).ChunkInputLimitError,
        )
    except ModuleNotFoundError:
        pytest.fail("T6 precise chunk input capacity type is missing", pytrace=False)


CONTROLLED = [
    (
        RepresentationInputLimitError(actual_bytes=2, max_bytes=1),
        FailedStage.REPRESENTATION,
    ),
    (EmbeddingInputLimitError(actual_tokens=2, max_tokens=1), FailedStage.EMBEDDING),
    (EmbeddingOutputError("invalid successful vector"), FailedStage.EMBEDDING),
    (IndexWriteRejectedError(), FailedStage.INDEX_WRITE),
    (IndexVerificationError("missing_ids"), FailedStage.INDEX_VERIFY),
]
UNCLASSIFIED = [
    ValueError("table cell cannot fit without data loss"),
    RuntimeError("unexpected"),
    Exception("unexpected"),
    InvariantViolationError(),
    CanonicalDataError(),
    ConfigurationError(),
    ProviderResponseError(),
    DependencyTimeoutError(),
    DependencyUnavailableError(),
    ResourceExhaustedError(),
    IndexProjectionMalformedError(),
    ImportError("SDK unavailable"),
    ConnectionError("unavailable"),
    TimeoutError("timeout"),
    MemoryError("OOM"),
    CancelledError(),
]


@pytest.mark.parametrize(("error", "stage"), CONTROLLED)
def test_classification_admits_only_known_deterministic_errors(
    error: BaseException, stage: FailedStage
) -> None:
    assert service().classify_index_failure(error) is stage


@pytest.mark.parametrize("error", UNCLASSIFIED)
@pytest.mark.asyncio
async def test_unclassified_errors_never_open_a_failure_transaction(
    tmp_path: Path, error: BaseException
) -> None:
    api = service()
    factory, context, _ = candidate(tmp_path)
    before = factory.database.clone()
    assert api.classify_index_failure(error) is None

    def forbidden_clock() -> NoReturn:
        pytest.fail("unclassified errors must propagate before reading the clock")

    with pytest.raises(type(error)) as caught:
        await api.record_index_failure(factory, context, error, clock=forbidden_clock)
    assert caught.value is error
    assert not factory.unit_of_works
    assert factory.database == before


@pytest.mark.parametrize(("error", "stage"), CONTROLLED)
@pytest.mark.parametrize("has_current", [True, False])
@pytest.mark.parametrize("marker", [True, False])
@pytest.mark.asyncio
async def test_record_changes_only_candidate_failure_fields(
    tmp_path: Path,
    error: BaseException,
    stage: FailedStage,
    has_current: bool,
    marker: bool,
) -> None:
    api = service()
    factory, context, _ = candidate(tmp_path)
    stored = factory.database.documents[context.document_id]
    changes: dict[str, object] = {"ingestion_status": IngestionStatus.INDEXING}
    if not has_current:
        metadata = stored.model_dump(mode="python")["parse_metadata"]
        metadata["current"] = None
        changes.update(
            current_revision_id=None,
            current_content_hash=None,
            current_pipeline_fingerprint=None,
            parse_metadata=metadata,
        )
    stored = StoredDocument.model_validate(
        {**stored.model_dump(mode="python"), **changes}
    )
    factory.database.documents[context.document_id] = stored
    factory.database.query_cache_invalidation_required = marker
    before = factory.database.clone()
    timestamp = NOW + timedelta(seconds=1)
    failed = await api.record_index_failure(
        factory, context, error, clock=lambda: timestamp
    )
    expected = stored.model_copy(
        update={
            "ingestion_status": IngestionStatus.FAILED,
            "failed_stage": stage,
            "updated_at": timestamp,
        }
    )
    before.documents[context.document_id] = expected
    assert failed == expected
    assert factory.database == before
    assert factory.commit_count == 1
    assert factory.rollback_count == 0
    assert factory.calls == ["documents.get_by_id", "documents.put"]
    assert factory.runtime_metadata_accesses == 0


@pytest.mark.parametrize(("error", "stage"), CONTROLLED)
@pytest.mark.parametrize(
    "status", [s for s in IngestionStatus if s is not IngestionStatus.INDEXING]
)
@pytest.mark.asyncio
async def test_every_other_origin_is_rejected(
    tmp_path: Path, error: BaseException, stage: FailedStage, status: IngestionStatus
) -> None:
    api = service()
    factory, context, _ = candidate(tmp_path)
    stored = factory.database.documents[context.document_id]
    factory.database.documents[context.document_id] = stored.model_copy(
        update={
            "ingestion_status": status,
            "failed_stage": stage if status is IngestionStatus.FAILED else None,
        }
    )
    before = factory.database.clone()
    with pytest.raises(InvariantViolationError):
        await api.record_index_failure(factory, context, error, clock=lambda: NOW)
    assert factory.database == before
    assert factory.commit_count == 0
    assert not factory.document_writes


@pytest.mark.parametrize(
    "change",
    [
        {"document_id": "doc_" + "f" * 64},
        {"file_name": "other.txt"},
        {"relative_source_path": "other/candidate.txt"},
        {"source_type": SourceType.PDF},
        {"observed_content_hash": "e" * 64},
        {"building_revision_id": "rev_" + "e" * 64},
        {"building_content_hash": "e" * 64},
        {"building_pipeline_fingerprint": "e" * 64},
        {
            "building_revision_id": None,
            "building_content_hash": None,
            "building_pipeline_fingerprint": None,
        },
    ],
)
@pytest.mark.asyncio
async def test_wrong_source_or_candidate_scope_never_records_failure(
    tmp_path: Path, change: dict[str, object]
) -> None:
    api = service()
    factory, context, _ = candidate(tmp_path)
    stored = factory.database.documents[context.document_id]
    factory.database.documents[context.document_id] = stored.model_copy(
        update={
            "ingestion_status": IngestionStatus.INDEXING,
            **change,
        }
    )
    before = factory.database.clone()
    with pytest.raises((InvariantViolationError, ValueError)):
        await api.record_index_failure(
            factory, context, CONTROLLED[0][0], clock=lambda: NOW
        )
    assert factory.database == before
    assert factory.commit_count == 0
    assert not factory.document_writes


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "same_current",
        "current_slot",
        "building_slot",
        "schema",
        "no_building_slot",
    ],
)
@pytest.mark.asyncio
async def test_missing_candidate_or_corrupt_envelope_is_not_overwritten(
    tmp_path: Path, damage: str
) -> None:
    api = service()
    factory, context, _ = candidate(tmp_path)
    stored = factory.database.documents[context.document_id]
    payload = stored.model_dump(mode="python")
    payload["ingestion_status"] = IngestionStatus.INDEXING
    if damage == "same_current":
        payload.update(
            current_revision_id=context.revision_id,
            current_content_hash=context.content_hash,
            current_pipeline_fingerprint=context.pipeline_fingerprint,
        )
    elif damage in {"current_slot", "building_slot"}:
        payload["parse_metadata"][damage.removesuffix("_slot")]["revision_id"] = (
            "rev_" + "f" * 64
        )
    elif damage == "schema":
        payload["parse_metadata"]["schema_version"] = 999
    elif damage == "no_building_slot":
        payload["parse_metadata"]["building"] = None
    factory.database.documents[context.document_id] = stored.model_copy(update=payload)
    if damage == "missing":
        del factory.database.documents[context.document_id]
    before = factory.database.clone()
    with pytest.raises((InvariantViolationError, CanonicalDataError)):
        await api.record_index_failure(
            factory, context, CONTROLLED[0][0], clock=lambda: NOW
        )
    assert factory.database == before
    assert factory.commit_count == 0
    assert not factory.document_writes


@pytest.mark.parametrize(
    "operation", ["documents.get_by_id", "documents.put", "uow.commit"]
)
@pytest.mark.parametrize(
    "error",
    [ConnectionError("PG unavailable"), TimeoutError("PG timeout"), CancelledError()],
)
@pytest.mark.asyncio
async def test_recorder_storage_errors_and_cancellation_propagate_and_rollback(
    tmp_path: Path, operation: str, error: BaseException
) -> None:
    api = service()
    factory, context, _ = candidate(tmp_path)
    factory.database.documents[context.document_id] = factory.database.documents[
        context.document_id
    ].model_copy(update={"ingestion_status": IngestionStatus.INDEXING})
    before = factory.database.clone()
    factory.fail_after(operation, error)
    with pytest.raises(type(error)) as caught:
        await api.record_index_failure(
            factory, context, CONTROLLED[0][0], clock=lambda: NOW
        )
    assert caught.value is error
    assert factory.database == before
    assert factory.commit_count == 0
    assert factory.rollback_count == 1


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2026, 10, 19),
        datetime(2026, 10, 19, tzinfo=timezone(timedelta(hours=8))),
        "2026-10-19",
    ],
)
@pytest.mark.asyncio
async def test_recorder_requires_aware_utc_clock_before_transaction(
    tmp_path: Path, timestamp: object
) -> None:
    api = service()
    factory, context, _ = candidate(tmp_path)
    with pytest.raises(ValueError, match="aware UTC"):
        await api.record_index_failure(
            factory, context, CONTROLLED[0][0], clock=lambda: timestamp
        )
    assert not factory.unit_of_works


@pytest.mark.parametrize(
    ("status", "stage"),
    [
        (IngestionStatus.PARSING, FailedStage.PARSE),
        (IngestionStatus.CHUNKING, FailedStage.CHUNK),
    ],
)
@pytest.mark.asyncio
async def test_parse_and_chunk_still_use_frozen_stage3_recorder(
    tmp_path: Path, status: IngestionStatus, stage: FailedStage
) -> None:
    factory, context, _ = candidate(tmp_path)
    factory.database.documents[context.document_id] = _stored_document(
        context, status=status, current=True
    )
    stored = factory.database.documents[context.document_id]
    failed = await record_failure(factory, context, stage, clock=lambda: NOW)
    assert failed == stored.model_copy(
        update={
            "ingestion_status": IngestionStatus.FAILED,
            "failed_stage": stage,
            "updated_at": NOW,
        }
    )
    assert factory.commit_count == 1


@pytest.mark.parametrize(("error", "stage"), CONTROLLED)
@pytest.mark.asyncio
async def test_frozen_stage3_recorder_does_not_accept_index_stages(
    tmp_path: Path, error: BaseException, stage: FailedStage
) -> None:
    factory, context, _ = candidate(tmp_path)
    with pytest.raises(InvariantViolationError):
        await record_failure(factory, context, stage, clock=lambda: NOW)
    assert not factory.unit_of_works


@pytest.mark.parametrize(
    ("data", "limit", "message"),
    [
        (
            table_data(headers=("x" * 21, "B")),
            20,
            "table header cannot fit without data loss",
        ),
        (
            table_data(rows=(("x" * 21, "1"),)),
            20,
            "table cell cannot fit without data loss",
        ),
        (table_data(), 20, "table header block leaves no room for a complete row"),
        (table_data(), 55, "table row cannot fit without data loss"),
    ],
)
def test_actual_oversized_tables_raise_precise_value_error_subtype(
    data: TableData, limit: int, message: str
) -> None:
    error_type = chunk_error_type()
    with pytest.raises(error_type, match=f"^{message}$") as caught:
        build_chunks(table_document((table_element(data),)), profile(limit))
    assert isinstance(caught.value, ValueError)
    assert service().classify_index_failure(caught.value) is None


@pytest.mark.parametrize(
    "operation",
    [
        lambda: chunking._split_ranges("abc", 0),
        lambda: chunking._split_ranges("abc", 1, source_maps=()),
        lambda: chunking._child_source_span_groups(((1, 2),), ()),
        lambda: chunking._child_source_span_groups(((0, 1),), ()),
        lambda: chunking._table_slices(element(0, "text"), ChunkingProfile()),
        lambda: ChunkingProfile(parent_max_chars=5, child_max_chars=1),
        lambda: chunking.require_chunker_behavior_version_change(
            previous_profile=ChunkingProfile(),
            previous_version="chunker-v7",
            current_profile=ChunkingProfile(parent_max_chars=100),
            current_version="chunker-v7",
        ),
    ],
)
def test_internal_chunk_errors_and_configuration_errors_remain_unclassified(
    operation: Callable[[], object],
) -> None:
    error_type = chunk_error_type()
    with pytest.raises(ValueError) as caught:
        operation()
    assert not isinstance(caught.value, error_type)
    assert service().classify_index_failure(caught.value) is None


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("text", "1373abf5d847ec352caca8494fae58a56d4e02d01c402f5f0a05281de46b6a21"),
        (
            "whitespace",
            "b2fa024cd1b3cd5b389ffc54e285cbdd064ddfb289147a69eeb975ef93f9f611",
        ),
        ("full", "4e8ddee752b8ec63d076290472125a5f51a87bdb6c045082d7fceb148ecc684e"),
        (
            "row_group",
            "6b663200132e1af0f3d50e926308907baa26c71a210c5ba0b16e8071b36039ed",
        ),
    ],
)
def test_pre_t6_canonical_outputs_including_ids_are_frozen(
    kind: str, expected: str
) -> None:
    # Captured from pre-T6 HEAD 5af10dc, before changing any production source.
    if kind == "text":
        parsed = document((element(0, "Alpha text."), element(1, "Beta text.")))
        selected_profile = small_profile()
    elif kind == "whitespace":
        parsed = document((element(0, "a" + " " * 40 + "b"),))
        selected_profile = small_profile()
    else:
        parsed = table_document((table_element(table_data()),))
        limit = (
            8000
            if kind == "full"
            else len(render_table_data(table_data(rows=(("Alpha", "1"),))))
        )
        selected_profile = profile(limit)
    chunks = build_chunks(parsed, selected_profile)
    assert canonical_sha256([c.model_dump(mode="json") for c in chunks]) == expected
    assert chunking.DEFAULT_CHUNKER_BEHAVIOR_VERSION == "chunker-v7"
    assert (
        ChunkingProfile().profile_fingerprint
        == "46aba26b91b69ccd6ba414561b594b88f49df674077cf0ef8344b04039737fcc"
    )
