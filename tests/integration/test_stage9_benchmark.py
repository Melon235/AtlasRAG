"""Synthetic Stage 9 integration over real PostgreSQL, Milvus, and Redis."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
import redis.asyncio as redis
from stage7.helpers import services as answer_services

from atlasrag.application.answer import AnswerRunner
from atlasrag.application.local_evidence import LocalEvidenceRunner
from atlasrag.application.rag_core import RagCoreRunner
from atlasrag.application.retrieval import RetrievalRunner
from atlasrag.config.fingerprint import pipeline_fingerprint, runtime_fingerprint
from atlasrag.domain.answer import GenerationResult
from atlasrag.domain.enums import LocalEvidenceStatus, WebExecutionStatus
from atlasrag.domain.evidence import LocalEvidenceResult
from atlasrag.domain.observation import ObservationStage, ProductionObservation
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
from atlasrag.services.sync.models import (
    BuildExecutionSummary,
    DeletionSummary,
    GlobalSafetyResult,
)
from atlasrag.services.sync.verification import GlobalKnowledgeVerificationService
from benchmarks.alignment.gold import align_gold_section
from benchmarks.contracts.models import (
    BenchmarkQuery,
    CanonicalEvidence,
    GoldSection,
    Modality,
    RankedUnit,
)
from benchmarks.ingestion.readiness import CorpusReadinessGate, ReadinessStatus
from benchmarks.ingestion.readiness_collector import collect_production_readiness
from benchmarks.ingestion.runner import BenchmarkIngestionRunner
from benchmarks.metrics.retrieval import evaluate_relaxed_document
from benchmarks.observation.events import JsonlObservationSink
from benchmarks.runners.quality_run import QualityPolicies, run_quality
from integration.stage4_helpers import resources as build_resources
from integration.stage4_helpers import runner as build_runner
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


class CaptureObserver:
    def __init__(self) -> None:
        self.events: list[ProductionObservation] = []

    def observe(self, event: ProductionObservation) -> None:
        self.events.append(event)


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
    index = isolated_milvus.provider
    await index.initialize()
    bundle = build_resources(postgres_uow_factory, index)
    backend = build_runner(bundle, postgres_pool)
    ingestion = await BenchmarkIngestionRunner(backend).ingest(
        tmp_path,
        ("guide.md",),
        expected_pipeline_fingerprint=pipeline_fingerprint(runtime),
    )
    assert ingestion.complete
    verification_service = GlobalKnowledgeVerificationService(
        postgres_uow_factory, index, runtime
    )

    async def fresh_verification() -> GlobalSafetyResult:
        return await verification_service.verify(
            deletion_summary=DeletionSummary(),
            build_summary=BuildExecutionSummary(),
        )

    evidence = await collect_production_readiness(
        postgres_uow_factory,
        runtime=runtime,
        verifier=fresh_verification,
        expected_source_paths=("guide.md",),
        expected_pipeline_fingerprint=pipeline_fingerprint(runtime),
        snapshot_identity="synthetic-snapshot-v1",
    )
    readiness = CorpusReadinessGate().assess(
        ingestion,
        evidence,
        expected_source_paths=("guide.md",),
        expected_snapshot_identity="synthetic-snapshot-v1",
    )
    assert readiness.status is ReadinessStatus.READY

    from integration.test_stage6_local_evidence import services as local_services

    local_bundle, _, _ = local_services(postgres_uow_factory, isolated_milvus.provider)
    capture = CaptureObserver()
    local_bundle = replace(local_bundle, observer=capture)
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
            observer=capture,
        )
    )
    redis_client = redis.from_url(
        __import__("os").environ["ATLASRAG_TEST_REDIS_URL"], decode_responses=False
    )
    provider = CountingRedis(RedisCacheProvider(redis_client))
    cache = SessionQueryCache(
        redis=cast(Any, provider),
        uow_factory=postgres_uow_factory,
        runtime_fingerprint=runtime_fingerprint(runtime),
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
    assert local.last is not None
    selected = local.last.selected_evidence[0]
    reranker_event = next(
        event for event in capture.events if event.stage is ObservationStage.RERANKER
    )
    ranked = [
        RankedUnit(
            identity=item.identity,
            document_id=item.document_id or "",
            rank=item.rank,
            score=item.score,
        )
        for item in reranker_event.candidates
        if item.document_id
    ]
    metrics = evaluate_relaxed_document(ranked, {selected.document_id: 1}, k=5)
    observed_stages = {event.stage for event in capture.events}
    assert {
        ObservationStage.DENSE,
        ObservationStage.BM25,
        ObservationStage.FUSION,
        ObservationStage.RERANKER,
        ObservationStage.EVIDENCE,
        ObservationStage.SUFFICIENCY,
        ObservationStage.ROUTING,
        ObservationStage.ANSWER,
    } <= observed_stages
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
                "identity": item.identity,
                "document_id": item.document_id,
                "rank": item.rank,
            }
            for item in ranked
        ],
        "gold_documents": [selected.document_id],
        "alignment": alignment.model_dump(mode="json"),
        "cases": [item.observation.model_dump(mode="json") for item in result.cases],
    }
    (tmp_path / "synthetic" / "cases.json").write_text(
        json.dumps(artifact, indent=2), encoding="utf-8"
    )
    replay = json.loads((tmp_path / "synthetic" / "cases.json").read_text())
    replay_units = [RankedUnit(**item) for item in replay["ranked_units"]]
    replay_metrics = evaluate_relaxed_document(
        replay_units, {replay["gold_documents"][0]: 1}, k=5
    )
    assert replay_metrics.hit == metrics.hit
    await redis_client.aclose()
