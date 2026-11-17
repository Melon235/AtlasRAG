"""Synthetic Stage 9 integration over real PostgreSQL, Milvus, and Redis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
import redis.asyncio as redis
from stage7.helpers import services as answer_services

from atlasrag.application.answer import AnswerRunner
from atlasrag.application.local_evidence import LocalEvidenceRunner
from atlasrag.application.rag_core import RagCoreRunner
from atlasrag.application.retrieval import RetrievalRunner
from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.domain.answer import GenerationResult
from atlasrag.domain.enums import LocalEvidenceStatus, WebExecutionStatus
from atlasrag.domain.evidence import LocalEvidenceResult
from atlasrag.domain.requests import LocalRetrievalRequest, UserTurnRequest
from atlasrag.domain.web import WebEvidenceResult
from atlasrag.graphs.rag_core.subgraph import RagCoreServices
from atlasrag.graphs.retrieval.graph import RetrievalGraphServices
from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.cache.session import SessionQueryCache
from atlasrag.services.rag_core.boundary import AnswerInputBuilder
from atlasrag.services.rag_core.completion import CompletionAssessor
from atlasrag.services.rag_core.query import QueryConstructionResult
from atlasrag.services.rag_core.routing import CorrectiveRoutingService
from benchmarks.alignment.gold import align_gold_section
from benchmarks.contracts.models import (
    BenchmarkQuery,
    CanonicalEvidence,
    GoldSection,
    Modality,
    RankedUnit,
)
from benchmarks.ingestion.accounting import ingestion_summary_from_sync_result
from benchmarks.ingestion.readiness import CorpusReadinessGate, ReadinessStatus
from benchmarks.ingestion.readiness_collector import collect_production_readiness
from benchmarks.metrics.retrieval import evaluate_relaxed_document
from benchmarks.observation.events import JsonlObservationSink
from benchmarks.runners.quality_run import QualityPolicies, run_quality
from integration.stage5_helpers import application as sync_application
from integration.stage5_helpers import request as sync_request
from integration.stage5_helpers import runtime_config
from integration.test_milvus import isolated_milvus as isolated_milvus

pytestmark = pytest.mark.integration

MARKDOWN = "# Guide\n\nAtlasRAG maintenance interval is 30 days.\n"


class FakeQuery:
    async def construct(self, request: UserTurnRequest) -> QueryConstructionResult:
        return QueryConstructionResult(
            request=LocalRetrievalRequest(
                original_query=request.query,
                retrieval_query=request.query,
                filters=request.filters,
            ),
            attempts=1,
            used_fallback=False,
        )


class FakeWeb:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request: object) -> WebEvidenceResult:
        self.calls += 1
        return WebEvidenceResult(execution_status=WebExecutionStatus.EMPTY, evidence=())


class CapturingLocal:
    def __init__(self, runner: LocalEvidenceRunner) -> None:
        self.runner = runner
        self.last: LocalEvidenceResult | None = None

    async def run(self, request: LocalRetrievalRequest) -> LocalEvidenceResult:
        self.last = await self.runner.run(request)
        return self.last


class FakeGeneration:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, input: object) -> GenerationResult:
        self.calls += 1
        return GenerationResult(answer_text="Answer [L1].", used_citation_ids=("L1",))


class CountingRedis:
    def __init__(self, provider: object) -> None:
        self.provider = provider
        self.gets = 0
        self.puts = 0

    async def get_query(self, *args: object, **kwargs: object) -> object:
        self.gets += 1
        return await self.provider.get_query(*args, **kwargs)  # type: ignore[attr-defined]

    async def put_query(self, *args: object, **kwargs: object) -> None:
        self.puts += 1
        await self.provider.put_query(*args, **kwargs)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_synthetic_stage9_real_ingestion_readiness_and_quality(
    postgres_uow_factory: Any,
    postgres_pool: Any,
    isolated_milvus: Any,
    tmp_path: Path,
) -> None:
    (tmp_path / "guide.md").write_text(MARKDOWN, encoding="utf-8")
    runtime = runtime_config()
    app = sync_application(
        postgres_uow_factory, postgres_pool, isolated_milvus.provider, tmp_path
    )
    sync_result = await app.run(sync_request(tmp_path))
    assert sync_result.serving_safety.value == "SAFE_TO_SERVE"
    evidence = await collect_production_readiness(
        postgres_uow_factory,
        runtime=runtime,
        sync_result=sync_result,
        expected_source_paths=("guide.md",),
        expected_pipeline_fingerprint=pipeline_fingerprint(runtime),
    )
    ingestion = ingestion_summary_from_sync_result(sync_result, ("guide.md",))
    readiness = CorpusReadinessGate().assess(
        ingestion, evidence, expected_source_paths=("guide.md",)
    )
    assert readiness.status is ReadinessStatus.READY

    from integration.test_stage6_local_evidence import services as local_services

    local_bundle, _, _ = local_services(postgres_uow_factory, isolated_milvus.provider)
    local = CapturingLocal(LocalEvidenceRunner(local_bundle))

    generation = FakeGeneration()
    answer = AnswerRunner(answer_services(generation))
    web = FakeWeb()
    core = RagCoreRunner(
        RagCoreServices(
            query_construction=cast(Any, FakeQuery()),
            local_evidence=cast(Any, local),
            routing=CorrectiveRoutingService(max_retries=1),
            web_evidence=cast(Any, web),
            answer_input=AnswerInputBuilder(),
            answer=answer,
            completion=CompletionAssessor(),
        )
    )
    redis_client = redis.from_url(
        __import__("os").environ["ATLASRAG_TEST_REDIS_URL"], decode_responses=False
    )
    provider = CountingRedis(RedisCacheProvider(redis_client))
    cache = SessionQueryCache(
        redis=cast(Any, provider),
        uow_factory=postgres_uow_factory,
        runtime_fingerprint=pipeline_fingerprint(runtime),
        ttl_seconds=60,
        policy=QueryCachePolicy(enabled=False),
        clock=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    retrieval = RetrievalRunner(RetrievalGraphServices(cache=cache, rag_core=core))
    query = BenchmarkQuery(
        query_id="q1",
        text="What is the maintenance interval?",
        modality=Modality.TEXT,
        query_type="extractive",
        reference_answer="The maintenance interval is 30 days.",
    )
    observation = JsonlObservationSink(tmp_path / "synthetic" / "trace.jsonl")
    result = await run_quality(
        [query],
        run_id="synthetic-integration",
        repetitions=2,
        policies=QualityPolicies(),
        retrieve=retrieval.run,
        observation=observation,
    )
    assert len(result.cases) == 2
    assert provider.gets == 0 and provider.puts == 0
    assert web.calls == 0 and generation.calls == 2
    assert (tmp_path / "synthetic" / "trace.jsonl").is_file()

    assert local.last is not None
    assert local.last.evidence_status is LocalEvidenceStatus.SUFFICIENT
    selected = local.last.selected_evidence[0]
    unit = RankedUnit(
        identity=selected.context_id, document_id=selected.document_id, rank=1
    )
    metrics = evaluate_relaxed_document([unit], {selected.document_id: 1}, k=1)
    gold = GoldSection(
        gold_section_id="s1",
        doc_id=selected.document_id,
        content=selected.content,
    )
    alignment = align_gold_section(
        query_id="q1",
        gold=gold,
        candidates=(
            CanonicalEvidence(
                evidence_id=selected.context_id,
                document_id=selected.document_id,
                evidence_type=selected.evidence_type.value,
                content=selected.content,
            ),
        ),
    )
    assert alignment.confidence.value == "HIGH"
    artifact = {
        "label": "SYNTHETIC_INTEGRATION",
        "readiness": readiness.status.value,
        "metrics": {"hit@1": metrics.hit, "recall@1": metrics.recall},
        "ranked_units": [
            {
                "identity": unit.identity,
                "document_id": unit.document_id,
                "rank": unit.rank,
            }
        ],
        "gold_documents": [selected.document_id],
        "alignment": alignment.model_dump(mode="json"),
        "cases": [item.observation.model_dump(mode="json") for item in result.cases],
    }
    (tmp_path / "synthetic" / "cases.json").write_text(
        json.dumps(artifact, indent=2), encoding="utf-8"
    )
    replay = json.loads((tmp_path / "synthetic" / "cases.json").read_text())
    replay_unit = RankedUnit(**replay["ranked_units"][0])
    replay_metrics = evaluate_relaxed_document(
        [replay_unit], {replay["gold_documents"][0]: 1}, k=1
    )
    assert replay_metrics.hit == metrics.hit
    await redis_client.aclose()
