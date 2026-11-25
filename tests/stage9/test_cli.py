from __future__ import annotations

import json
from pathlib import Path

from atlasrag.domain.observation import ObservationStage
from benchmarks.cli import main
from benchmarks.observation.artifacts import (
    CaseObservationArtifact,
    StoredObservation,
    write_case_artifact,
)
from benchmarks.runners.manifests import RunManifest


def manifest() -> RunManifest:
    return RunManifest(
        run_id="run",
        dataset_revision="rev",
        snapshot_sha256="a" * 64,
        corpus_profile="FULL_CORPUS",
        query_profile="SUPPORTED",
        planned_queries=1,
        code_commit="deadbeef",
        dirty_tree=False,
        pipeline_fingerprint="pipeline",
        runtime_fingerprint="runtime",
        evaluator_fingerprint="eval",
        web_policy="OFF",
        query_cache_policy="OFF",
        parent_cache_policy="OFF",
        concurrency=1,
        repetitions=1,
        environment_class="synthetic",
        attempt_view="FINAL_LOCAL_ATTEMPT",
    )


def _events() -> tuple[StoredObservation, ...]:
    values = [
        StoredObservation(
            stage=stage, status="OK", invocation_id="inv", attempt_index=1
        )
        for stage in (
            ObservationStage.DENSE,
            ObservationStage.BM25,
            ObservationStage.FUSION,
            ObservationStage.RERANKER,
            ObservationStage.EVIDENCE,
            ObservationStage.SUFFICIENCY,
        )
    ]
    values.append(
        StoredObservation(
            stage=ObservationStage.ROUTING,
            status="ANSWER",
            invocation_id="inv",
            attempt_index=1,
        )
    )
    values.append(
        StoredObservation(
            stage=ObservationStage.ANSWER,
            status="SUCCESS",
            invocation_id="inv",
            attempt_index=1,
        )
    )
    return tuple(values)


def files(
    tmp_path: Path, *, gold: dict[str, dict[str, int]] | None = None
) -> tuple[Path, Path, Path]:
    artifact_path = tmp_path / "cases.jsonl"
    write_case_artifact(
        artifact_path,
        CaseObservationArtifact(
            run_id="run",
            query_id="q1",
            repetition_index=0,
            invocation_id="inv",
            observations=_events(),
            terminal_outcome="SUCCESS",
            attempt_view="FINAL_LOCAL_ATTEMPT",
        ),
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest().model_dump_json(), encoding="utf-8")
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(json.dumps(gold or {"q1": {"d1": 1}}), encoding="utf-8")
    return artifact_path, manifest_path, gold_path


def test_cli_valid_manifest_and_query_specific_gold(tmp_path: Path) -> None:
    artifacts, manifest_path, gold = files(tmp_path)
    assert (
        main(
            [
                "compute-metrics",
                "--artifacts",
                str(artifacts),
                "--manifest",
                str(manifest_path),
                "--gold",
                str(gold),
            ]
        )
        == 0
    )


def test_cli_empty_or_wrong_gold_fails(tmp_path: Path) -> None:
    artifacts, manifest_path, gold = files(tmp_path, gold={"other": {"d1": 1}})
    assert (
        main(
            [
                "compute-metrics",
                "--artifacts",
                str(artifacts),
                "--manifest",
                str(manifest_path),
                "--gold",
                str(gold),
            ]
        )
        == 2
    )
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert (
        main(
            [
                "compute-metrics",
                "--artifacts",
                str(empty),
                "--manifest",
                str(manifest_path),
                "--gold",
                str(gold),
            ]
        )
        == 2
    )


def test_cli_attempt_view_mismatch_and_legacy_gold_fail(tmp_path: Path) -> None:
    artifacts, manifest_path, _ = files(tmp_path)
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"d1": 1}), encoding="utf-8")
    assert (
        main(
            [
                "compute-metrics",
                "--artifacts",
                str(artifacts),
                "--manifest",
                str(manifest_path),
                "--gold",
                str(legacy),
            ]
        )
        == 2
    )
    assert (
        main(
            [
                "compute-metrics",
                "--artifacts",
                str(artifacts),
                "--manifest",
                str(manifest_path),
                "--gold",
                str(legacy),
                "--attempt-view",
                "FIRST_LOCAL_ATTEMPT",
            ]
        )
        == 2
    )
