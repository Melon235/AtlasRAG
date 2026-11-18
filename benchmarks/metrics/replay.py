"""Replay deterministic metrics from validated observation artifacts only."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

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
    StoredObservation,
    validate_case_artifact,
)

RANKING_STAGES = (
    ObservationStage.DENSE,
    ObservationStage.BM25,
    ObservationStage.FUSION,
    ObservationStage.RERANKER,
)


class AttemptSelection(StrEnum):
    FIRST_LOCAL_ATTEMPT = "FIRST_LOCAL_ATTEMPT"
    FINAL_LOCAL_ATTEMPT = "FINAL_LOCAL_ATTEMPT"
    PER_ATTEMPT = "PER_ATTEMPT"


@dataclass(frozen=True)
class StageReplay:
    stage: ObservationStage
    attempt_index: int
    hit5: float
    recall5: float
    hit10: float
    recall10: float
    mrr10: float
    ndcg10: float
    strict_metrics: RetrievalMetrics | None


@dataclass(frozen=True)
class ReplayResult:
    attempt_view: AttemptSelection
    stages: tuple[StageReplay, ...]
    selected_evidence_mapping: str
    metric_version: str = "retrieval-metrics-v1"


def _events_for(
    artifact: CaseObservationArtifact, stage: ObservationStage, attempt: int
) -> StoredObservation:
    events = [
        event
        for event in artifact.observations
        if event.stage is stage and event.attempt_index == attempt
    ]
    if len(events) != 1:
        raise ObservationArtifactError(
            f"exactly one {stage.value} observation is required for attempt {attempt}"
        )
    return events[0]


def ranked_units(
    artifact: CaseObservationArtifact,
    stage: ObservationStage,
    *,
    attempt_index: int,
) -> tuple[RankedUnit, ...]:
    event = _events_for(artifact, stage, attempt_index)
    if any(candidate.document_id is None for candidate in event.candidates):
        raise ObservationArtifactError(
            "ranking candidates require document identity for document-level replay"
        )
    return tuple(
        RankedUnit(
            identity=candidate.identity,
            document_id=candidate.document_id or "",
            rank=candidate.rank,
            score=candidate.score,
        )
        for candidate in event.candidates
    )


def replay_case_metrics(
    artifact: CaseObservationArtifact,
    *,
    gold_documents: dict[str, int],
    strict_evidence: dict[str, int] | None = None,
    attempt_view: AttemptSelection = AttemptSelection.FINAL_LOCAL_ATTEMPT,
) -> ReplayResult:
    validate_case_artifact(artifact)
    attempts = sorted({event.attempt_index for event in artifact.observations})
    if attempt_view is AttemptSelection.PER_ATTEMPT:
        selected_attempts = attempts
    elif attempt_view is AttemptSelection.FIRST_LOCAL_ATTEMPT:
        selected_attempts = [attempts[0]]
    else:
        selected_attempts = [attempts[-1]]
    stages: list[StageReplay] = []
    for attempt in selected_attempts:
        for stage in RANKING_STAGES:
            units = ranked_units(artifact, stage, attempt_index=attempt)
            at5 = evaluate_relaxed_document(units, gold_documents, k=5)
            at10 = evaluate_relaxed_document(units, gold_documents, k=10)
            strict = (
                evaluate_strict_evidence(units, strict_evidence, k=10)
                if strict_evidence
                else None
            )
            stages.append(
                StageReplay(
                    stage=stage,
                    attempt_index=attempt,
                    hit5=at5.hit,
                    recall5=at5.recall,
                    hit10=at10.hit,
                    recall10=at10.recall,
                    mrr10=at10.mrr,
                    ndcg10=at10.ndcg,
                    strict_metrics=strict,
                )
            )
    return ReplayResult(
        attempt_view=attempt_view,
        stages=tuple(stages),
        selected_evidence_mapping="UNAVAILABLE_NO_PARENT_METADATA",
    )
