"""Explicit real CUDA Qwen hard gate; never collected into default execution."""

from __future__ import annotations

import math

import pytest
from stage4.indexing_fixtures import canonical_whitespace_chunks

from atlasrag.domain.indexing.models import RepresentationProfileV1
from atlasrag.domain.ingestion.chunks import ChunkType
from atlasrag.providers.embedding.profile import EmbeddingExecutionProfile
from atlasrag.providers.embedding.qwen import (
    QWEN_MODEL_ID,
    QWEN_REVISION,
    QwenEmbeddingProvider,
)
from atlasrag.services.indexing.representation import RepresentationBuilder

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_real_qwen_cuda_document_batch_and_canonical_whitespace() -> None:
    provider = QwenEmbeddingProvider(
        device="cuda:0", profile=EmbeddingExecutionProfile(batch_size=3)
    )
    # Integration collection already requires ATLASRAG_RUN_INTEGRATION=1.
    # Downloads are authorized here only; regular initialize is cache-only.
    await provider.initialize(allow_download=True)
    identity = provider.identity
    assert identity.model_id == identity.tokenizer_id == QWEN_MODEL_ID
    assert identity.model_revision == identity.tokenizer_revision == QWEN_REVISION
    assert identity.dimension == 1024
    assert identity.device == "cuda:0"
    # Inspect the genuinely loaded SDK object, not requested configuration.
    loaded = provider._loaded
    assert loaded is not None
    assert all(
        str(p.device) == "cuda:0" and str(p.dtype) == "torch.float16"
        for p in loaded.model.parameters()
    )
    assert loaded.model.config._attn_implementation == "sdpa"
    children = [
        chunk
        for chunk in canonical_whitespace_chunks()
        if chunk.chunk_type is ChunkType.TEXT_CHILD and chunk.raw_content == " " * 1500
    ]
    assert children, (
        "frozen Stage 3 must emit the whitespace Child used in this hard gate"
    )
    builder = RepresentationBuilder(
        RepresentationProfileV1(), representation_version="representation-v1"
    )
    whitespace = builder.build(children[0]).dense_text
    assert whitespace == " " * 1500
    texts = [
        "AtlasRAG stores canonical document lineage.",
        "A short second document.",
        whitespace,
    ]
    batched = await provider.embed(texts)
    assert len(batched) == 3
    for text, row in zip(texts, batched, strict=True):
        assert len(row) == 1024 and all(math.isfinite(value) for value in row)
        assert math.sqrt(sum(value * value for value in row)) == pytest.approx(
            1, abs=1e-5
        )
        single = (await provider.embed([text]))[0]
        # FP16 SDPA can differ slightly with padded batch geometry.
        assert sum(a * b for a, b in zip(row, single, strict=True)) > 0.999
    assert sum(a * b for a, b in zip(batched[0], batched[1], strict=True)) < 0.999
    print(
        f"model={identity.model_id} revision={identity.model_revision} tokenizer_revision={identity.tokenizer_revision} device={identity.device} dtype=float16 attention=sdpa dimension={identity.dimension} batch=3 canonical_whitespace=PASS order=PASS finite=PASS"
    )
