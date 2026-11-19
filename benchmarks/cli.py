"""Non-interactive Stage 9 benchmark tooling CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from benchmarks.datasets.open_ragbench import DatasetAdapterError, OpenRagBenchAdapter
from benchmarks.metrics.replay import AttemptSelection, replay_case_metrics
from benchmarks.observation.artifacts import (
    CaseObservationArtifact,
    ObservationArtifactError,
    read_case_artifacts,
    validate_case_artifact,
)
from benchmarks.runners.manifests import RunManifest
from benchmarks.snapshots.manifest import SnapshotError, validate_snapshot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmark")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-snapshot")
    validate.add_argument("--snapshot", type=Path, required=True)
    profile = sub.add_parser("profile")
    profile.add_argument("--snapshot", type=Path, required=True)
    verify = sub.add_parser("verify-corpus")
    verify.add_argument("--snapshot", type=Path, required=True)
    verify.add_argument("--profile", default="FULL_CORPUS")
    for name in ("compute-metrics", "report"):
        command = sub.add_parser(name)
        command.add_argument("--artifacts", type=Path, required=True)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--gold", type=Path, required=True)
        command.add_argument("--attempt-view", default="")
        command.add_argument("--alternate-analysis", action="store_true")
    quality = sub.add_parser("run-quality")
    quality.add_argument("--snapshot", type=Path, required=True)
    quality.add_argument("--runtime-module", type=Path, required=True)
    quality.add_argument("--gold", type=Path, required=True)
    quality.add_argument("--output", type=Path, required=True)
    quality.add_argument("--repetitions", type=int, default=1)
    quality.add_argument("--max-cases", type=int, default=100)
    quality.add_argument("--attempt-view", default="FINAL_LOCAL_ATTEMPT")
    check = sub.add_parser("check-artifacts")
    check.add_argument("--artifacts", type=Path, required=True)
    check.add_argument("--manifest", type=Path, required=True)
    return parser


def _gold(path: Path) -> dict[str, dict[str, int]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not value:
        raise ValueError("gold must be a nonempty query_id -> document relevance map")
    result: dict[str, dict[str, int]] = {}
    for query_id, docs in value.items():
        if not isinstance(query_id, str) or not isinstance(docs, dict):
            raise ValueError("gold query mapping is invalid")
        if not all(
            isinstance(doc_id, str) and type(grade) is int
            for doc_id, grade in docs.items()
        ):
            raise ValueError("gold document relevance must be integer")
        result[query_id] = docs
    return result


def _manifest(path: Path) -> RunManifest:
    return RunManifest.model_validate_json(path.read_text(encoding="utf-8"))


def _artifacts(path: Path) -> tuple[CaseObservationArtifact, ...]:
    cases = read_case_artifacts(path)
    for case in cases:
        validate_case_artifact(case)
    return cases


def _validate_run(
    manifest: RunManifest,
    cases: tuple[CaseObservationArtifact, ...],
    gold: dict[str, dict[str, int]],
) -> None:
    if not cases:
        raise ValueError("no committed case artifacts")
    if manifest.planned_queries != len(gold):
        raise ValueError("manifest planned_queries does not match gold query set")
    if {case.run_id for case in cases} != {manifest.run_id}:
        raise ValueError("artifact run_id does not match manifest")
    if {case.query_id for case in cases} != set(gold):
        raise ValueError("case query identities do not match gold")
    if {case.attempt_view for case in cases} != {manifest.attempt_view}:
        raise ValueError("case attempt view does not match manifest")
    for query_id in gold:
        indexes = sorted(
            case.repetition_index for case in cases if case.query_id == query_id
        )
        if indexes != list(range(manifest.repetitions)):
            raise ValueError("case repetition set does not match manifest")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run-quality":
            from benchmarks.quality_command import execute_quality_command

            return asyncio.run(execute_quality_command(args))
        if args.command == "validate-snapshot":
            manifest = validate_snapshot(args.snapshot)
            print(
                json.dumps(
                    {
                        "dataset": manifest.dataset_name,
                        "revision": manifest.dataset_revision,
                        "documents": manifest.total_documents,
                        "queries": manifest.total_queries,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "profile":
            adapter = OpenRagBenchAdapter(args.snapshot)
            print(
                json.dumps(
                    {
                        "counts": adapter.counts(),
                        "profiles": adapter.supported_profiles(),
                        "qrel_referenced_documents": len(
                            adapter.qrel_referenced_document_ids()
                        ),
                        "unreferenced_documents": len(
                            adapter.document_partition().unreferenced_ids
                        ),
                        "verified_hard_negative_documents": adapter.document_partition().verified_hard_negative_ids,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "verify-corpus":
            adapter = OpenRagBenchAdapter(args.snapshot)
            if args.profile == "NEGATIVE_ONLY_CORPUS":
                print(
                    json.dumps(
                        {"status": "BLOCKED_UNVERIFIED_PARTITION", "exit_code": 2},
                        sort_keys=True,
                    )
                )
                return 2
            if args.profile != "FULL_CORPUS":
                print(
                    json.dumps({"error": "unsupported corpus profile"}, sort_keys=True)
                )
                return 2
            print(
                json.dumps(
                    {
                        "status": "SNAPSHOT_VALID",
                        "corpus_profile": args.profile,
                        "documents": len(adapter.corpus_document_ids()),
                        "supported_queries": len(adapter.supported_queries()),
                        "note": "production ingestion readiness requires separate verification",
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command in {"compute-metrics", "report"}:
            run_manifest = _manifest(args.manifest)
            cases = _artifacts(args.artifacts)
            gold = _gold(args.gold)
            _validate_run(run_manifest, cases, gold)
            selection = (
                AttemptSelection(args.attempt_view)
                if args.attempt_view
                else AttemptSelection(run_manifest.attempt_view)
            )
            if (
                selection.value != run_manifest.attempt_view
                and not args.alternate_analysis
            ):
                raise ValueError("attempt view conflicts with run manifest")
            results = []
            for case in cases:
                replay = replay_case_metrics(
                    case,
                    gold_documents=gold[case.query_id],
                    attempt_view=selection,
                    allow_alternate_analysis=True,
                )
                results.append(
                    {
                        "case": {
                            "run_id": case.run_id,
                            "query_id": case.query_id,
                            "repetition_index": case.repetition_index,
                        },
                        "attempt_view": replay.attempt_view.value,
                        "alternate_analysis": selection.value
                        != run_manifest.attempt_view,
                        "stages": [
                            {
                                "stage": item.stage.value,
                                "attempt_index": item.attempt_index,
                                "hit5": item.hit5,
                                "recall5": item.recall5,
                                "hit10": item.hit10,
                                "recall10": item.recall10,
                                "mrr10": item.mrr10,
                                "ndcg10": item.ndcg10,
                                "strict_metrics": None
                                if item.strict_metrics is None
                                else item.strict_metrics.__dict__,
                            }
                            for item in replay.stages
                        ],
                    }
                )
            if args.command == "compute-metrics":
                print(
                    json.dumps(
                        {
                            "status": "VALID",
                            "run_id": run_manifest.run_id,
                            "attempt_view": run_manifest.attempt_view,
                            "cases": results,
                        },
                        sort_keys=True,
                    )
                )
                return 0
            print(
                json.dumps(
                    {
                        "status": "SYNTHETIC_OR_DECLARED_REPORT",
                        "run_id": run_manifest.run_id,
                        "attempt_view": run_manifest.attempt_view,
                        "case_count": len(cases),
                        "cases": results,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "check-artifacts":
            run_manifest = _manifest(args.manifest)
            cases = _artifacts(args.artifacts)
            if not cases:
                raise ValueError("no committed case artifacts")
            if {case.run_id for case in cases} != {run_manifest.run_id}:
                raise ValueError("artifact run_id does not match manifest")
            print(
                json.dumps(
                    {
                        "status": "COMMITTED_CASES_VALID",
                        "committed_cases": len(cases),
                        "run_id": run_manifest.run_id,
                    },
                    sort_keys=True,
                )
            )
            return 0
    except (
        SnapshotError,
        DatasetAdapterError,
        ObservationArtifactError,
        ValueError,
        OSError,
        json.JSONDecodeError,
    ) as error:
        print(
            json.dumps(
                {"status": "ERROR", "error": str(error), "exit_code": 2},
                sort_keys=True,
            )
        )
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
