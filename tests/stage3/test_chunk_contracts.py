"""Canonical chunk, structural-lineage, and identity contract tests."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from atlasrag._canonical import canonical_sha256
from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.ingestion.chunks import (
    ChunkingStrategy,
    ChunkRecord,
    ChunkType,
    ElementSpan,
    RetrievalRole,
    TableProvenance,
    TableSlice,
    TableSliceMode,
    TextChildProvenance,
    TextParentProvenance,
    render_table_slice,
)
from atlasrag.domain.ingestion.source import derive_document_id
from atlasrag.services.ingestion.identity import (
    build_chunk,
    chunk_content_hash,
    chunk_id,
)

DOCUMENT_ID = derive_document_id("docs/guide.md")
REVISION_ID = "rev_" + "a" * 64
OTHER_REVISION_ID = "rev_" + "b" * 64
ELEMENT_ID = "elem_" + "1" * 64
SECOND_ELEMENT_ID = "elem_" + "2" * 64


def parent_provenance() -> TextParentProvenance:
    return TextParentProvenance(
        source_spans=(
            ElementSpan(
                element_id=ELEMENT_ID,
                element_order=0,
                char_start=0,
                char_end=12,
            ),
        )
    )


def parent_chunk(**overrides: Any) -> ChunkRecord:
    values: dict[str, object] = {
        "document_id": DOCUMENT_ID,
        "revision_id": REVISION_ID,
        "chunk_type": ChunkType.TEXT_PARENT,
        "retrieval_role": RetrievalRole.CONTEXT_ONLY,
        "chunking_strategy": ChunkingStrategy.STRUCTURE,
        "raw_content": "Parent text.",
        "parent_id": None,
        "order": 0,
        "section_path": ("Overview",),
        "provenance": parent_provenance(),
        "table_slice": None,
    }
    values.update(overrides)
    return build_chunk(**values)  # type: ignore[arg-type]


def child_chunk(parent: ChunkRecord, **overrides: Any) -> ChunkRecord:
    values: dict[str, object] = {
        "document_id": parent.document_id,
        "revision_id": parent.revision_id,
        "chunk_type": ChunkType.TEXT_CHILD,
        "retrieval_role": RetrievalRole.RETRIEVABLE,
        "chunking_strategy": ChunkingStrategy.RECURSIVE,
        "raw_content": "Parent",
        "parent_id": parent.chunk_id,
        "order": 1,
        "section_path": parent.section_path,
        "provenance": TextChildProvenance(
            source_spans=(
                ElementSpan(
                    element_id=ELEMENT_ID,
                    element_order=0,
                    char_start=0,
                    char_end=6,
                ),
            ),
            parent_char_start=0,
            parent_char_end=6,
        ),
        "table_slice": None,
    }
    values.update(overrides)
    return build_chunk(**values)  # type: ignore[arg-type]


def table_slice() -> TableSlice:
    anchor = SourceAnchor(sheet_name="Data", cell_range="A1:B3")
    return TableSlice(
        mode=TableSliceMode.FULL,
        source_element_id=ELEMENT_ID,
        headers=("Name", "Value"),
        rows=(("Alpha", "1"), ("Beta", "2")),
        row_start=0,
        row_end=2,
        caption="Summary",
        sheet_name="Data",
        source_anchor=anchor,
    )


def table_chunk(**overrides: Any) -> ChunkRecord:
    sliced = table_slice()
    values: dict[str, object] = {
        "document_id": DOCUMENT_ID,
        "revision_id": REVISION_ID,
        "chunk_type": ChunkType.TABLE,
        "retrieval_role": RetrievalRole.RETRIEVABLE,
        "chunking_strategy": ChunkingStrategy.TABLE,
        "raw_content": render_table_slice(sliced),
        "parent_id": None,
        "order": 0,
        "section_path": ("Data",),
        "provenance": TableProvenance(
            source_element_id=ELEMENT_ID,
            source_element_order=0,
            row_start=0,
            row_end=2,
            sheet_name="Data",
        ),
        "table_slice": sliced,
    }
    values.update(overrides)
    return build_chunk(**values)  # type: ignore[arg-type]


def test_element_span_and_text_provenance_are_strict_ordered_ranges() -> None:
    first = ElementSpan(
        element_id=ELEMENT_ID,
        element_order=3,
        char_start=0,
        char_end=5,
    )
    second = ElementSpan(
        element_id=ELEMENT_ID,
        element_order=3,
        char_start=5,
        char_end=9,
    )
    later = ElementSpan(
        element_id=SECOND_ELEMENT_ID,
        element_order=4,
        char_start=0,
        char_end=2,
    )

    provenance = TextParentProvenance(source_spans=(first, second, later))

    assert provenance.source_spans == (first, second, later)
    assert (
        TextParentProvenance.model_validate_json(provenance.model_dump_json())
        == provenance
    )
    with pytest.raises(ValidationError, match="nonempty"):
        TextParentProvenance(source_spans=())
    with pytest.raises(ValidationError, match="ordered|overlap"):
        TextParentProvenance(source_spans=(second, first))
    with pytest.raises(ValidationError, match="ordered|overlap"):
        TextParentProvenance(
            source_spans=(
                first,
                ElementSpan(
                    element_id=ELEMENT_ID,
                    element_order=3,
                    char_start=4,
                    char_end=8,
                ),
            )
        )
    with pytest.raises(ValidationError, match="element_id|element_order"):
        TextParentProvenance(
            source_spans=(
                first,
                ElementSpan(
                    element_id=ELEMENT_ID,
                    element_order=4,
                    char_start=0,
                    char_end=2,
                ),
            )
        )
    with pytest.raises(ValidationError, match="char_end"):
        ElementSpan(
            element_id=ELEMENT_ID,
            element_order=0,
            char_start=2,
            char_end=2,
        )
    with pytest.raises(ValidationError):
        ElementSpan.model_validate(
            {
                "element_id": ELEMENT_ID,
                "element_order": 0,
                "char_start": "0",
                "char_end": 1,
            }
        )


def test_child_and_table_provenance_require_nonempty_bounded_ranges() -> None:
    child = TextChildProvenance(
        source_spans=(
            ElementSpan(
                element_id=ELEMENT_ID,
                element_order=0,
                char_start=1,
                char_end=4,
            ),
        ),
        parent_char_start=2,
        parent_char_end=5,
    )
    table = TableProvenance(
        source_element_id=ELEMENT_ID,
        source_element_order=7,
        row_start=2,
        row_end=5,
        sheet_name="Data",
    )

    assert child.parent_char_end - child.parent_char_start == 3
    assert table.row_end - table.row_start == 3
    with pytest.raises(ValidationError, match="parent_char_end"):
        TextChildProvenance(
            source_spans=child.source_spans,
            parent_char_start=5,
            parent_char_end=5,
        )
    with pytest.raises(ValidationError, match="row_end"):
        TableProvenance(
            source_element_id=ELEMENT_ID,
            source_element_order=0,
            row_start=1,
            row_end=1,
        )


def test_table_slice_is_rectangular_typed_and_preserves_lineage() -> None:
    sliced = table_slice()

    assert sliced.rows == (("Alpha", "1"), ("Beta", "2"))
    assert sliced.row_end - sliced.row_start == len(sliced.rows)
    assert "Summary" in render_table_slice(sliced)
    assert TableSlice.model_validate_json(sliced.model_dump_json()) == sliced
    with pytest.raises(ValidationError, match="rows|row"):
        TableSlice.model_validate({**sliced.model_dump(), "row_end": 1})
    with pytest.raises(ValidationError, match="rectangular"):
        TableSlice.model_validate({**sliced.model_dump(), "rows": (("only-one",),)})
    with pytest.raises(ValidationError, match="FULL"):
        TableSlice.model_validate({**sliced.model_dump(), "row_start": 1, "row_end": 3})


@pytest.mark.parametrize(
    ("factory", "field", "value"),
    [
        (parent_chunk, "retrieval_role", RetrievalRole.RETRIEVABLE),
        (parent_chunk, "chunking_strategy", ChunkingStrategy.RECURSIVE),
        (parent_chunk, "parent_id", "chunk_" + "9" * 64),
        (child_chunk, "retrieval_role", RetrievalRole.CONTEXT_ONLY),
        (child_chunk, "chunking_strategy", ChunkingStrategy.STRUCTURE),
        (child_chunk, "parent_id", None),
        (table_chunk, "retrieval_role", RetrievalRole.CONTEXT_ONLY),
        (table_chunk, "chunking_strategy", ChunkingStrategy.STRUCTURE),
        (table_chunk, "parent_id", "chunk_" + "9" * 64),
    ],
)
def test_chunk_record_enforces_exact_role_strategy_and_parent_invariants(
    factory: Any, field: str, value: object
) -> None:
    parent = parent_chunk()

    with pytest.raises(ValidationError):
        if factory is child_chunk:
            factory(parent, **{field: value})
        else:
            factory(**{field: value})


def test_chunk_record_enforces_typed_provenance_and_table_slice_invariants() -> None:
    parent = parent_chunk()
    child = child_chunk(parent)
    table = table_chunk()

    with pytest.raises(ValidationError, match="provenance"):
        parent_chunk(provenance=child.provenance)
    with pytest.raises(ValidationError, match="table_slice"):
        parent_chunk(table_slice=table.table_slice)
    with pytest.raises(ValidationError, match="provenance"):
        table_chunk(provenance=parent.provenance)
    with pytest.raises(ValidationError, match="table_slice"):
        table_chunk(table_slice=None)

    wrong_slice = TableSlice.model_validate(
        {
            **table.table_slice.model_dump(),  # type: ignore[union-attr]
            "source_element_id": SECOND_ELEMENT_ID,
        }
    )
    with pytest.raises(ValidationError, match="lineage"):
        table_chunk(
            table_slice=wrong_slice, raw_content=render_table_slice(wrong_slice)
        )


def test_chunk_hash_and_id_use_the_approved_canonical_payload() -> None:
    provenance = parent_provenance()
    content_hash = chunk_content_hash("Parent text.")

    actual_id = chunk_id(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        chunk_type=ChunkType.TEXT_PARENT,
        chunking_strategy=ChunkingStrategy.STRUCTURE,
        provenance=provenance,
        content_hash=content_hash,
    )

    assert content_hash == canonical_sha256("Parent text.")
    assert actual_id == "chunk_" + canonical_sha256(
        {
            "document_id": DOCUMENT_ID,
            "revision_id": REVISION_ID,
            "chunk_type": "TEXT_PARENT",
            "chunking_strategy": "STRUCTURE",
            "provenance": provenance.model_dump(mode="json"),
            "content_hash": content_hash,
        }
    )
    assert actual_id == parent_chunk().chunk_id


def test_chunk_identity_revalidates_constructed_typed_provenance() -> None:
    forged = TextParentProvenance.model_construct(source_spans=())

    with pytest.raises(ValidationError, match="source_spans"):
        chunk_id(
            document_id=DOCUMENT_ID,
            revision_id=REVISION_ID,
            chunk_type=ChunkType.TEXT_PARENT,
            chunking_strategy=ChunkingStrategy.STRUCTURE,
            provenance=forged,
            content_hash=chunk_content_hash("Parent text."),
        )


def test_chunk_record_rejects_forged_content_hash_and_identity() -> None:
    chunk = parent_chunk()

    with pytest.raises(ValidationError, match="content_hash"):
        ChunkRecord.model_validate({**chunk.model_dump(), "content_hash": "0" * 64})
    with pytest.raises(ValidationError, match="chunk_id"):
        ChunkRecord.model_validate(
            {**chunk.model_dump(), "chunk_id": "chunk_" + "0" * 64}
        )
    with pytest.raises(ValidationError, match="chunk_id"):
        ChunkRecord.model_validate(
            {**chunk.model_dump(), "revision_id": OTHER_REVISION_ID}
        )


def test_text_chunk_source_spans_cannot_exceed_rendered_content() -> None:
    oversized_span = ElementSpan(
        element_id=ELEMENT_ID,
        element_order=0,
        char_start=0,
        char_end=100,
    )

    with pytest.raises(ValidationError, match="source_spans"):
        parent_chunk(provenance=TextParentProvenance(source_spans=(oversized_span,)))

    parent = parent_chunk()
    with pytest.raises(ValidationError, match="source_spans"):
        child_chunk(
            parent,
            provenance=TextChildProvenance(
                source_spans=(oversized_span,),
                parent_char_start=0,
                parent_char_end=6,
            ),
        )


def test_text_chunks_accept_nonempty_source_backed_whitespace_but_reject_empty() -> (
    None
):
    whitespace_span = ElementSpan(
        element_id=ELEMENT_ID,
        element_order=0,
        char_start=2,
        char_end=5,
    )
    parent = parent_chunk(
        raw_content="   ",
        provenance=TextParentProvenance(source_spans=(whitespace_span,)),
    )
    child = child_chunk(
        parent,
        raw_content="   ",
        provenance=TextChildProvenance(
            source_spans=(whitespace_span,),
            parent_char_start=0,
            parent_char_end=3,
        ),
    )

    assert parent.raw_content == child.raw_content == "   "
    assert parent.content_hash == child.content_hash == chunk_content_hash("   ")

    empty_span = ElementSpan(
        element_id=ELEMENT_ID,
        element_order=0,
        char_start=0,
        char_end=1,
    )
    with pytest.raises(ValueError, match="nonempty|empty"):
        parent_chunk(
            raw_content="",
            provenance=TextParentProvenance(source_spans=(empty_span,)),
        )


def test_all_chunk_kinds_are_immutable_stable_json_contracts() -> None:
    parent = parent_chunk()
    chunks = (parent, child_chunk(parent), table_chunk())

    for chunk in chunks:
        restored = ChunkRecord.model_validate_json(chunk.model_dump_json())
        assert restored == chunk
        with pytest.raises(ValidationError, match="frozen"):
            chunk.order = 99


def test_chunk_domain_has_no_retrieval_runtime_or_experiment_fields() -> None:
    forbidden_fragments = {
        "vector",
        "embedding",
        "score",
        "query",
        "session",
        "citation",
        "benchmark",
        "experiment",
        "provider",
        "milvus",
    }
    model_types = (
        ElementSpan,
        TextParentProvenance,
        TextChildProvenance,
        TableProvenance,
        TableSlice,
        ChunkRecord,
    )

    for model_type in model_types:
        lowered = {field.casefold() for field in model_type.model_fields}
        assert not any(
            fragment in field for field in lowered for fragment in forbidden_fragments
        )
