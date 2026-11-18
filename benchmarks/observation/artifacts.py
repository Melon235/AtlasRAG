"""Durable benchmark-owned per-case observation artifacts and validation."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from atlasrag.domain.observation import ObservationStage, ProductionObservation


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StoredCandidate(_Frozen):
    identity: StrictStr
    rank: int = Field(ge=1)
    score: float | None = None
    document_id: StrictStr | None = None


class StoredObservation(_Frozen):
    stage: ObservationStage
    status: StrictStr
    candidates: tuple[StoredCandidate, ...] = ()
    selected_identities: tuple[StrictStr, ...] = ()
    latency_ms: float | None = None
    degraded: bool = False
    invocation_id: StrictStr
    attempt_index: int = Field(ge=1)


class CaseObservationArtifact(_Frozen):
    run_id: StrictStr
    query_id: StrictStr
    repetition_index: int = Field(ge=0)
    invocation_id: StrictStr
    observations: tuple[StoredObservation, ...]
    terminal_outcome: StrictStr


class ObservationArtifactError(ValueError):
    pass


def stored_observation(event: ProductionObservation) -> StoredObservation:
    if not event.invocation_id:
        raise ObservationArtifactError("observation has no invocation identity")
    return StoredObservation(
        stage=event.stage,
        status=event.status,
        candidates=tuple(
            StoredCandidate(
                identity=item.identity,
                rank=item.rank,
                score=item.score,
                document_id=item.document_id,
            )
            for item in event.candidates
        ),
        selected_identities=event.selected_identities,
        latency_ms=event.latency_ms,
        degraded=event.degraded,
        invocation_id=event.invocation_id,
        attempt_index=event.attempt_index,
    )


def validate_case_artifact(
    artifact: CaseObservationArtifact,
    *,
    required_stages: frozenset[ObservationStage],
    allow_safe_failure: bool = True,
) -> None:
    if not artifact.observations:
        raise ObservationArtifactError("case has no observations")
    invocation_ids = {item.invocation_id for item in artifact.observations}
    if invocation_ids != {artifact.invocation_id}:
        raise ObservationArtifactError("cross-invocation observation contamination")
    stages = {item.stage for item in artifact.observations}
    missing = required_stages - stages
    if missing:
        raise ObservationArtifactError(
            f"missing required observations: {sorted(stage.value for stage in missing)}"
        )
    attempts = [item.attempt_index for item in artifact.observations]
    if attempts != sorted(attempts):
        raise ObservationArtifactError("observation attempts are out of order")
    answer_events = [
        item for item in artifact.observations if item.stage is ObservationStage.ANSWER
    ]
    if not answer_events:
        raise ObservationArtifactError("missing terminal ANSWER observation")
    if not allow_safe_failure and any(
        item.status == "SAFE_FAILURE" for item in answer_events
    ):
        raise ObservationArtifactError("SAFE_FAILURE is not allowed for this run")


def write_case_artifact(path: Path, artifact: CaseObservationArtifact) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(artifact.model_dump_json() + "\n")


def read_case_artifacts(path: Path) -> tuple[CaseObservationArtifact, ...]:
    if not path.exists():
        return ()
    return tuple(
        CaseObservationArtifact.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
