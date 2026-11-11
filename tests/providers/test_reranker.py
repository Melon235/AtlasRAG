"""Pinned reranker provider failure normalization tests (no model load)."""

from __future__ import annotations

from typing import Any, cast

import pytest

from atlasrag.providers.reranker.base import (
    RerankerInputLimitError,
    RerankerUnavailableError,
)
from atlasrag.providers.reranker.bge import BgeRerankerProvider


@pytest.mark.asyncio
async def test_inference_runtime_failure_is_wrapped_as_typed_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = BgeRerankerProvider(device="cuda:0")
    provider._loaded = cast(Any, object())
    monkeypatch.setattr(
        provider,
        "_score_sync",
        lambda query, passages: (_ for _ in ()).throw(
            RuntimeError("CUDA out of memory")
        ),
    )
    with pytest.raises(RerankerUnavailableError):
        await provider.score("q", ("passage",))


@pytest.mark.asyncio
async def test_input_limit_is_not_masked_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = BgeRerankerProvider(device="cuda:0")
    provider._loaded = cast(Any, object())
    monkeypatch.setattr(
        provider,
        "_score_sync",
        lambda query, passages: (_ for _ in ()).throw(RerankerInputLimitError()),
    )
    with pytest.raises(RerankerInputLimitError):
        await provider.score("q", ("passage",))
