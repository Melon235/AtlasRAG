"""Real isolated Milvus admission gate for lossless canonical whitespace."""

import asyncio
import json
from contextlib import aclosing
from pathlib import Path

import pytest
from providers.test_milvus_provider import milvus_record
from pydantic import ValidationError
from stage3.test_candidate_services import NOW, _document, _stored_document
from stage4.candidate_fakes import Embedding, candidate
from stage4.indexing_fixtures import (
    canonical_whitespace_chunks,
    retrieval_record,
    table_chunk,
)

from atlasrag.domain.ingestion.chunks import (
    ChunkType,
    TableSliceMode,
    derive_chunk_content_hash,
)
from atlasrag.providers.index.models import MilvusChunkRecord
from atlasrag.repositories.postgres.records import IngestionStatus
from atlasrag.services.indexing.verification import CandidateVerificationAttempt
from atlasrag.services.ingestion.chunking import ChunkingProfile, build_chunks
from atlasrag.services.ingestion.mapping import (
    map_chunks_to_stored,
    map_elements_to_stored,
)
from integration.test_milvus import SDK_TIMEOUT_SECONDS, IsolatedMilvus
from integration.test_milvus import isolated_milvus as isolated_milvus

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_real_milvus_preserves_table_slice_headers_rows_sheet_and_anchor(
    isolated_milvus: IsolatedMilvus,
) -> None:
    """Non-null sheet metadata supplements the frozen Markdown TABLE E2E case."""
    chunk = table_chunk(TableSliceMode.ROW_GROUP)
    record = MilvusChunkRecord.model_validate(
        retrieval_record(chunk).model_dump(mode="json")
    )
    index = isolated_milvus.provider
    await index.initialize()
    await index.insert_revision_batch(
        [record], document_id=record.document_id, revision_id=record.revision_id
    )
    await index.wait_revision_visible(record.document_id, record.revision_id)
    actual = [
        row.model_dump(mode="json")
        async for row in index.iter_revision_projection(
            record.document_id, record.revision_id, batch_size=1
        )
    ]
    expected = record.model_dump(mode="json", exclude={"dense_vector"})
    assert actual == [expected]
    assert chunk.table_slice is not None
    assert chunk.table_slice.headers == ("Name", "值")
    assert chunk.table_slice.rows == (("Alpha", "1"), ("Beta", "2"))
    assert actual[0]["content"] == chunk.raw_content
    metadata = actual[0]["structured_metadata"]
    assert metadata["provenance"]["row_start"] == 5
    assert metadata["provenance"]["row_end"] == 7
    assert metadata["provenance"]["sheet_name"] == "Metrics"
    assert metadata["source_anchor"]["sheet_name"] == "Metrics"
    assert metadata["source_anchor"]["cell_range"] == "A1:B9"
    # Whitespace admission is not permission to admit an empty text record.
    for field in ("content", "sparse_text"):
        with pytest.raises(ValidationError):
            MilvusChunkRecord.model_validate(
                {**record.model_dump(mode="json"), field: ""}
            )
    assert await index.count_revision(record.document_id, record.revision_id) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    ["A" + " " * 4000 + "B", "界😀A" + " " * 4000 + "Bé\n"],
    ids=["ascii", "mixed-unicode"],
)
async def test_whitespace_children_have_exact_strong_milvus_projection(
    isolated_milvus: IsolatedMilvus,
    content: str,
) -> None:
    chunks = canonical_whitespace_chunks(content)
    children = tuple(
        chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD
    )
    assert len(children) == 3
    assert children[1].raw_content == " " * 1500
    records = tuple(
        MilvusChunkRecord.model_validate(
            retrieval_record(child).model_dump(mode="json")
        )
        for child in children
    )
    resource = isolated_milvus
    await resource.provider.initialize()
    await resource.provider.replace_revision_set(
        records,
        document_id=children[0].document_id,
        revision_id=children[0].revision_id,
    )
    fields = [
        "chunk_id",
        "document_id",
        "revision_id",
        "parent_id",
        "chunk_type",
        "source_type",
        "file_name",
        "content",
        "sparse_text",
        "structured_metadata",
    ]
    projection = await asyncio.to_thread(
        resource.client.query,
        collection_name=resource.collection_name,
        filter=f"document_id == {json.dumps(children[0].document_id)} and revision_id == {json.dumps(children[0].revision_id)}",
        output_fields=fields,
        limit=100,
        consistency_level="Strong",
        timeout=SDK_TIMEOUT_SECONDS,
    )
    assert len(projection) == len(records)
    actual = {row["chunk_id"]: row for row in projection}
    assert len(actual) == len(projection)
    expected = {
        record.chunk_id: {
            field: record.model_dump(mode="json")[field] for field in fields
        }
        for record in records
    }
    assert actual == expected
    for child in children:
        row = actual[child.chunk_id]
        assert row["content"].encode("utf-8") == child.raw_content.encode("utf-8")
        assert row["sparse_text"].encode("utf-8") == child.raw_content.encode("utf-8")
        assert derive_chunk_content_hash(row["content"]) == child.content_hash
    assert "".join(actual[child.chunk_id]["content"] for child in children) == content


