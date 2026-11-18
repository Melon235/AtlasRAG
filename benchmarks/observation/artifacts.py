"""Durable benchmark-owned per-case observation artifacts and validation."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from atlasrag.domain.observation import ObservationStage, ProductionObservation

_REQUIRED_LOCAL = frozenset(
    {
        ObservationStage.DENSE,
        ObservationStage.BM25,
        ObservationStage.FUSION,
        ObservationStage.RERANKER,
        ObservationStage.EVIDENCE,
        ObservationStage.SUFFICIENCY,
    }
)


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
    schema_version: int = 1
    run_id: StrictStr
    query_id: StrictStr
    repetition_index: int = Field(ge=0)
    invocation_id: StrictStr
    observations: tuple[StoredObservation, ...]
    terminal_outcome: StrictStr
    case_status: StrictStr = "COMPLETED"
    observation_status: StrictStr = "COMPLETE"
    integrity_sha256: StrictStr = ""


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


def _validate_candidates(event: StoredObservation) -> None:
    ranks = [item.rank for item in event.candidates]
    if ranks != sorted(ranks) or len(ranks) != len(set(ranks)):
        raise ObservationArtifactError("candidate ranks must be strictly ordered")
    identities = [item.identity for item in event.candidates]
    if len(identities) != len(set(identities)):
        raise ObservationArtifactError("duplicate candidate identity")
    for item in event.candidates:
        if item.score is not None and not math.isfinite(item.score):
            raise ObservationArtifactError("candidate score must be finite")


def validate_case_artifact(
    artifact: CaseObservationArtifact,
    *,
    required_stages: frozenset[ObservationStage] = _REQUIRED_LOCAL,
    allow_safe_failure: bool = True,
) -> None:
    if artifact.schema_version != 1:
        raise ObservationArtifactError("unsupported artifact schema version")
    if not artifact.observations:
        raise ObservationArtifactError("case has no observations")
    if artifact.observation_status != "COMPLETE":
        raise ObservationArtifactError("case observation capture is incomplete")
    invocation_ids = {item.invocation_id for item in artifact.observations}
    if invocation_ids != {artifact.invocation_id}:
        raise ObservationArtifactError("cross-invocation observation contamination")
    ordered_attempts = [item.attempt_index for item in artifact.observations]
    if ordered_attempts != sorted(ordered_attempts):
        raise ObservationArtifactError("observation attempts are out of order")
    attempts = sorted(set(ordered_attempts))
    if attempts != list(range(1, len(attempts) + 1)):
        raise ObservationArtifactError("impossible attempt sequence")
    by_attempt: dict[int, list[StoredObservation]] = {index: [] for index in attempts}
    for event in artifact.observations:
        by_attempt[event.attempt_index].append(event)
        _validate_candidates(event)
    for index, events in by_attempt.items():
        stages = [event.stage for event in events]
        if len(stages) != len(set(stages)):
            raise ObservationArtifactError(
                f"duplicate conflicting stage in attempt {index}"
            )
        missing = required_stages - set(stages)
        if missing:
            raise ObservationArtifactError(
                f"missing required observations in attempt {index}"
            )
    answer_events = [
        event
        for event in artifact.observations
        if event.stage is ObservationStage.ANSWER
    ]
    if len(answer_events) != 1:
        raise ObservationArtifactError(
            "exactly one terminal ANSWER observation is required"
        )
    answer = answer_events[0]
    if artifact.terminal_outcome != answer.status:
        raise ObservationArtifactError(
            "terminal outcome does not match ANSWER observation"
        )
    if answer.status == "SAFE_FAILURE" and not allow_safe_failure:
        raise ObservationArtifactError("SAFE_FAILURE is not allowed for this run")
    technical = artifact.case_status == "TECHNICAL_FAILURE"
    if technical and answer.status != "SAFE_FAILURE":
        raise ObservationArtifactError("technical failure must be a failed case")


def _integrity_payload(artifact: CaseObservationArtifact) -> bytes:
    payload = artifact.model_dump(mode="json", exclude={"integrity_sha256"})
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def write_case_artifact(
    path: Path, artifact: CaseObservationArtifact
) -> CaseObservationArtifact:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = (artifact.run_id, artifact.query_id, artifact.repetition_index)
    if any(
        (existing.run_id, existing.query_id, existing.repetition_index) == key
        for existing in read_case_artifacts(path)
    ):
        raise ObservationArtifactError("duplicate committed case identity")
    sealed = artifact.model_copy(
        update={
            "integrity_sha256": hashlib.sha256(_integrity_payload(artifact)).hexdigest()
        }
    )
    with path.open("a", encoding="utf-8") as stream:
        stream.write(sealed.model_dump_json() + "\n")
    return sealed


def read_case_artifacts(path: Path) -> tuple[CaseObservationArtifact, ...]:
    if not path.exists():
        return ()
    values: list[CaseObservationArtifact] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = CaseObservationArtifact.model_validate(json.loads(line))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise ObservationArtifactError("corrupt artifact line") from error
        expected = hashlib.sha256(_integrity_payload(item)).hexdigest()
        if item.integrity_sha256 != expected:
            raise ObservationArtifactError("artifact integrity checksum mismatch")
        values.append(item)
    return tuple(values)
