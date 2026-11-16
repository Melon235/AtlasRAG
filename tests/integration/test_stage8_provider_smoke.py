"""Explicit live DeepSeek Query Construction smoke (synthetic only)."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from atlasrag.domain.requests import UserTurnRequest
from atlasrag.providers.generation.deepseek import DeepSeekQueryRewriteProvider
from atlasrag.services.rag_core.query import QueryConstructionService

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_live_query_construction_synthetic_smoke() -> None:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        pytest.skip("live query rewrite unavailable: DEEPSEEK_API_KEY not configured")
    model = os.environ.get("DEEPSEEK_MODEL_ID", "deepseek-chat")
    async with httpx.AsyncClient(trust_env=False) as client:
        provider = DeepSeekQueryRewriteProvider(
            model_id=model,
            api_key=SecretStr(api_key),
            client=client,
        )
        result = await QueryConstructionService(
            provider=provider,
            clock=lambda: datetime(2026, 11, 14, tzinfo=UTC),
        ).construct(
            UserTurnRequest(
                session_id="stage8-live",
                query="查一下 ERR-104 在 manual-v2.3.pdf 中的处理方式",
            )
        )
    assert (
        result.request.original_query
        == "查一下 ERR-104 在 manual-v2.3.pdf 中的处理方式"
    )
    assert "err-104" in result.request.retrieval_query.casefold()
    assert "manual-v2.3.pdf" in result.request.retrieval_query.casefold()
    print("STAGE8_QUERY_LIVE_OK", {"model": model, "attempts": result.attempts})
