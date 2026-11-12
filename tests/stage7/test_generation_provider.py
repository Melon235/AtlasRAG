"""DeepSeek structured transport/provider contract tests (fake transport)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from atlasrag.domain.errors import ConfigurationError, DependencyUnavailableError
from atlasrag.providers.generation.base import GenerationProtocolError
from atlasrag.providers.generation.deepseek import DeepSeekGenerationProvider
from atlasrag.services.answer.generation_input import GenerationInputBuilder
from atlasrag.services.answer.profile import AnswerProfile
from stage7.helpers import answer_input


def provider(
    handler: Callable[[httpx.Request], httpx.Response], *, retries: int = 0
) -> DeepSeekGenerationProvider:
    return DeepSeekGenerationProvider(
        model_id="deepseek-chat",
        api_key=SecretStr("test-secret"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=retries,
    )


@pytest.mark.asyncio
async def test_structured_generation_parses_and_sends_separated_messages() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answer_text": "Answer [L1].",
                                    "used_citation_ids": ["L1"],
                                }
                            )
                        }
                    }
                ]
            },
        )

    input = GenerationInputBuilder(profile=AnswerProfile()).build(answer_input())
    result = await provider(handler).generate(input)
    assert result.used_citation_ids == ("L1",)
    body = json.loads(seen[0].content)
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"
    assert "UNTRUSTED_EVIDENCE_DATA_JSON" in body["messages"][1]["content"]
    assert body["response_format"] == {"type": "json_object"}
    assert "test-secret" not in json.dumps(body)


@pytest.mark.asyncio
async def test_malformed_structured_output_is_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "not-json"}}]}
        )

    with pytest.raises(GenerationProtocolError):
        await provider(handler).generate(
            GenerationInputBuilder(profile=AnswerProfile()).build(answer_input())
        )


@pytest.mark.asyncio
async def test_auth_and_transport_failures_are_typed() -> None:
    with pytest.raises(ConfigurationError):
        await provider(lambda request: httpx.Response(401)).generate(
            GenerationInputBuilder(profile=AnswerProfile()).build(answer_input())
        )
    with pytest.raises(DependencyUnavailableError):
        await provider(lambda request: httpx.Response(503)).generate(
            GenerationInputBuilder(profile=AnswerProfile()).build(answer_input())
        )


@pytest.mark.asyncio
async def test_batch_summary_provider_preserves_source_order_and_bounds() -> None:
    from atlasrag.domain.enums import WebContentOrigin
    from atlasrag.domain.web import PreparedWebSource
    from atlasrag.providers.generation.deepseek import DeepSeekSummaryProvider
    from stage7.test_web_pipeline import result

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"summaries": ["one", "two"]})}}
                ]
            },
        )

    sources = tuple(
        PreparedWebSource(
            title=f"t{index}",
            url=result(f"https://{index}.example/x", index).url,
            domain=f"{index}.example",
            search_rank=index,
            content="content",
            content_origin=WebContentOrigin.FETCHED_PAGE,
        )
        for index in (1, 2)
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        summaries = await DeepSeekSummaryProvider(
            model_id="deepseek-chat",
            api_key=SecretStr("test-secret"),
            client=client,
        ).summarize("query", sources)
    assert summaries == ("one", "two")
    body = json.loads(seen[0].content)
    assert "https://1.example/x" in body["messages"][1]["content"]


@pytest.mark.asyncio
async def test_malformed_summary_response_is_provider_error() -> None:
    from atlasrag.domain.enums import WebContentOrigin
    from atlasrag.domain.errors import ProviderResponseError
    from atlasrag.domain.web import PreparedWebSource
    from atlasrag.providers.generation.deepseek import DeepSeekSummaryProvider
    from stage7.test_web_pipeline import result

    source = PreparedWebSource(
        title="t",
        url=result("https://a.example/x", 1).url,
        domain="a.example",
        search_rank=1,
        content="content",
        content_origin=WebContentOrigin.FETCHED_PAGE,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"summaries": []})}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderResponseError):
            await DeepSeekSummaryProvider(
                model_id="deepseek-chat",
                api_key=SecretStr("test"),
                client=client,
            ).summarize("q", (source,))