@pytest.mark.asyncio
async def test_candidate_batches_append_without_replacing_last_good(
    isolated_milvus: IsolatedMilvus,
) -> None:
    index = isolated_milvus.provider
    await index.initialize()
    original = milvus_record(chunk_id="r1-child", revision_id="r1")
    await index.replace_revision_set([original], document_id="doc-1", revision_id="r1")
    await index.delete_revision("doc-1", "r2")
    records = [
        milvus_record(chunk_id="r2-a", revision_id="r2", content=" " * 1500),
        milvus_record(chunk_id="r2-b", revision_id="r2"),
    ]
    for record in records:
        await index.insert_revision_batch(
            [record], document_id="doc-1", revision_id="r2"
        )
    await index.wait_revision_visible("doc-1", "r2")
    for revision, expected_records in (("r1", [original]), ("r2", records)):
        actual = [
            row.model_dump(mode="json")
            async for row in index.iter_revision_projection(
                "doc-1", revision, batch_size=1
            )
        ]
        expected = []
        for record in expected_records:
            row = record.model_dump(mode="json")
            row.pop("dense_vector")
            expected.append(row)
        assert sorted(actual, key=lambda row: row["chunk_id"]) == sorted(
            expected, key=lambda row: row["chunk_id"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    ["A" + " " * 4000 + "B", "界😀A" + " " * 4000 + "Bé\n"],
    ids=["ascii", "mixed-unicode"],
)
async def test_formal_verifier_accepts_real_milvus_exact_whitespace_candidate(
    isolated_milvus: IsolatedMilvus, tmp_path: Path, content: str
) -> None:
    """T5 real Milvus gate with durable fake PG; T9 owns real PG + Milvus E2E."""
    factory, context, runtime = candidate(tmp_path)
    document = _document(context, content)
    chunks = build_chunks(document)  # Frozen Stage 3 default splitter, unchanged.
    children = tuple(c for c in chunks if c.chunk_type is ChunkType.TEXT_CHILD)
    assert len(children) == 3 and children[1].raw_content == " " * 1500
    scope = context.document_id, context.revision_id
    factory.database.documents[context.document_id] = _stored_document(
        context,
        status=IngestionStatus.CHUNKED,
        current=True,
        building_document=document,
    )
    factory.database.elements[scope] = map_elements_to_stored(
        document.elements, created_at=NOW
    )
    factory.database.chunks[scope] = map_chunks_to_stored(
        chunks,
        chunker_behavior_version=runtime.part_i.chunker_behavior_version,
        profile_fingerprint=ChunkingProfile().profile_fingerprint,
        created_at=NOW,
    )
    embedding = Embedding(factory, runtime)
    attempt = CandidateVerificationAttempt(
        factory, context, config=runtime, index=isolated_milvus.provider
    )
    await attempt.index(embedding=embedding, clock=lambda: NOW, batch_size=2)
    assert " " * 1500 in [text for batch in embedding.inputs for text in batch]
    embedding.error = AssertionError("formal verification must not re-embed")
    await attempt.verify(batch_size=1)
    async with factory() as unit:
        verified = await attempt.consume_verified(unit, context)
        assert verified.chunks == chunks
    async with aclosing(
        isolated_milvus.provider.iter_revision_projection(*scope, batch_size=1)
    ) as rows:
        observed = {row.chunk_id: row async for row in rows}
    assert set(observed) == {child.chunk_id for child in children}
    for child in children:
        row = observed[child.chunk_id]
        assert row.content.encode("utf-8") == child.raw_content.encode("utf-8")
        assert row.sparse_text == row.content
        assert derive_chunk_content_hash(row.content) == child.content_hash
