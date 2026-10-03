"""Service-free validation tests for Milvus infrastructure records."""

from __future__ import annotations

import math
from collections.abc import Mapping

import pytest
from pydantic import ValidationError

from atlasrag.domain.errors import InvariantViolationError
from atlasrag.domain.retrieval import RetrievalCandidate
from atlasrag.providers.index.models import (
    MilvusChunkRecord,
    validate_milvus_record,
    validate_milvus_record_set,
)


def milvus_record(**overrides: object) -> MilvusChunkRecord:
    values: dict[str, object] = {
        "chunk_id": "chunk-1",
        "document_id": "doc-1",
        "revision_id": "rev-1",
        "file_name": "manual.md",
        "parent_id": "parent-1",
        "chunk_type": "TEXT_CHILD",
        "source_type": "MD",
        "content": "Canonical child content.",
        "sparse_text": "Architecture canonical child content",
        "dense_vector": [0.25, -0.5, 0.75],
        "structured_metadata": {
            "section_path": ["Architecture", "Persistence"],
            "page": 7,
            "published": True,
            "optional": None,
            "weight": 1.25,
        },
    }
    values.update(overrides)
    return MilvusChunkRecord.model_validate(values)


def test_records_accept_only_retrievable_chunk_types_and_are_frozen() -> None:
    child = milvus_record()
    table = milvus_record(
        chunk_id="table-1",
        chunk_type="TABLE",
        parent_id=None,
        source_type="XLSX",
    )

    assert child.chunk_type.value == "TEXT_CHILD"
    assert child.source_type.value == "MD"
    assert child.dense_vector == (0.25, -0.5, 0.75)
    assert table.chunk_type.value == "TABLE"
    assert table.parent_id is None
    with pytest.raises(ValidationError):
        setattr(child, "content", "mutated")
    with pytest.raises(ValidationError):
        milvus_record(chunk_type="TEXT_PARENT")


def test_text_child_requires_a_distinct_parent() -> None:
    with pytest.raises(ValidationError, match="parent"):
        milvus_record(parent_id=None)
    with pytest.raises(ValidationError, match="parent"):
        milvus_record(parent_id="chunk-1")


