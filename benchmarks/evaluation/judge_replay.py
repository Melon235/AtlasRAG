"""Judge-only DeepEval replay planner; never calls a paid Judge without authorization."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

METRICS = (
    "faithfulness",
    "answer_relevancy",
    "contextual_relevancy",
    "contextual_precision",
    "contextual_recall",
)


@dataclass(frozen=True)
class ReplayPlan:
    missing: dict[str, list[str]]
    attempts_estimate: int
    token_estimate: int
    cost_estimate_micro_usd: int


def build_plan(
    captures: list[dict[str, Any]],
    prior: list[dict[str, Any]],
    references: dict[str, str],
) -> ReplayPlan:
    query_ids = [row["query_id"] for row in captures]
    if len(query_ids) != 20 or len(set(query_ids)) != 20:
        raise ValueError("captures must contain 20 unique query identities")
    if {row["query_id"] for row in prior} != set(query_ids):
        raise ValueError("prior results do not cover the capture panel")
    if set(references) != set(query_ids):
        raise ValueError("reference answers must cover every captured query")
    missing: dict[str, list[str]] = {}
    for metric in METRICS:
        missing[metric] = [
            row["query_id"]
            for row in prior
            if row["scores"].get(metric, {}).get("status") != "EVALUATED"
        ]
    # Observed previous batch: 35 metric evaluations consumed 155 actual HTTP attempts.
    metric_count = sum(len(items) for items in missing.values())
    attempts = max(1, round(metric_count * (155 / 35))) if metric_count else 0
    tokens = max(1, round(attempts * (240781 / 200))) if attempts else 0
    cost = max(1, round(attempts * (367252 / 200))) if attempts else 0
    return ReplayPlan(missing, attempts, tokens, cost)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--captures", type=Path, required=True)
    parser.add_argument("--prior", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--authorized", action="store_true")
    args = parser.parse_args()
    captures = [
        json.loads(line)
        for line in args.captures.read_text().splitlines()
        if line.strip()
    ]
    prior = json.loads(args.prior.read_text())["results"]
    references = json.loads(args.references.read_text())
    plan = build_plan(captures, prior, references)
    out = {
        "status": "AUTHORIZED" if args.authorized else "AWAITING_PAID_AUTHORIZATION",
        "missing": plan.missing,
        "attempts_estimate": plan.attempts_estimate,
        "token_estimate": plan.token_estimate,
        "cost_micro_usd_estimate": plan.cost_estimate_micro_usd,
        "proposed_caps": {
            "attempts": 320,
            "tokens": 500000,
            "cost_usd": 0.80,
            "wall_seconds": 7200,
        },
        "prior_successes_preserved": True,
        "citation_validation": "MARKER_ONLY (citation ID appears in answer)",
        "payment_executed": False,
    }
    args.plan.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, sort_keys=True))
    return 0 if not args.authorized else 2


if __name__ == "__main__":
    raise SystemExit(main())
