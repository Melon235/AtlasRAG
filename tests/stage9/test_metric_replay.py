from __future__ import annotations

import pytest

from atlasrag.domain.observation import ObservationStage
from benchmarks.metrics.replay import (
    AttemptSelection,
    ranked_units,
    replay_case_metrics,
)
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
        observation(
            stage,
            status="ANSWER" if stage is ObservationStage.ROUTING else "OK",
        )
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
        attempt_view="FINAL_LOCAL_ATTEMPT",
    )


def test_replay_from_artifact_without_production_calls() -> None:
    result = replay_case_metrics(
        artifact(),
        gold_documents={"d1": 1},
        attempt_view=AttemptSelection.FINAL_LOCAL_ATTEMPT,
    )
    assert len(result.stages) == 4
    assert all(item.recall10 == 1.0 for item in result.stages)
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
        ranked_units(item, ObservationStage.DENSE, attempt_index=1)


def _attempt_artifact() -> CaseObservationArtifact:
    events = []
    for attempt in (1, 2):
        for stage in (
            ObservationStage.DENSE,
            ObservationStage.BM25,
            ObservationStage.FUSION,
            ObservationStage.RERANKER,
            ObservationStage.EVIDENCE,
            ObservationStage.SUFFICIENCY,
            ObservationStage.ROUTING,
        ):
            events.append(
                StoredObservation(
                    stage=stage,
                    status=(
                        "RETRY_LOCAL"
                        if stage is ObservationStage.ROUTING and attempt == 1
                        else "ANSWER"
                        if stage is ObservationStage.ROUTING
                        else "OK"
                    ),
                    candidates=(
                        StoredCandidate(
                            identity=f"c{attempt}", document_id="d1", rank=1, score=0.9
                        ),
                    ),
                    invocation_id="inv",
                    attempt_index=attempt,
                )
            )
    events.append(
        StoredObservation(
            stage=ObservationStage.ANSWER,
            status="SUCCESS",
            invocation_id="inv",
            attempt_index=2,
        )
    )
    return CaseObservationArtifact(
        run_id="run",
        query_id="q1",
        repetition_index=0,
        invocation_id="inv",
        observations=tuple(events),
        terminal_outcome="SUCCESS",
        attempt_view="FINAL_LOCAL_ATTEMPT",
    )


def test_first_final_and_per_attempt_replay_are_explicit() -> None:
    item = _attempt_artifact()
    first = replay_case_metrics(
        item,
        gold_documents={"d1": 1},
        attempt_view=AttemptSelection.FIRST_LOCAL_ATTEMPT,
    )
    final = replay_case_metrics(
        item,
        gold_documents={"d1": 1},
        attempt_view=AttemptSelection.FINAL_LOCAL_ATTEMPT,
    )
    per_attempt = replay_case_metrics(
        item,
        gold_documents={"d1": 1},
        attempt_view=AttemptSelection.PER_ATTEMPT,
    )
    assert {stage.attempt_index for stage in first.stages} == {1}
    assert {stage.attempt_index for stage in final.stages} == {2}
    assert {stage.attempt_index for stage in per_attempt.stages} == {1, 2}
    assert len(per_attempt.stages) == 8