@pytest.mark.parametrize(
    "field,value",
    [
        ("chunk_id", ""),
        ("document_id", " doc-1"),
        ("revision_id", "rev\n1"),
        ("file_name", " "),
        ("parent_id", "bad\x7fid"),
        ("content", ""),
        ("sparse_text", "   "),
    ],
)
def test_record_text_boundaries_are_canonical(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        milvus_record(**{field: value})


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_type": "MARKDOWN"},
        {"source_type": b"MD"},
        {"chunk_type": b"TABLE"},
        {"dense_vector": {0.1, 0.2, 0.3}},
        {"dense_vector": (value for value in (0.1, 0.2, 0.3))},
        {"dense_vector": [0.1, True, 0.3]},
        {"dense_vector": [0.1, "0.2", 0.3]},
        {"dense_vector": [0.1]},
        {"dense_vector": []},
    ],
)
def test_record_rejects_noncanonical_enums_and_vectors(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        milvus_record(**overrides)


@pytest.mark.parametrize("nonfinite", [math.nan, math.inf, -math.inf])
def test_dense_vectors_must_be_finite(nonfinite: float) -> None:
    with pytest.raises(ValidationError):
        milvus_record(dense_vector=[0.1, nonfinite, 0.3])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chunk_id", "文" * 171),
        ("document_id", "文" * 171),
        ("revision_id", "文" * 171),
        ("parent_id", "文" * 171),
        ("file_name", "文" * 342),
        ("content", "文" * 21_846),
        ("sparse_text", "文" * 21_846),
    ],
)
def test_varchar_limits_are_enforced_as_utf8_bytes(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        milvus_record(**{field: value})


def test_structured_metadata_respects_milvus_encoded_json_byte_limit() -> None:
    with pytest.raises(ValidationError, match="65,536 bytes"):
        milvus_record(structured_metadata={"payload": "x" * 65_536})


def test_embedding_dimension_is_checked_at_the_provider_boundary() -> None:
    record = milvus_record()

    assert validate_milvus_record(record, embedding_dimension=3) == record
    with pytest.raises(InvariantViolationError):
        validate_milvus_record(record, embedding_dimension=2)
    with pytest.raises(InvariantViolationError):
        validate_milvus_record(record, embedding_dimension=True)
    with pytest.raises(InvariantViolationError):
        validate_milvus_record(record, embedding_dimension=0)
    with pytest.raises(InvariantViolationError):
        validate_milvus_record(record, embedding_dimension=1)
    with pytest.raises(InvariantViolationError):
        validate_milvus_record(record, embedding_dimension=32_769)


def test_record_set_requires_unique_ids_and_one_exact_scope() -> None:
    first = milvus_record()
    second = milvus_record(chunk_id="chunk-2")

    assert validate_milvus_record_set(
        [first, second],
        document_id="doc-1",
        revision_id="rev-1",
        embedding_dimension=3,
    ) == (first, second)

    with pytest.raises(InvariantViolationError):
        validate_milvus_record_set(
            [first, first],
            document_id="doc-1",
            revision_id="rev-1",
            embedding_dimension=3,
        )
    with pytest.raises(InvariantViolationError):
        validate_milvus_record_set(
            [first, milvus_record(chunk_id="chunk-2", document_id="doc-2")],
            document_id="doc-1",
            revision_id="rev-1",
            embedding_dimension=3,
        )
    with pytest.raises(InvariantViolationError):
        validate_milvus_record_set(
            [first, milvus_record(chunk_id="chunk-2", revision_id="rev-2")],
            document_id="doc-1",
            revision_id="rev-1",
            embedding_dimension=3,
        )


def test_record_set_rejects_untyped_or_forged_records() -> None:
    record = milvus_record()

    with pytest.raises(InvariantViolationError):
        validate_milvus_record_set(
            [record.model_dump(mode="python")],  # type: ignore[list-item]
            document_id="doc-1",
            revision_id="rev-1",
            embedding_dimension=3,
        )
    forged = MilvusChunkRecord.model_construct(
        **{
            **record.model_dump(mode="python"),
            "dense_vector": (math.inf, 0.0, 0.0),
        }
    )
    with pytest.raises(InvariantViolationError):
        validate_milvus_record(forged, embedding_dimension=3)


def test_structured_metadata_is_typed_recursive_json_and_immutable() -> None:
    source = {"nested": {"labels": ["a", "b"]}}
    record = milvus_record(structured_metadata=source)

    source["nested"] = {"labels": ["changed"]}
    assert record.structured_metadata["nested"] == {"labels": ("a", "b")}
    nested = record.structured_metadata["nested"]
    assert isinstance(nested, Mapping)
    with pytest.raises(TypeError):
        record.structured_metadata["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        nested["labels"] = ()  # type: ignore[index]
    assert record.model_dump(mode="json")["structured_metadata"] == {
        "nested": {"labels": ["a", "b"]}
    }


@pytest.mark.parametrize(
    "metadata",
    [
        [],
        {1: "not-a-string-key"},
        {"bad": object()},
        {"bad": math.inf},
        {"bad": "nul\x00value"},
    ],
)
def test_structured_metadata_rejects_non_json_values(metadata: object) -> None:
    with pytest.raises(ValidationError):
        milvus_record(structured_metadata=metadata)


def test_structured_metadata_rejects_cycles() -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    with pytest.raises(ValidationError, match="cycles"):
        milvus_record(structured_metadata=cyclic)


def test_stage1_retrieval_candidate_remains_vector_free() -> None:
    assert "dense_vector" not in RetrievalCandidate.model_fields
    assert "sparse_text" not in RetrievalCandidate.model_fields
