"""Lossless pure representations, profile binding, and fail-closed admission."""

from typing import Any

import pytest
from pydantic import ValidationError

from atlasrag.domain.indexing.failures import RepresentationInputLimitError
from atlasrag.domain.indexing.models import RepresentationProfileV1
from atlasrag.domain.ingestion.chunks import (
    ChunkRecord,
    ChunkType,
    TableSliceMode,
    derive_chunk_content_hash,
    render_table_slice,
)
from atlasrag.providers.index.models import MilvusChunkRecord
from atlasrag.services.indexing.representation import RepresentationBuilder
from stage4.indexing_fixtures import (
    canonical_whitespace_chunks,
    retrieval_record,
    table_chunk,
    text_chunks,
)


def builder(**kwargs: Any) -> RepresentationBuilder:
    return RepresentationBuilder(
        RepresentationProfileV1(), representation_version="representation-v1", **kwargs
    )


@pytest.mark.parametrize(
    "content",
    ["A" + " " * 4000 + "B", "界😀A" + " " * 4000 + "Bé\n"],
    ids=["ascii", "mixed-unicode"],
)
def test_actual_stage3_children_survive_all_index_contracts_losslessly(
    content: str,
) -> None:
    chunks = canonical_whitespace_chunks(content)
    snapshots = tuple(chunk.model_dump_json() for chunk in chunks)
    children = tuple(
        chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD
    )
    assert len(chunks) == 4
    assert len(children) == 3
    if content.startswith("A"):
        assert tuple(len(child.raw_content) for child in children) == (1500, 1500, 1002)
    assert children[1].raw_content == " " * 1500
    records = tuple(retrieval_record(child) for child in children)
    rows = tuple(
        MilvusChunkRecord.model_validate(record.model_dump(mode="json"))
        for record in records
    )
    assert len(rows) == len(children) == len({row.chunk_id for row in rows})
    assert "".join(row.content for row in rows) == content
    for child, record, row in zip(children, records, rows, strict=True):
        assert (row.chunk_id, row.document_id, row.revision_id, row.parent_id) == (
            child.chunk_id,
            child.document_id,
            child.revision_id,
            child.parent_id,
        )
        assert (
            row.content.encode("utf-8")
            == row.sparse_text.encode("utf-8")
            == child.raw_content.encode("utf-8")
        )
        assert derive_chunk_content_hash(row.content) == child.content_hash
        assert record.structured_metadata.provenance == child.provenance
        assert row.model_dump(mode="json")[
            "structured_metadata"
        ] == record.structured_metadata.model_dump(mode="json")
    assert tuple(chunk.model_dump_json() for chunk in chunks) == snapshots


@pytest.mark.parametrize(
    "content", ["原文 exact\nsecond line", "# Heading\n\n| a | b |", "😀é中文"]
)
def test_child_representation_is_exact_canonical_raw_content(content: str) -> None:
    _, child = text_chunks(content)
    result = builder().build(child)
    assert result.dense_text == result.sparse_text == child.raw_content
    assert (result.chunk_id, result.document_id, result.revision_id) == (
        child.chunk_id,
        child.document_id,
        child.revision_id,
    )
    assert result.structured_metadata.section_path == child.section_path
    assert result.structured_metadata.provenance == child.provenance
    assert result.structured_metadata.source_anchor is None


@pytest.mark.parametrize("mode", list(TableSliceMode))
def test_table_representation_preserves_renderer_and_typed_lineage(
    mode: TableSliceMode,
) -> None:
    table = table_chunk(mode)
    assert table.table_slice is not None
    result = builder().build(table)
    assert (
        result.dense_text == result.sparse_text == render_table_slice(table.table_slice)
    )
    assert result.structured_metadata.provenance == table.provenance
    assert result.structured_metadata.source_anchor == table.table_slice.source_anchor
    assert result.structured_metadata.source_anchor is not None
    assert result.structured_metadata.source_anchor.sheet_name == "Metrics"
    assert all(cell in result.dense_text for cell in table.table_slice.headers)
    assert all(
        cell in result.dense_text for row in table.table_slice.rows for cell in row
    )


def test_context_parent_is_rejected() -> None:
    parent, _ = text_chunks()
    with pytest.raises(ValueError, match="retrievable|TEXT_PARENT"):
        builder().build(parent)


@pytest.mark.parametrize(
    "updates",
    [
        {"raw_content": "forged"},
        {"parent_id": None},
        {"revision_id": "bad"},
        {"chunk_id": "chunk_" + "0" * 64},
        {"unknown": "payload"},
    ],
)
@pytest.mark.parametrize("construct", [False, True])
def test_forged_chunk_is_revalidated(
    updates: dict[str, object], construct: bool
) -> None:
    _, child = text_chunks()
    forged = child.model_copy(update=updates)
    if construct:
        forged = ChunkRecord.model_construct(**(child.model_dump() | updates))
        if "unknown" in updates:
            object.__setattr__(forged, "unknown", updates["unknown"])
    with pytest.raises((ValueError, TypeError)):
        builder().build(forged)


@pytest.mark.parametrize(
    "text,limit", [("abc", 3), ("中文", 6), ("😀", 4), ("界" * 21845, 65535)]
)
def test_text_capacity_counts_utf8_bytes_and_accepts_exact_boundary(
    text: str, limit: int
) -> None:
    _, child = text_chunks(text)
    assert builder(max_text_bytes=limit).build(child).dense_text == text
    with pytest.raises(RepresentationInputLimitError) as caught:
        builder(max_text_bytes=limit - 1).build(child)
    assert caught.value.actual_bytes == len(text.encode("utf-8"))
    assert caught.value.max_bytes == limit - 1


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "4", 65536])
def test_capacity_configuration_is_strict(limit: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        builder(max_text_bytes=limit)


def test_version_drift_and_forged_profile_fail_before_build() -> None:
    with pytest.raises(ValueError, match="representation_version"):
        RepresentationBuilder(
            RepresentationProfileV1(), representation_version="representation-v2"
        )
    with pytest.raises(ValidationError):
        RepresentationBuilder(
            RepresentationProfileV1().model_copy(update={"strategy_name": "prefix"}),
            representation_version="representation-v1",
        )
