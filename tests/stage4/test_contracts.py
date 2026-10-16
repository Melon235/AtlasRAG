"""Closed immutable indexing DTOs remain internal and reject invalid lineage."""

from typing import Any

import pytest
from pydantic import ValidationError

from atlasrag.domain.enums import RetrievalChunkType, SourceType
from atlasrag.domain.indexing.models import (
    EmbeddingModelIdentity,
    IndexStructuredMetadataV1,
    PublishResult,
    RepresentationProfileV1,
    RetrievalRecord,
    RetrievalRepresentation,
)
from atlasrag.domain.ingestion.chunks import TextChildProvenance
from stage4.indexing_fixtures import (
    DOCUMENT_ID,
    REVISION_ID,
    table_chunk,
    text_chunks,
)


def metadata() -> IndexStructuredMetadataV1:
    _, child = text_chunks()
    assert isinstance(child.provenance, TextChildProvenance)
    return IndexStructuredMetadataV1(
        section_path=child.section_path, provenance=child.provenance
    )


@pytest.mark.parametrize("model", [RepresentationProfileV1, IndexStructuredMetadataV1])
def test_schema_version_is_strict_integer_one(model: Any) -> None:
    values = {} if model is RepresentationProfileV1 else metadata().model_dump()
    assert model.model_validate(values | {"schema_version": 1}).schema_version == 1
    for invalid in (True, 1.0, "1", 2):
        with pytest.raises(ValidationError):
            model.model_validate(values | {"schema_version": invalid})


def record_data() -> dict[str, Any]:
    _, child = text_chunks()
    return dict(
        chunk_id=child.chunk_id,
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        file_name="guide.md",
        parent_id=child.parent_id,
        chunk_type=RetrievalChunkType.TEXT_CHILD,
        source_type=SourceType.MD,
        content=child.raw_content,
        sparse_text=child.raw_content,
        dense_vector=[0.25, -0.5],
        structured_metadata=metadata(),
    )


@pytest.mark.parametrize(
    "model,field",
    [
        (RetrievalRecord, "content"),
        (RetrievalRecord, "sparse_text"),
        (RetrievalRepresentation, "dense_text"),
        (RetrievalRepresentation, "sparse_text"),
    ],
)
def test_direct_text_contracts_enforce_utf8_capacity(model: Any, field: str) -> None:
    values = record_data()
    if model is RetrievalRepresentation:
        values = {
            name: values[name]
            for name in (
                "chunk_id",
                "document_id",
                "revision_id",
                "chunk_type",
                "structured_metadata",
            )
        } | {"dense_text": "text", "sparse_text": "text"}
    at_limit = "界" * 21845
    assert getattr(model.model_validate(values | {field: at_limit}), field) == at_limit
    with pytest.raises(ValidationError, match="UTF-8"):
        model.model_validate(values | {field: at_limit + "a"})


@pytest.mark.parametrize(
    "model,field",
    [
        (RetrievalRecord, "content"),
        (RetrievalRecord, "sparse_text"),
        (RetrievalRepresentation, "dense_text"),
        (RetrievalRepresentation, "sparse_text"),
    ],
)
@pytest.mark.parametrize("content", [" ", "\t\n ", " \n界😀é\t "])
def test_index_text_preserves_nonempty_whitespace_and_unicode(
    model: Any, field: str, content: str
) -> None:
    values = record_data()
    if model is RetrievalRepresentation:
        values = {
            name: values[name]
            for name in (
                "chunk_id",
                "document_id",
                "revision_id",
                "chunk_type",
                "structured_metadata",
            )
        } | {"dense_text": "text", "sparse_text": "text"}
    result = model.model_validate(values | {field: content})
    assert getattr(result, field).encode("utf-8") == content.encode("utf-8")
    with pytest.raises(ValidationError):
        model.model_validate(values | {field: ""})


def test_record_nested_values_are_immutable_and_input_is_detached() -> None:
    values = record_data()
    record = RetrievalRecord(**values)
    values["dense_vector"].append(4.0)
    assert record.dense_vector == (0.25, -0.5)
    with pytest.raises(ValidationError):
        record.structured_metadata.section_path = ("changed",)
    assert isinstance(record.structured_metadata.provenance, TextChildProvenance)
    with pytest.raises(ValidationError):
        record.structured_metadata.provenance.source_spans[0].char_start = 1
    assert RetrievalRecord.model_validate_json(record.model_dump_json()) == record


