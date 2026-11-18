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


def artifact(*events: StoredObservation) -> CaseObservationArtifact:
    return CaseObservationArtifact(
        run_id="run",
        query_id="q1",
        repetition_index=0,
        invocation_id="inv",
        observations=events,
        terminal_outcome="SUCCESS",
        attempt_view="FINAL_LOCAL_ATTEMPT",
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


def test_causal_order_and_terminal_attempt_are_enforced() -> None:
    unordered = artifact(
        observation(ObservationStage.FUSION),
        observation(ObservationStage.DENSE),
        observation(ObservationStage.BM25),
        observation(ObservationStage.RERANKER),
        observation(ObservationStage.EVIDENCE),
        observation(ObservationStage.SUFFICIENCY),
        observation(ObservationStage.ROUTING),
        observation(ObservationStage.ANSWER, status="SUCCESS"),
    )
    with pytest.raises(ObservationArtifactError, match="causal stage order"):
        validate_case_artifact(unordered)


def _writer_worker(path: str, queue: object) -> None:
    try:
        write_case_artifact(Path(path), artifact(*full_events()))
        queue.put("ok")  # type: ignore[attr-defined]
    except ObservationArtifactError:
        queue.put("duplicate")  # type: ignore[attr-defined]


def test_concurrent_writers_commit_exactly_one_case(tmp_path: Path) -> None:
    import multiprocessing

    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    path = tmp_path / "cases.jsonl"
    processes = [
        context.Process(target=_writer_worker, args=(str(path), queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
    results = [queue.get(timeout=5) for _ in processes]
    assert sorted(results) == ["duplicate", "ok"]
    records = read_case_artifacts(path)
    assert len(records) == 1
    validate_case_artifact(records[0])
