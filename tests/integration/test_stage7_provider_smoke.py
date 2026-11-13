"""Explicit live-provider smoke; requires external credentials/services."""

from __future__ import annotations

import os

import httpx
import pytest
from pydantic import HttpUrl, SecretStr
from stage7.helpers import local_evidence, services

from atlasrag.application.answer import AnswerRunner
from atlasrag.application.web_evidence import WebEvidenceRunner
from atlasrag.domain.answer import (
    AnswerInput,
    AnswerLocalEvidence,
    GenerationResult,
)
from atlasrag.domain.enums import (
    LocalAnswerEvidenceStatus,
    WebContentOrigin,
    WebEvidenceRepresentation,
    WebExecutionStatus,
)
from atlasrag.domain.web import PreparedWebSource, WebEvidenceRequest
from atlasrag.graphs.web_evidence.subgraph import WebEvidenceServices
from atlasrag.providers.generation.deepseek import (
    DeepSeekGenerationProvider,
    DeepSeekSummaryProvider,
)
from atlasrag.providers.web.fetch import SafeHttpFetcher
from atlasrag.providers.web.searxng import SearxngSearchProvider
from atlasrag.services.web_evidence.profile import WebEvidenceProfile
from atlasrag.services.web_evidence.service import (
    BatchSummaryWebEvidenceStrategy,
    DirectWebEvidenceStrategy,
    WebPreparationService,
)

pytestmark = pytest.mark.integration


def model_id() -> str:
    return (
        os.environ.get("DEEPSEEK_MODEL_ID")
        or os.environ.get("ATLASRAG_GENERATION_MODEL_ID")
        or "deepseek-chat"
    )


def synthetic_answer_input() -> AnswerInput:
    evidence = local_evidence(
        content="The sample manual specifies a maintenance interval of 30 days."
    )
    return AnswerInput(
        original_query=(
            "What is the maintenance interval specified in the sample manual?"
        ),
        local_evidence_status=LocalAnswerEvidenceStatus.SUFFICIENT,
        local_evidence=(AnswerLocalEvidence(citation_id="L1", evidence=evidence),),
        web_evidence=(),
    )


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
        search = SearxngSearchProvider(base_url=searxng_url, client=client)
        results = await search.search("AtlasRAG synthetic connectivity check")
        assert results
        assert [item.search_rank for item in results] == list(
            range(1, len(results) + 1)
        )
        assert all(item.domain == item.url.host for item in results)

        profile = WebEvidenceProfile(
            max_concurrent_fetches=2,
            fetch_timeout_seconds=3.0,
            total_content_chars=8_000,
        )
        web_services = WebEvidenceServices(
            search=search,
            preparation=WebPreparationService(
                fetcher=SafeHttpFetcher(
                    timeout_seconds=3.0, max_bytes=500_000, max_redirects=3
                ),
                profile=profile,
            ),
            strategy=DirectWebEvidenceStrategy(),
            profile=profile,
        )
        web_result = await WebEvidenceRunner(web_services).run(
            WebEvidenceRequest(original_query="AtlasRAG synthetic connectivity check")
        )
        assert web_result.execution_status in {
            WebExecutionStatus.OK,
            WebExecutionStatus.DEGRADED,
        }
        assert web_result.evidence

        provider = DeepSeekGenerationProvider(
            model_id=model_id(),
            api_key=SecretStr(api_key),
            client=client,
        )
        response, outcome = await AnswerRunner(services(provider)).run(
            synthetic_answer_input()
        )
        assert outcome.value == "SUCCESS"
        assert response.answer_text.strip()
        assert [citation.citation_id for citation in response.citations] == ["L1"]
        assert isinstance(
            GenerationResult(
                answer_text=response.answer_text,
                used_citation_ids=("L1",),
            ),
            GenerationResult,
        )

        summary_sources = (
            PreparedWebSource(
                title="Synthetic manual excerpt",
                url=HttpUrl("https://example.com/manual"),
                domain="example.com",
                search_rank=1,
                content="The sample manual specifies a 30-day maintenance interval.",
                content_origin=WebContentOrigin.FETCHED_PAGE,
            ),
            PreparedWebSource(
                title="Synthetic safety excerpt",
                url=HttpUrl("https://example.org/safety"),
                domain="example.org",
                search_rank=2,
                content="Maintenance must be recorded after each service interval.",
                content_origin=WebContentOrigin.FETCHED_PAGE,
            ),
        )
        summary_provider = DeepSeekSummaryProvider(
            model_id=model_id(),
            api_key=SecretStr(api_key),
            client=client,
            max_summary_chars=500,
            max_tokens=256,
        )
        summary_strategy = BatchSummaryWebEvidenceStrategy(
            provider=summary_provider, max_summary_chars=500
        )
        summary_result = await summary_strategy.build(
            WebEvidenceRequest(original_query="What is the maintenance interval?"),
            summary_sources,
            degraded=False,
        )
        assert summary_result.execution_status is WebExecutionStatus.OK
        assert [item.url for item in summary_result.evidence] == [
            source.url for source in summary_sources
        ]
        assert [item.title for item in summary_result.evidence] == [
            source.title for source in summary_sources
        ]
        assert [item.search_rank for item in summary_result.evidence] == [1, 2]
        assert all(
            item.representation is WebEvidenceRepresentation.SUMMARY
            for item in summary_result.evidence
        )
        print(
            "STAGE7_LIVE_OK",
            {
                "search_results": len(results),
                "web_evidence": len(web_result.evidence),
                "web_status": web_result.execution_status.value,
                "model": model_id(),
                "generation_citations": [
                    citation.citation_id for citation in response.citations
                ],
                "summary_evidence": len(summary_result.evidence),
            },
        )
