from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

import pytest

from atlasrag.domain.ingestion.source import derive_document_id, derive_revision_id
from atlasrag.domain.results import FinalResponse
from atlasrag.graphs.indexing.contracts import BuildRequest, BuildResult
from benchmarks.contracts.models import BenchmarkQuery, Modality
from benchmarks.ingestion.readiness import (
    CorpusReadinessGate,
    ProductionReadinessEvidence,
    ReadinessStatus,
)
from benchmarks.ingestion.runner import BenchmarkIngestionRunner
from benchmarks.runners.quality_run import QualityPolicies, run_quality


class FakeBuild:
    def __init__(self, outcomes: dict[str, str] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.requests: list[BuildRequest] = []

    async def build(self, request: BuildRequest) -> BuildResult:
        self.requests.append(request)
        relative = request.source.relative_source_path
        document_id = derive_document_id(relative)
        return BuildResult(
            document_id=document_id,
            revision_id=derive_revision_id(document_id, "a" * 64, "a" * 64),
            outcome=cast(
                Literal["READY", "FAILED", "SOURCE_MUTATED"],
                self.outcomes.get(relative, "READY"),
            ),
        )


@pytest.mark.asyncio
async def test_ingestion_exact_set_and_readiness_gate(tmp_path: Path) -> None:
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text("content", encoding="utf-8")
    backend = FakeBuild()
    summary = await BenchmarkIngestionRunner(backend).ingest(
        tmp_path, ("a.md", "b.md"), expected_pipeline_fingerprint="a" * 64
    )
    assert summary.complete and len(summary.records) == 2
    assert len(backend.requests) == 2
    evidence = ProductionReadinessEvidence(
        canonical_lineage_verified=True,
        milvus_exact_set_verified=True,
        serving_safety="SAFE_TO_SERVE",
        published_source_paths=("a.md", "b.md"),
    )
    gate = CorpusReadinessGate()
    ready = gate.assess(summary, evidence, expected_source_paths=("a.md", "b.md"))
    assert ready.status is ReadinessStatus.READY
    bad = gate.assess(
        summary,
        ProductionReadinessEvidence(
            canonical_lineage_verified=False,
            milvus_exact_set_verified=False,
            serving_safety="UNSAFE_TO_SERVE",
            published_source_paths=("a.md",),
        ),
        expected_source_paths=("a.md", "b.md"),
    )
    assert bad.status is ReadinessStatus.PARTIAL
    assert "STAGE5_NOT_SAFE_TO_SERVE" in bad.reasons
    assert "PUBLISHED_SOURCE_SET_MISMATCH" in bad.reasons


@pytest.mark.asyncio
async def test_quality_run_sessions_and_policy_rejection() -> None:
    seen: list[str] = []

    async def retrieve(request: object) -> FinalResponse:
        seen.append(request.session_id)  # type: ignore[attr-defined]
        return FinalResponse(answer_text="answer", citations=())

    query = BenchmarkQuery(
        query_id="q1",
        text="question",
        modality=Modality.TEXT,
        query_type="extractive",
        reference_answer="gold",
    )
    result = await run_quality(
        [query],
        run_id="run",
        repetitions=2,
        policies=QualityPolicies(),
        retrieve=retrieve,
    )
    assert len(result.cases) == 2
    assert seen == ["benchmark:run:q1:0", "benchmark:run:q1:1"]
    with pytest.raises(ValueError, match="require Web"):
        QualityPolicies(web_enabled=True).validate()


@pytest.mark.asyncio
async def test_observation_sink_records_generic_events(tmp_path: Path) -> None:
    from benchmarks.observation.events import JsonlObservationSink

    async def retrieve(request: object) -> FinalResponse:
        return FinalResponse(answer_text="answer", citations=())

    sink = JsonlObservationSink(tmp_path / "trace.jsonl")
    query = BenchmarkQuery(
        query_id="q1",
        text="question",
        modality=Modality.TEXT,
        query_type="extractive",
    )
    await run_quality(
        [query],
        run_id="run",
        repetitions=1,
        policies=QualityPolicies(),
        retrieve=retrieve,
        observation=sink,
    )
    lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "RETRIEVAL_REQUEST" in lines[0] and "FINAL_RESPONSE" in lines[1]


@pytest.mark.asyncio
async def test_ingestion_failure_accounting_and_source_set(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a", encoding="utf-8")

    class Broken:
        async def build(self, request: BuildRequest) -> BuildResult:
            raise RuntimeError("boom")

    summary = await BenchmarkIngestionRunner(Broken()).ingest(
        tmp_path, ("a.md",), expected_pipeline_fingerprint="a" * 64
    )
    assert not summary.complete
    assert summary.records[0].outcome == "TECHNICAL_FAILURE"
    assert summary.records[0].failure_reason == "RuntimeError"
    mismatch = await BenchmarkIngestionRunner(FakeBuild()).ingest(
        tmp_path, ("a.md", "missing.md"), expected_pipeline_fingerprint="a" * 64
    )
    assert mismatch.failure_reasons == ("SOURCE_SET_MISMATCH",)


@pytest.mark.asyncio
async def test_readiness_collector_uses_real_records_and_pipeline() -> None:
    from types import SimpleNamespace
    from typing import Any, cast

    from stage4.candidate_fakes import config

    from atlasrag.config.fingerprint import pipeline_fingerprint
    from atlasrag.services.sync.models import ServingSafety, SyncOutcome, SyncResult
    from benchmarks.ingestion.readiness_collector import collect_production_readiness

    class Documents:
        async def list(self) -> list[SimpleNamespace]:
            return await _documents()

    class Unit:
        documents = Documents()

        async def __aenter__(self) -> Unit:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    async def _documents() -> list[SimpleNamespace]:
        return [
            SimpleNamespace(relative_source_path="a.md", current_revision_id="rev"),
        ]

    runtime = config()
    result = SyncResult(
        outcome=SyncOutcome.SUCCESS,
        serving_safety=ServingSafety.SAFE_TO_SERVE,
        verified_documents=1,
    )
    evidence = await collect_production_readiness(
        cast(Any, lambda: Unit()),
        runtime=runtime,
        sync_result=result,
        expected_source_paths=("a.md",),
        expected_pipeline_fingerprint=pipeline_fingerprint(runtime),
    )
    assert evidence.canonical_lineage_verified
    assert evidence.pipeline_fingerprint_compatible
    assert evidence.serving_safety == "SAFE_TO_SERVE"
    gate = CorpusReadinessGate().assess(
        type("S", (), {"complete": True, "failure_reasons": (), "records": (1,)})(),
        evidence,
        expected_source_paths=("a.md",),
    )
    assert gate.status is ReadinessStatus.READY
