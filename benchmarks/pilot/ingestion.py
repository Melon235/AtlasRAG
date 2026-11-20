"""Pilot-only ingestion orchestration over the real Part I build boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atlasrag.config.models import RuntimeConfig
from atlasrag.services.sync.models import GlobalSafetyResult
from benchmarks.ingestion.readiness import (
    CorpusReadinessGate,
    ProductionReadinessEvidence,
)
from benchmarks.ingestion.readiness_collector import collect_production_readiness
from benchmarks.ingestion.runner import BenchmarkIngestionRunner, IngestionSummary
from benchmarks.pilot.identity import PilotInputs, pilot_required_source_paths


@dataclass(frozen=True)
class PilotIngestionResult:
    summary: IngestionSummary
    readiness: ProductionReadinessEvidence
    gate_status: str
    gate_reasons: tuple[str, ...]


async def ingest_selected_pdfs(
    *,
    pdf_directory: Path,
    inputs: PilotInputs,
    backend: Any,
    uow_factory: Any,
    runtime: RuntimeConfig,
    verifier: Callable[[], Awaitable[GlobalSafetyResult]],
    expected_pipeline_fingerprint: str,
) -> PilotIngestionResult:
    expected = pilot_required_source_paths(inputs)
    root = pdf_directory.resolve()
    actual = tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        )
    )
    if actual != expected:
        raise ValueError(
            "pilot PDF set mismatch: expected exactly " + ", ".join(expected)
        )
    runner = BenchmarkIngestionRunner(backend)
    summary = await runner.ingest(
        root,
        expected,
        expected_pipeline_fingerprint=expected_pipeline_fingerprint,
    )
    evidence = await collect_production_readiness(
        uow_factory,
        runtime=runtime,
        verifier=verifier,
        expected_source_paths=expected,
        expected_pipeline_fingerprint=expected_pipeline_fingerprint,
        snapshot_identity=inputs.mapping_fingerprint,
    )
    gate = CorpusReadinessGate().assess(
        summary,
        evidence,
        expected_source_paths=expected,
        expected_snapshot_identity=inputs.mapping_fingerprint,
    )
    return PilotIngestionResult(
        summary=summary,
        readiness=evidence,
        gate_status=gate.status.value,
        gate_reasons=gate.reasons,
    )
