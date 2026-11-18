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
from benchmarks.ingestion.runner import (
    BenchmarkIngestionRunner,
    IngestionRecord,
    IngestionSummary,
)
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
        pipeline_fingerprint_compatible=True,
        stage5_verified_documents=2,
        verification_id="a" * 64,
        verification_execution_id="exec",
        verification_evidence_digest="d" * 64,
        snapshot_identity="snapshot",
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
            pipeline_fingerprint_compatible=True,
            stage5_verified_documents=2,
            verification_id="a" * 64,
            verification_execution_id="exec",
            verification_evidence_digest="d" * 64,
            snapshot_identity="snapshot",
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
async def test_readiness_collector_uses_fresh_verification() -> None:
    from types import SimpleNamespace
    from typing import Any, cast

    from stage4.candidate_fakes import config

    from atlasrag.config.fingerprint import pipeline_fingerprint
    from atlasrag.domain.ingestion.source import derive_document_id, derive_revision_id
    from atlasrag.repositories.postgres.records import IngestionStatus
    from atlasrag.services.sync.models import GlobalSafetyResult, ServingSafety
    from benchmarks.ingestion.readiness_collector import collect_production_readiness

    runtime = config()
    pipeline = pipeline_fingerprint(runtime)
    document_id = derive_document_id("a.md")
    content_hash = "a" * 64

    class Documents:
        async def list(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    relative_source_path="a.md",
                    document_id=document_id,
                    current_revision_id=derive_revision_id(
                        document_id, content_hash, pipeline
                    ),
                    current_content_hash=content_hash,
                    current_pipeline_fingerprint=pipeline,
                    building_revision_id=None,
                    ingestion_status=IngestionStatus.READY,
                    failed_stage=None,
                ),
            ]

    class Unit:
        documents = Documents()

        async def __aenter__(self) -> Unit:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    async def verifier() -> GlobalSafetyResult:
        return GlobalSafetyResult(
            serving_safety=ServingSafety.SAFE_TO_SERVE,
            verified_documents=1,
        )

    evidence = await collect_production_readiness(
        cast(Any, lambda: Unit()),
        runtime=runtime,
        verifier=verifier,
        expected_source_paths=("a.md",),
        expected_pipeline_fingerprint=pipeline,
        snapshot_identity="snapshot",
    )
    assert evidence.canonical_lineage_verified
    assert evidence.pipeline_fingerprint_compatible
    assert evidence.serving_safety == "SAFE_TO_SERVE"
    assert evidence.stage5_verified_documents == 1
    assert evidence.verification_id
    gate = CorpusReadinessGate().assess(
        IngestionSummary(
            records=(IngestionRecord("a.md", "READY"),),
            expected_count=1,
            complete=True,
            failure_reasons=(),
        ),
        evidence,
        expected_source_paths=("a.md",),
    )
    assert gate.status is ReadinessStatus.READY


@pytest.mark.asyncio
async def test_readiness_negative_cases_and_freshness() -> None:
    from types import SimpleNamespace
    from typing import Any, cast

    from stage4.candidate_fakes import config

    from atlasrag.config.fingerprint import pipeline_fingerprint
    from atlasrag.domain.ingestion.source import derive_document_id, derive_revision_id
    from atlasrag.repositories.postgres.records import IngestionStatus
    from atlasrag.services.sync.models import GlobalSafetyResult, ServingSafety
    from benchmarks.ingestion.readiness_collector import collect_production_readiness

    runtime = config()
    pipeline = pipeline_fingerprint(runtime)

    def doc(path: str, *, fingerprint: str = pipeline) -> SimpleNamespace:
        document_id = derive_document_id(path)
        return SimpleNamespace(
            relative_source_path=path,
            document_id=document_id,
            current_revision_id=derive_revision_id(document_id, "a" * 64, fingerprint),
            current_content_hash="a" * 64,
            current_pipeline_fingerprint=fingerprint,
            building_revision_id=None,
            ingestion_status=IngestionStatus.READY,
            failed_stage=None,
        )

    class Documents:
        def __init__(self, docs: list[SimpleNamespace]) -> None:
            self.docs = docs

        async def list(self) -> list[SimpleNamespace]:
            return self.docs

    class Unit:
        def __init__(self, docs: list[SimpleNamespace]) -> None:
            self.documents = Documents(docs)

        async def __aenter__(self) -> Unit:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    async def safe() -> GlobalSafetyResult:
        return GlobalSafetyResult(
            serving_safety=ServingSafety.SAFE_TO_SERVE,
            verified_documents=1,
        )

    async def stale() -> GlobalSafetyResult:
        return GlobalSafetyResult(
            serving_safety=ServingSafety.SAFE_TO_SERVE,
            verified_documents=0,
        )

    for docs, expected, verifier, reason in (
        (
            [doc("a.md", fingerprint="b" * 64)],
            ("a.md",),
            safe,
            "PIPELINE_FINGERPRINT_MISMATCH",
        ),
        (
            [doc("a.md"), doc("extra.md")],
            ("a.md",),
            safe,
            "PUBLISHED_SOURCE_SET_MISMATCH",
        ),
        ([], ("a.md",), safe, "PUBLISHED_SOURCE_SET_MISMATCH"),
        ([doc("a.md")], ("a.md",), stale, "STAGE5_CORPUS_COUNT_MISMATCH"),
    ):
        evidence = await collect_production_readiness(
            cast(Any, lambda d=docs: Unit(d)),
            runtime=runtime,
            verifier=verifier,
            expected_source_paths=expected,
            expected_pipeline_fingerprint=pipeline,
            snapshot_identity="snapshot",
        )
        gate = CorpusReadinessGate().assess(
            IngestionSummary(
                records=(IngestionRecord("a.md", "READY"),),
                expected_count=len(expected),
                complete=True,
                failure_reasons=(),
            ),
            evidence,
            expected_source_paths=expected,
        )
        assert gate.status is not ReadinessStatus.READY
        assert reason in gate.reasons


def test_accounting_handles_noop_and_failure_without_synthetic_records() -> None:
    from benchmarks.ingestion.accounting import IngestionAccounting
    from benchmarks.ingestion.runner import IngestionRecord

    noop = IngestionAccounting.from_actual(
        (),
        sync_result=None,
        verified_published_paths=("a.md", "b.md"),
        expected_source_paths=("a.md", "b.md"),
        unchanged_source_paths=("a.md", "b.md"),
    )
    assert noop.complete
    partial = IngestionAccounting.from_actual(
        (IngestionRecord("a.md", "FAILED"),),
        sync_result=None,
        verified_published_paths=("a.md", "b.md"),
        expected_source_paths=("a.md", "b.md"),
    )
    assert not partial.complete
