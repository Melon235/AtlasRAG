"""Non-interactive Stage 9 benchmark tooling CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.datasets.open_ragbench import DatasetAdapterError, OpenRagBenchAdapter
from benchmarks.metrics.replay import AttemptSelection, replay_case_metrics
from benchmarks.observation.artifacts import (
    ObservationArtifactError,
    read_case_artifacts,
    validate_case_artifact,
)
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
    metrics = sub.add_parser("compute-metrics")
    metrics.add_argument("--artifacts", type=Path, required=True)
    metrics.add_argument("--gold", type=Path, required=True)
    metrics.add_argument("--attempt-view", default="FINAL_LOCAL_ATTEMPT")
    report = sub.add_parser("report")
    report.add_argument("--artifacts", type=Path, required=True)
    report.add_argument("--gold", type=Path, required=True)
    report.add_argument("--attempt-view", default="FINAL_LOCAL_ATTEMPT")
    resume = sub.add_parser("resume")
    resume.add_argument("--artifacts", type=Path, required=True)
    return parser


def _gold(path: Path) -> dict[str, int]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and type(grade) is int for key, grade in value.items()
    ):
        raise ValueError("gold must be an object of document_id to integer relevance")
    return value


def _artifacts(path: Path):
    cases = read_case_artifacts(path)
    for case in cases:
        validate_case_artifact(case)
    return cases


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
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
            cases = _artifacts(args.artifacts)
            gold = _gold(args.gold)
            selection = AttemptSelection(args.attempt_view)
            results = []
            for case in cases:
                replay = replay_case_metrics(
                    case,
                    gold_documents=gold,
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
                print(json.dumps({"status": "VALID", "cases": results}, sort_keys=True))
                return 0
            print(
                json.dumps(
                    {
                        "status": "SYNTHETIC_OR_DECLARED_REPORT",
                        "case_count": len(cases),
                        "cases": results,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "resume":
            cases = _artifacts(args.artifacts)
            print(
                json.dumps(
                    {
                        "status": "RESUME_SAFE",
                        "committed_cases": len(cases),
                        "keys": [
                            [c.run_id, c.query_id, c.repetition_index] for c in cases
                        ],
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
                {"status": "ERROR", "error": str(error), "exit_code": 2}, sort_keys=True
            )
        )
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
