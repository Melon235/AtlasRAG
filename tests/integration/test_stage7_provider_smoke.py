"""Explicit live-provider smoke; requires external credentials/services."""

from __future__ import annotations

import os

import httpx
import pytest
from pydantic import SecretStr
from stage7.helpers import answer_input

from atlasrag.providers.generation.deepseek import DeepSeekGenerationProvider
from atlasrag.providers.web.searxng import SearxngSearchProvider
from atlasrag.services.answer.generation_input import GenerationInputBuilder
from atlasrag.services.answer.profile import AnswerProfile

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_live_searxng_and_deepseek_synthetic_smoke() -> None:
    searxng_url = os.environ.get("ATLASRAG_SEARXNG_URL")
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not searxng_url or not api_key:
        pytest.skip(
            "live provider acceptance unavailable: ATLASRAG_SEARXNG_URL and/or "
            "DEEPSEEK_API_KEY not configured"
        )
    async with httpx.AsyncClient(trust_env=False) as client:
        results = await SearxngSearchProvider(
            base_url=searxng_url, client=client
        ).search("AtlasRAG synthetic connectivity check")
        assert results
        provider = DeepSeekGenerationProvider(
            model_id="deepseek-chat",
            api_key=SecretStr(api_key),
            client=client,
        )
        input = GenerationInputBuilder(profile=AnswerProfile()).build(answer_input())
        generation = await provider.generate(input)
        assert generation.answer_text.strip()
