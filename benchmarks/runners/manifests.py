"""Run manifests, append-only observations, validity, and comparison checks."""

from __future__ import annotations

import fcntl
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from benchmarks.contracts.models import RunValidity


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CaseStatus(StrEnum):
    PLANNED = "PLANNED"
    COMPLETED = "COMPLETED"
    CONTROLLED_PRODUCT_FAILURE = "CONTROLLED_PRODUCT_FAILURE"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
    UNFINISHED = "UNFINISHED"


class RunManifest(_Frozen):
    run_id: StrictStr
    dataset_revision: StrictStr
    snapshot_sha256: StrictStr
    corpus_profile: StrictStr
    query_profile: StrictStr
    planned_queries: int = Field(ge=1)
    code_commit: StrictStr
    dirty_tree: bool
    pipeline_fingerprint: StrictStr
    runtime_fingerprint: StrictStr
    evaluator_fingerprint: StrictStr
    web_policy: StrictStr
    query_cache_policy: StrictStr
    parent_cache_policy: StrictStr
    concurrency: int = Field(ge=1)
    repetitions: int = Field(ge=1)
    environment_class: StrictStr
    model_identities: dict[str, str] = Field(default_factory=dict)
    attempt_view: StrictStr = "FINAL_LOCAL_ATTEMPT"


class CaseObservation(_Frozen):
    query_id: StrictStr
    repetition_index: int = Field(ge=0)
    status: CaseStatus
    product_outcome: StrictStr | None = None
    failure_reason: StrictStr | None = None


class RunValidityResult(_Frozen):
    validity: RunValidity
    planned: int
    attempted: int
    completed: int
    product_failures: int
    technical_failures: int
    unfinished: int


def assess_validity(
    planned: int, observations: list[CaseObservation]
) -> RunValidityResult:
    attempted = len(observations)
    completed = sum(
        item.status in {CaseStatus.COMPLETED, CaseStatus.CONTROLLED_PRODUCT_FAILURE}
        for item in observations
    )
    product_failures = sum(
        item.status is CaseStatus.CONTROLLED_PRODUCT_FAILURE for item in observations
    )
    technical_failures = sum(
        item.status is CaseStatus.TECHNICAL_FAILURE for item in observations
    )
    unfinished = sum(item.status is CaseStatus.UNFINISHED for item in observations)
    duplicate_keys = len(
        {(item.query_id, item.repetition_index) for item in observations}
    ) != len(observations)
    if (
        planned <= 0
        or duplicate_keys
        or technical_failures
        or unfinished
        or attempted != planned
    ):
        validity = RunValidity.INVALID
    elif completed != planned:
        validity = RunValidity.PARTIAL
    else:
        validity = RunValidity.VALID
    return RunValidityResult(
        validity=validity,
        planned=planned,
        attempted=attempted,
        completed=completed,
        product_failures=product_failures,
        technical_failures=technical_failures,
        unfinished=unfinished,
    )


class RunLedger:
    """Append-only JSONL ledger; existing case keys are never overwritten."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> list[CaseObservation]:
        if not self.path.exists():
            return []
        return [
            CaseObservation.model_validate_json(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def append(self, observation: CaseObservation) -> None:
        key = (observation.query_id, observation.repetition_index)
        if any((item.query_id, item.repetition_index) == key for item in self.read()):
            raise ValueError("observation already exists")
        with self.path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.seek(0)
                existing = [
                    CaseObservation.model_validate_json(line)
                    for line in stream.read().splitlines()
                    if line.strip()
                ]
                if any(
                    (item.query_id, item.repetition_index) == key for item in existing
                ):
                    raise ValueError("observation already exists")
                stream.seek(0, 2)
                stream.write(observation.model_dump_json() + "\n")
                stream.flush()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class ComparisonResult(_Frozen):
    accepted: bool
    reasons: tuple[str, ...] = ()


def validate_comparison(
    left: RunManifest, right: RunManifest, *, declared_factor: str
) -> ComparisonResult:
    reasons: list[str] = []
    immutable = (
        "dataset_revision",
        "snapshot_sha256",
        "query_profile",
        "evaluator_fingerprint",
        "web_policy",
        "query_cache_policy",
        "parent_cache_policy",
        "attempt_view",
    )
    for field in immutable:
        if getattr(left, field) != getattr(right, field):
            reasons.append(f"incompatible {field}")
    if declared_factor not in {
        "runtime_fingerprint",
        "pipeline_fingerprint",
        "corpus_profile",
    }:
        reasons.append("unsupported declared factor")
    if declared_factor == "runtime_fingerprint":
        if left.pipeline_fingerprint != right.pipeline_fingerprint:
            reasons.append("runtime-only comparison changed pipeline fingerprint")
        if left.corpus_profile != right.corpus_profile:
            reasons.append("runtime-only comparison changed corpus profile")
        if left.runtime_fingerprint == right.runtime_fingerprint:
            reasons.append("declared runtime factor did not differ")
    elif declared_factor == "pipeline_fingerprint":
        if left.runtime_fingerprint != right.runtime_fingerprint:
            reasons.append("index-affecting comparison changed runtime fingerprint")
        if left.corpus_profile != right.corpus_profile:
            reasons.append("index-affecting comparison changed corpus profile")
        if left.pipeline_fingerprint == right.pipeline_fingerprint:
            reasons.append("declared pipeline factor did not differ")
    elif declared_factor == "corpus_profile":
        if left.pipeline_fingerprint != right.pipeline_fingerprint:
            reasons.append("corpus comparison changed pipeline fingerprint")
        if left.runtime_fingerprint != right.runtime_fingerprint:
            reasons.append("corpus comparison changed runtime fingerprint")
        if left.corpus_profile == right.corpus_profile:
            reasons.append("declared corpus factor did not differ")
    return ComparisonResult(accepted=not reasons, reasons=tuple(reasons))
