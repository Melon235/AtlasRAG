"""Real isolated Milvus admission gate for lossless canonical whitespace."""

import asyncio
import json

import pytest
from stage4.indexing_fixtures import canonical_whitespace_chunks, retrieval_record

from atlasrag.domain.ingestion.chunks import ChunkType, derive_chunk_content_hash
from atlasrag.providers.index.models import MilvusChunkRecord
from integration.test_milvus import SDK_TIMEOUT_SECONDS, IsolatedMilvus
from integration.test_milvus import isolated_milvus as isolated_milvus

pytestmark = pytest.mark.integration


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
