"""Non-interactive Stage 9 benchmark tooling CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.datasets.open_ragbench import DatasetAdapterError, OpenRagBenchAdapter
from benchmarks.snapshots.manifest import SnapshotError, validate_snapshot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmark")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-snapshot")
    validate.add_argument("--snapshot", type=Path, required=True)
    profile = sub.add_parser("profile")
    profile.add_argument("--snapshot", type=Path, required=True)
    return parser


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
                        "positive_documents": len(adapter.positive_document_ids()),
                    },
                    sort_keys=True,
                )
            )
            return 0
    except (SnapshotError, DatasetAdapterError) as error:
        print(f"benchmark error: {error}", file=__import__("sys").stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
