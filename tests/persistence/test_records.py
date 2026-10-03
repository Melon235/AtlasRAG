"""Storage records validate canonical data without adding business transitions."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

NOW = datetime(2026, 9, 28, tzinfo=UTC)


@pytest.fixture
def records() -> ModuleType:
    return importlib.import_module("atlasrag.repositories.postgres.records")


def test_persistence_records_module_exists() -> None:
    path = Path(__file__).resolve().parents[2]
    assert (path / "src/atlasrag/repositories/postgres/records.py").is_file(), (
        "Stage 2 immutable persistence records are not implemented"
    )


def document_data() -> dict[str, object]:
    return {
        "document_id": "doc-1",
        "file_name": "manual.md",
        "relative_source_path": "guides/manual.md",
        "source_type": "MD",
        "observed_content_hash": "hash-1",
        "ingestion_status": "RECEIVED",
        "created_at": NOW,
        "updated_at": NOW,
    }


def element_data() -> dict[str, object]:
    return {
        "element_id": "el-1",
        "document_id": "doc-1",
        "revision_id": "rev-1",
        "element_type": "TABLE",
        "order_index": 0,
        "content": "A | B",
        "section_path": ["Chapter 1"],
        "source_anchor": {"page_number": 1},
        "structured_content": {"rows": [["A", "B"], [1, True]], "empty": None},
        "metadata": {"parser": "v1"},
        "created_at": NOW,
    }


def chunk_data() -> dict[str, object]:
    return {
        "chunk_id": "child-1",
        "document_id": "doc-1",
        "revision_id": "rev-1",
        "chunk_type": "TEXT_CHILD",
        "parent_id": "parent-1",
        "content": "text",
        "section_path": ["Chapter 1"],
        "source_anchor": {"page_number": 1},
        "strategy_metadata": {"version": "v1", "window": 100},
        "created_at": NOW,
    }


def model(records: ModuleType, name: str) -> type[BaseModel]:
    return cast(type[BaseModel], getattr(records, name))


@pytest.mark.parametrize(
    "name,data",
    [
        ("StoredDocument", document_data()),
        ("StoredElement", element_data()),
        ("StoredChunk", chunk_data()),
        ("RuntimeMetadata", {}),
    ],
)
def test_records_are_frozen_strict_and_json_round_trip(
    records: ModuleType,
    name: str,
    data: dict[str, object],
) -> None:
    record_type = model(records, name)
    record = record_type.model_validate(data)
    assert record_type.model_validate_json(record.model_dump_json()) == record
    field = next(iter(record_type.model_fields))
    with pytest.raises(ValidationError, match="frozen"):
        setattr(record, field, getattr(record, field))
    for forbidden in ("provider", "graph", "benchmark_case_id", "unknown"):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            record_type.model_validate({**data, forbidden: "value"})


@pytest.mark.parametrize(
    "value", ["", " ", " doc-1", "doc-1 ", "doc\x00", "doc\n", 123]
)
@pytest.mark.parametrize(
    "name,base,field",
    [
        ("StoredDocument", document_data(), "document_id"),
        ("StoredElement", element_data(), "element_id"),
        ("StoredElement", element_data(), "document_id"),
        ("StoredElement", element_data(), "revision_id"),
        ("StoredChunk", chunk_data(), "chunk_id"),
        ("StoredChunk", chunk_data(), "parent_id"),
    ],
)
def test_identifiers_are_canonical_nonblank_strings(
    records: ModuleType,
    name: str,
    base: dict[str, object],
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        model(records, name).model_validate({**base, field: value})


@pytest.mark.parametrize(
    "value",
    [
        "/abs.md",
        "../a.md",
        "a/../b.md",
        "a//b.md",
        "./a.md",
        "a\\b.md",
        "C:/a.md",
        "a.md ",
    ],
)
def test_document_reuses_canonical_relative_source_path(
    records: ModuleType, value: str
) -> None:
    with pytest.raises(ValidationError, match="canonical POSIX relative path"):
        model(records, "StoredDocument").model_validate(
            {**document_data(), "relative_source_path": value}
        )


@pytest.mark.parametrize(
    "name,base,field",
    [
        ("StoredDocument", document_data(), "created_at"),
        ("StoredDocument", document_data(), "updated_at"),
        ("StoredElement", element_data(), "created_at"),
        ("StoredChunk", chunk_data(), "created_at"),
    ],
)
def test_naive_timestamps_are_rejected(
    records: ModuleType, name: str, base: dict[str, object], field: str
) -> None:
    with pytest.raises(ValidationError):
        model(records, name).model_validate({**base, field: NOW.replace(tzinfo=None)})


@pytest.mark.parametrize("prefix", ["current", "building"])
@pytest.mark.parametrize("mask", range(1, 7))
def test_partial_revision_triples_are_rejected(
    records: ModuleType, prefix: str, mask: int
) -> None:
    data = document_data()
    for index, suffix in enumerate(
        ("revision_id", "content_hash", "pipeline_fingerprint")
    ):
        if mask & (1 << index):
            data[f"{prefix}_{suffix}"] = "value"
    with pytest.raises(ValidationError, match="all present or all absent"):
        model(records, "StoredDocument").model_validate(data)


def test_current_and_candidate_can_coexist_on_failure(records: ModuleType) -> None:
    data = document_data()
    for prefix in ("current", "building"):
        for suffix in ("revision_id", "content_hash", "pipeline_fingerprint"):
            data[f"{prefix}_{suffix}"] = f"{prefix}-{suffix}"
    data.update(ingestion_status="FAILED", failed_stage="INDEX_VERIFY")
    record = model(records, "StoredDocument").model_validate(data)
    assert record.model_dump()["current_revision_id"] == "current-revision_id"


@pytest.mark.parametrize(
    "status",
    [
        "RECEIVED",
        "PARSING",
        "PARSED",
        "CHUNKING",
        "CHUNKED",
        "INDEXING",
        "READY",
        "FAILED",
    ],
)
def test_storage_status_vocabulary_without_transition_logic(
    records: ModuleType, status: str
) -> None:
    data = {**document_data(), "ingestion_status": status}
    if status == "FAILED":
        data["failed_stage"] = "PARSE"
    assert (
        model(records, "StoredDocument")
        .model_validate(data)
        .model_dump()["ingestion_status"]
        == status
    )


@pytest.mark.parametrize(
    "stage",
    ["PARSE", "CHUNK", "REPRESENTATION", "EMBEDDING", "INDEX_WRITE", "INDEX_VERIFY"],
)
def test_failed_stage_vocabulary(records: ModuleType, stage: str) -> None:
    data = {**document_data(), "ingestion_status": "FAILED", "failed_stage": stage}
    assert (
        model(records, "StoredDocument")
        .model_validate(data)
        .model_dump()["failed_stage"]
        == stage
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"ingestion_status": "FAILED"},
        {"failed_stage": "PARSE"},
        {"ingestion_status": "READY", "failed_stage": "CHUNK"},
        {"ingestion_status": "INVALID"},
        {"ingestion_status": b"READY"},
        {"source_type": "HTML"},
        {"source_type": b"MD"},
        {"ingestion_status": "FAILED", "failed_stage": "INVALID"},
    ],
)
def test_invalid_status_and_failure_combinations(
    records: ModuleType, changes: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        model(records, "StoredDocument").model_validate({**document_data(), **changes})


@pytest.mark.parametrize(
    "kind,parent",
    [
        ("TEXT_CHILD", "parent-1"),
        ("TEXT_PARENT", None),
        ("TABLE", None),
        ("TABLE", "parent-1"),
    ],
)
def test_stored_chunk_vocabulary(
    records: ModuleType, kind: str, parent: str | None
) -> None:
    assert model(records, "StoredChunk").model_validate(
        {**chunk_data(), "chunk_type": kind, "parent_id": parent}
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"parent_id": None},
        {"parent_id": "child-1"},
        {"chunk_type": "TABLE", "parent_id": "child-1"},
        {"chunk_type": "TABLE", "parent_id": " "},
        {"chunk_type": "TEXT_PARENT"},
        {"chunk_type": "INVALID"},
        {"chunk_type": b"TEXT_CHILD"},
        {"section_path": [""]},
        {"section_path": {"part"}},
    ],
)
def test_invalid_chunk_parent_or_provenance(
    records: ModuleType, changes: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        model(records, "StoredChunk").model_validate({**chunk_data(), **changes})


@pytest.mark.parametrize(
    "value",
    [
        {1: "bad key"},
        {"value": object()},
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": {1, 2}},
        [1, 2],
    ],
)
def test_metadata_is_json_safe(records: ModuleType, value: object) -> None:
    with pytest.raises(ValidationError):
        model(records, "StoredElement").model_validate(
            {**element_data(), "metadata": value}
        )


def test_metadata_is_deeply_frozen_and_detached(records: ModuleType) -> None:
    values: list[object] = [1, {"cell": "original"}]
    data = {**element_data(), "metadata": {"values": values}}
    record = model(records, "StoredElement").model_validate(data)
    values.append("later")
    nested = record.metadata  # type: ignore[attr-defined]
    assert nested["values"] == (1, {"cell": "original"})
    with pytest.raises(TypeError):
        nested["new"] = "bad"
    with pytest.raises(TypeError):
        nested["values"][1]["cell"] = "bad"


def test_cyclic_metadata_is_rejected(records: ModuleType) -> None:
    cyclic: dict[str, object] = {}
    cyclic["cycle"] = cyclic
    with pytest.raises(ValidationError):
        model(records, "StoredDocument").model_validate(
            {**document_data(), "parse_metadata": cyclic}
        )


@pytest.mark.parametrize("value", [-1, True, "1"])
def test_element_order_is_nonnegative_integer(
    records: ModuleType, value: object
) -> None:
    with pytest.raises(ValidationError):
        model(records, "StoredElement").model_validate(
            {**element_data(), "order_index": value}
        )


def test_runtime_metadata_default_and_strict_boolean(records: ModuleType) -> None:
    record_type = model(records, "RuntimeMetadata")
    assert record_type().model_dump() == {"query_cache_invalidation_required": False}
    assert record_type.model_validate({"query_cache_invalidation_required": True})
    with pytest.raises(ValidationError):
        record_type.model_validate({"query_cache_invalidation_required": "false"})
