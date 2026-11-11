"""Real pinned Qwen query embedding and BGE reranker smoke on explicit CUDA."""

from __future__ import annotations

import math

import pytest

from atlasrag.providers.embedding.qwen import QWEN_MODEL_ID, QwenEmbeddingProvider
from atlasrag.providers.reranker.bge import (
    BGE_RERANKER_MODEL_ID,
    BGE_RERANKER_REVISION,
    BgeRerankerProvider,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_real_qwen_query_and_bge_reranker() -> None:
    embedding = QwenEmbeddingProvider(device="cuda:0")
    await embedding.initialize()
    query_vector = await embedding.embed_query(
        "Which retrieval components combine dense and lexical evidence?"
    )
    identity = embedding.identity
    assert identity.model_id == QWEN_MODEL_ID
    assert identity.dimension == 1024
    assert len(query_vector) == 1024
    assert all(math.isfinite(value) for value in query_vector)
    assert math.isclose(
        sum(value * value for value in query_vector), 1.0, rel_tol=0.0, abs_tol=1e-3
    )

    reranker = BgeRerankerProvider(device="cuda:0", max_input_tokens=8192)
    await reranker.initialize()
    scores = await reranker.score(
        "Which retrieval components combine dense and lexical evidence?",
        (
            "AtlasRAG combines dense vector retrieval with BM25 lexical retrieval.",
            "Bananas are yellow and grow in tropical climates.",
        ),
    )
    reranker_identity = reranker.identity
    assert reranker_identity.model_id == BGE_RERANKER_MODEL_ID
    assert reranker_identity.model_revision == BGE_RERANKER_REVISION
    assert reranker_identity.device.startswith("cuda")
    assert reranker_identity.dtype == "float16"
    assert len(scores) == 2
    assert all(math.isfinite(score) for score in scores)
    assert scores[0] > scores[1]
    print(
        "model_smoke_ok",
        {
            "query_model": identity.model_id,
            "query_revision": identity.model_revision,
            "query_device": identity.device,
            "query_dimension": identity.dimension,
            "reranker_model": reranker_identity.model_id,
            "reranker_revision": reranker_identity.model_revision,
            "reranker_device": reranker_identity.device,
            "reranker_dtype": reranker_identity.dtype,
        },
    )
