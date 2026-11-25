from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.runners.manifests import (
    CaseObservation,
    CaseStatus,
    RunLedger,
    RunManifest,
    assess_validity,
    validate_comparison,
)


def manifest(**overrides: object) -> RunManifest:
    values: dict[str, object] = dict(
        run_id="run-1",
        dataset_revision="rev",
        snapshot_sha256="a" * 64,
        corpus_profile="FULL_CORPUS",
        query_profile="SUPPORTED",
        planned_queries=2,
        code_commit="deadbeef",
        dirty_tree=False,
        pipeline_fingerprint="pipeline",
        runtime_fingerprint="runtime-a",
        evaluator_fingerprint="eval",
        web_policy="OFF",
        query_cache_policy="OFF",
        parent_cache_policy="BYPASSED",
        concurrency=1,
        repetitions=1,
        environment_class="local",
    )
    values.update(overrides)
    return RunManifest.model_validate(values)


def observation(
    query_id: str, status: CaseStatus, repetition: int = 0
) -> CaseObservation:
    return CaseObservation(
        query_id=query_id, repetition_index=repetition, status=status
    )


def test_validity_and_append_only_resume(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "cases.jsonl")
    ledger.append(observation("q1", CaseStatus.COMPLETED))
    with pytest.raises(ValueError, match="already exists"):
        ledger.append(observation("q1", CaseStatus.COMPLETED))
    ledger.append(observation("q2", CaseStatus.TECHNICAL_FAILURE))
    assert len(ledger.read()) == 2
    result = assess_validity(2, ledger.read())
    assert result.validity == "INVALID" and result.technical_failures == 1
    partial = assess_validity(
        2,
        [observation("q1", CaseStatus.COMPLETED)],
    )
    assert partial.validity == "INVALID" and partial.attempted == 1


def test_comparison_accepts_only_declared_factor() -> None:
    left = manifest()
    right = manifest(run_id="run-2", runtime_fingerprint="runtime-b")
    assert validate_comparison(
        left, right, declared_factor="runtime_fingerprint"
    ).accepted
    incompatible = manifest(run_id="run-3", dataset_revision="other")
    result = validate_comparison(
        left, incompatible, declared_factor="runtime_fingerprint"
    )
    assert not result.accepted
    pipeline_changed = manifest(run_id="run-4", pipeline_fingerprint="pipeline-b")
    result = validate_comparison(
        left, pipeline_changed, declared_factor="runtime_fingerprint"
    )
    assert not result.accepted


@pytest.mark.asyncio
async def test_quality_case_uses_isolated_session_and_never_gold() -> None:
    from atlasrag.domain.requests import UserTurnRequest
    from atlasrag.domain.results import FinalResponse
    from benchmarks.contracts.models import BenchmarkQuery, Modality
    from benchmarks.runners.quality import run_quality_case

    seen: list[UserTurnRequest] = []

    async def retrieve(request: UserTurnRequest) -> FinalResponse:
        seen.append(request)
        return FinalResponse(answer_text="answer", citations=())

    query = BenchmarkQuery(
        query_id="q1",
        text="question",
        modality=Modality.TEXT,
        query_type="extractive",
        reference_answer="gold answer must stay in evaluator",
    )
    case = await run_quality_case(
        query, run_id="run", repetition_index=0, retrieve=retrieve
    )
    assert case.observation.status is CaseStatus.COMPLETED
    request = seen[0]
    assert request.session_id == "benchmark:run:q1:0"
    assert not hasattr(request, "reference_answer")


def test_zero_planned_is_invalid_and_manifest_rejects_zero() -> None:
    from pydantic import ValidationError

    result = assess_validity(0, [])
    assert result.validity == "INVALID"
    with pytest.raises(ValidationError):
        manifest(planned_queries=0)


def test_comparison_rejects_confounds_and_identical_factor() -> None:
    left = manifest()
    pipeline_changed = manifest(
        run_id="run-2",
        pipeline_fingerprint="pipeline-b",
        runtime_fingerprint="runtime-b",
    )
    assert not validate_comparison(
        left, pipeline_changed, declared_factor="pipeline_fingerprint"
    ).accepted
    same = manifest(run_id="run-3")
    assert not validate_comparison(
        left, same, declared_factor="runtime_fingerprint"
    ).accepted