@pytest.mark.parametrize(
    "updates",
    [
        {"extra": 1},
        {"chunk_id": "bad"},
        {"document_id": "bad"},
        {"revision_id": "bad"},
        {"parent_id": None},
        {"parent_id": "bad"},
        {"chunk_type": "TEXT_PARENT"},
        {"chunk_type": "TABLE"},
        {"source_type": b"MD"},
        {"content": ""},
        # Approved Stage 4 amendment: whitespace text is valid, empty is not.
        {"sparse_text": ""},
        {"dense_vector": []},
        {"dense_vector": {1.0, 2.0}},
        {"dense_vector": [True, 1.0]},
        {"dense_vector": ["1", 2.0]},
        {"dense_vector": [float("nan"), 1.0]},
        {"dense_vector": [float("inf"), 1.0]},
        {"structured_metadata": {"arbitrary": {"provider": "payload"}}},
    ],
)
def test_invalid_record_is_rejected(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RetrievalRecord.model_validate(record_data() | updates)


def test_record_rejects_self_parent_and_mismatched_table_metadata() -> None:
    data = record_data()
    with pytest.raises(ValidationError):
        RetrievalRecord.model_validate(data | {"parent_id": data["chunk_id"]})
    table = table_chunk()
    with pytest.raises(ValidationError):
        RetrievalRecord.model_validate(
            data | {"structured_metadata": {"provenance": table.provenance}}
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"schema_version": "v2"},
        {"strategy_name": "summary"},
        {"representation_version": "representation-v2"},
        {"unknown": 1},
    ],
)
def test_profile_rejects_unknown_schema_strategy_and_version(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        RepresentationProfileV1.model_validate(updates)


def test_forged_unknown_fields_in_nested_metadata_fail_closed() -> None:
    with pytest.raises(ValidationError):
        RetrievalRecord.model_validate(
            record_data()
            | {
                "structured_metadata": metadata().model_copy(
                    update={"provider_payload": {"x": 1}}
                )
            }
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"schema_version": True},
        {"schema_version": b"index-structured-metadata-v1"},
        {"section_path": {"unordered"}},
        {"extra": 1},
        {"source_anchor": {"page_number": 1}},
    ],
)
def test_metadata_schema_and_text_anchor_are_closed(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        IndexStructuredMetadataV1.model_validate(metadata().model_dump() | updates)


def test_representation_revalidates_forged_nested_metadata() -> None:
    data = record_data()
    with pytest.raises(ValidationError):
        RetrievalRepresentation(
            chunk_id=data["chunk_id"],
            document_id=DOCUMENT_ID,
            revision_id=REVISION_ID,
            chunk_type=RetrievalChunkType.TEXT_CHILD,
            dense_text="text",
            sparse_text="text",
            structured_metadata=metadata().model_copy(
                update={"schema_version": "unknown"}
            ),
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"dimension": 0},
        {"dimension": True},
        {"model_revision": " "},
        {"tokenizer_revision": ""},
        {"device": ""},
        {"extra": 1},
    ],
)
def test_embedding_identity_is_strict_and_versioned(updates: dict[str, object]) -> None:
    data = dict(
        model_id="Qwen/Qwen3-Embedding-0.6B",
        model_revision="abc",
        tokenizer_id="Qwen/Qwen3-Embedding-0.6B",
        tokenizer_revision="def",
        dimension=1024,
        device="cuda:0",
    )
    assert EmbeddingModelIdentity.model_validate(data).dimension == 1024
    with pytest.raises(ValidationError):
        EmbeddingModelIdentity.model_validate(data | updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"document_id": "bad"},
        {"published_revision_id": "bad"},
        {"previous_current_revision_id": "bad"},
        {"extra": 1},
    ],
)
def test_publish_result_validates_revision_identities(
    updates: dict[str, object],
) -> None:
    data = dict(document_id=DOCUMENT_ID, published_revision_id=REVISION_ID)
    assert PublishResult.model_validate(data).previous_current_revision_id is None
    with pytest.raises(ValidationError):
        PublishResult.model_validate(data | updates)
