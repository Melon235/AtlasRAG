from __future__ import annotations

from pathlib import Path

import pytest

from atlasrag.domain.observation import ObservationStage
from benchmarks.observation.artifacts import (
    CaseObservationArtifact,
    ObservationArtifactError,
    StoredCandidate,
    StoredObservation,
    read_case_artifacts,
    validate_case_artifact,
    write_case_artifact,
)


def observation(
    stage: ObservationStage,
    *,
    invocation: str = "inv",
    attempt: int = 1,
    status: str = "OK",
    candidates: tuple[StoredCandidate, ...] = (),
) -> StoredObservation:
    return StoredObservation(
        stage=stage,
        status=status,
        candidates=candidates,
        invocation_id=invocation,
        attempt_index=attempt,
    )


def full_events() -> tuple[StoredObservation, ...]:
    return tuple(
        observation(stage)
        for stage in (
            ObservationStage.DENSE,
            ObservationStage.BM25,
            ObservationStage.FUSION,
            ObservationStage.RERANKER,
            ObservationStage.EVIDENCE,
            ObservationStage.SUFFICIENCY,
            ObservationStage.ROUTING,
            ObservationStage.ANSWER,
        )
    )[:-1] + (observation(ObservationStage.ANSWER, status="SUCCESS"),)


def artifact(*events: StoredObservation) -> CaseObservationArtifact:
    return CaseObservationArtifact(
        run_id="run",
        query_id="q1",
        repetition_index=0,
        invocation_id="inv",
        observations=events,
        terminal_outcome="SUCCESS",
    )


def test_valid_artifact_and_jsonl_roundtrip(tmp_path: Path) -> None:
    item = artifact(*full_events())
    validate_case_artifact(
        item,
        required_stages=frozenset(
            {
                ObservationStage.FUSION,
                ObservationStage.RERANKER,
                ObservationStage.EVIDENCE,
                ObservationStage.SUFFICIENCY,
                ObservationStage.ROUTING,
                ObservationStage.ANSWER,
            }
        ),
    )
    sealed = write_case_artifact(tmp_path / "cases.jsonl", item)
    assert read_case_artifacts(tmp_path / "cases.jsonl") == (sealed,)


def test_missing_cross_invocation_and_order_rejected() -> None:
    with pytest.raises(ObservationArtifactError, match="missing"):
        validate_case_artifact(
            artifact(observation(ObservationStage.ANSWER)),
            required_stages=frozenset(
                {ObservationStage.DENSE, ObservationStage.ANSWER}
            ),
        )
    with pytest.raises(ObservationArtifactError, match="cross-invocation"):
        validate_case_artifact(
            artifact(
                observation(ObservationStage.FUSION),
                observation(ObservationStage.ANSWER, invocation="other"),
            ),
            required_stages=frozenset(
                {ObservationStage.FUSION, ObservationStage.ANSWER}
            ),
        )
    with pytest.raises(ObservationArtifactError, match="out of order"):
        validate_case_artifact(
            artifact(
                observation(ObservationStage.FUSION, attempt=2),
                observation(ObservationStage.ANSWER, attempt=1),
            ),
            required_stages=frozenset(
                {ObservationStage.FUSION, ObservationStage.ANSWER}
            ),
        )
