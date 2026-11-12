"""Search-boundary immutable contracts for Stage 6."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from atlasrag.domain.enums import RetrievalChunkType
from atlasrag.providers.index.models import MilvusSearchHit


def test_search_hit_rejects_text_parent_and_child_without_parent() -> None:
    payload = {
        "chunk_id": "chunk_" + "a" * 64,
        "document_id": "doc_" + "b" * 64,
        "revision_id": "rev_" + "c" * 64,
        "chunk_type": "TEXT_PARENT",
        "parent_id": None,
        "content": "parent is not a retrieval unit",
        "score": 0.5,
    }
    with pytest.raises(ValidationError):
        MilvusSearchHit.model_validate(payload)
    payload["chunk_type"] = "TEXT_CHILD"
    with pytest.raises(ValidationError):
        MilvusSearchHit.model_validate(payload)


def test_search_hit_rejects_nonfinite_score() -> None:
    with pytest.raises(ValidationError):
        MilvusSearchHit(
            chunk_id="chunk_" + "a" * 64,
            document_id="doc_" + "b" * 64,
            revision_id="rev_" + "c" * 64,
            chunk_type=RetrievalChunkType.TABLE,
            content="table",
            score=float("nan"),
        )
