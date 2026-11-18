"""Replay deterministic metrics from validated observation artifacts only."""

from __future__ import annotations

from dataclasses import dataclass

from atlasrag.domain.observation import ObservationStage
from benchmarks.contracts.models import RankedUnit
from benchmarks.metrics.retrieval import (
    RetrievalMetrics,
    evaluate_relaxed_document,
    evaluate_strict_evidence,
)
from benchmarks.observation.artifacts import (
    CaseObservationArtifact,
    ObservationArtifactError,
    validate_case_artifact,
)

_RANKING_STAGES = (
    ObservationStage.DENSE,
    ObservationStage.BM25,
    ObservationStage.FUSION,
    ObservationStage.RERANKER,
)


@dataclass(frozen=True)
class StageReplay:
    stage: ObservationStage
    document_metrics: RetrievalMetrics
    strict_metrics: RetrievalMetrics | None


@dataclass(frozen=True)
class ReplayResult:
    stages: tuple[StageReplay, ...]
    selected_evidence_mapping: str


def ranked_units(
    artifact: CaseObservationArtifact, stage: ObservationStage
) -> tuple[RankedUnit, ...]:
    events = [event for event in artifact.observations if event.stage is stage]
    if len(events) != 1:
        raise ObservationArtifactError(
            f"exactly one {stage.value} observation is required"
        )
    return tuple(
        RankedUnit(
            identity=candidate.identity,
            document_id=candidate.document_id or "",
            rank=candidate.rank,
            score=candidate.score,
        )
        for candidate in events[0].candidates
        if candidate.document_id
    )


def replay_case_metrics(
    artifact: CaseObservationArtifact,
    *,
    gold_documents: dict[str, int],
    strict_evidence: dict[str, int] | None = None,
    k: int = 10,
) -> ReplayResult:
    validate_case_artifact(artifact)
    stages: list[StageReplay] = []
    for stage in _RANKING_STAGES:
        units = ranked_units(artifact, stage)
        document_metrics = evaluate_relaxed_document(units, gold_documents, k=k)
        strict_metrics = (
            evaluate_strict_evidence(units, strict_evidence, k=k)
            if strict_evidence
            else None
        )
        stages.append(StageReplay(stage, document_metrics, strict_metrics))
    return ReplayResult(
        stages=tuple(stages),
        selected_evidence_mapping="UNAVAILABLE_NO_PARENT_METADATA",
    )
