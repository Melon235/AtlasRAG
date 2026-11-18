from __future__ import annotations

import pytest

from atlasrag.domain.observation import ObservationStage
from benchmarks.metrics.replay import ranked_units, replay_case_metrics
from benchmarks.observation.artifacts import (
    CaseObservationArtifact,
    ObservationArtifactError,
    StoredCandidate,
    StoredObservation,
)


def observation(stage: ObservationStage, *, status: str = "OK") -> StoredObservation:
    return StoredObservation(
        stage=stage,
        status=status,
        candidates=(
            StoredCandidate(identity="c1", document_id="d1", rank=1, score=0.9),
            StoredCandidate(identity="c2", document_id="d2", rank=2, score=0.5),
        ),
        invocation_id="inv",
        attempt_index=1,
    )


def artifact() -> CaseObservationArtifact:
    events = tuple(
        observation(stage)
        for stage in (
            ObservationStage.DENSE,
            ObservationStage.BM25,
            ObservationStage.FUSION,
            ObservationStage.RERANKER,
            ObservationStage.EVIDENCE,
            ObservationStage.SUFFICIENCY,
            ObservationStage.ROUTING,
        )
    ) + (observation(ObservationStage.ANSWER, status="SUCCESS"),)
    return CaseObservationArtifact(
        run_id="run",
        query_id="q1",
        repetition_index=0,
        invocation_id="inv",
        observations=events,
        terminal_outcome="SUCCESS",
    )


def test_replay_from_artifact_without_production_calls() -> None:
    result = replay_case_metrics(artifact(), gold_documents={"d1": 1}, k=1)
    assert len(result.stages) == 4
    assert all(item.document_metrics.recall == 1.0 for item in result.stages)
    assert result.selected_evidence_mapping.startswith("UNAVAILABLE")


def test_missing_ranking_stage_fails_closed() -> None:
    item = artifact().model_copy(
        update={
            "observations": tuple(
                event
                for event in artifact().observations
                if event.stage is not ObservationStage.DENSE
            )
        }
    )
    with pytest.raises(ObservationArtifactError, match="exactly one"):
        ranked_units(item, ObservationStage.DENSE)
