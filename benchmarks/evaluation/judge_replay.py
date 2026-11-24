"""Judge-only DeepEval replay planner; never calls a paid Judge without authorization."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmarks.evaluation.deepeval_adapter import EvaluationRecord, MetricScore

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
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    captures = [
        json.loads(line)
        for line in args.captures.read_text().splitlines()
        if line.strip()
    ]
    prior = json.loads(args.prior.read_text())["results"]
    references = json.loads(args.references.read_text())
    plan = build_plan(captures, prior, references)
    out: dict[str, Any] = {
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
    if args.authorized:
        if args.ledger is None or args.output is None:
            raise ValueError("authorized execution requires --ledger and --output")
        import os

        import httpx

        from benchmarks.budget import BudgetLedger, BudgetPolicy
        from benchmarks.evaluation.deepeval_adapter import make_budgeted_judge

        ledger = BudgetLedger(args.ledger, BudgetPolicy(320, 500000, 800000))
        client = httpx.Client(timeout=httpx.Timeout(90, connect=10), trust_env=False)
        key = os.environ["DEEPSEEK_API_KEY"]
        model = os.environ.get("DEEPSEEK_MODEL_ID", "deepseek-flash")

        def generate(prompt: str) -> str:
            response = client.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "thinking": {"type": "disabled"},
                    "temperature": 0,
                    "max_tokens": 4096,
                    "stream": False,
                },
            )
            response.raise_for_status()
            body = response.json()
            usage = body.get("usage", {})
            ledger.reconcile(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
                cost_micro_usd=int(usage.get("prompt_tokens", 0))
                + 4 * int(usage.get("completion_tokens", 0)),
            )
            return str(body["choices"][0]["message"]["content"])

        def reserve_attempt() -> None:
            ledger.reserve_attempt()

        judge = make_budgeted_judge(
            generate=generate, name=model, reserve=reserve_attempt
        )
        state = execute_missing(
            captures=captures,
            prior=prior,
            references=references,
            evaluator=real_evaluator(judge),
            budget_check=lambda: True,
            output_path=args.output,
        )
        out["payment_executed"] = True
        out["executed_queries"] = len(state.completed)
        out["blocked"] = state.blocked
    args.plan.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


@dataclass
class ReplayState:
    prior: dict[str, dict[str, Any]]
    completed: dict[str, dict[str, Any]]
    blocked: list[str]


def execute_missing(
    *,
    captures: list[dict[str, Any]],
    prior: list[dict[str, Any]],
    references: dict[str, str],
    evaluator: Callable[[str, EvaluationRecord], MetricScore],
    budget_check: Callable[[], bool],
    output_path: Path,
) -> ReplayState:
    """Execute missing metrics only; persist after every completed evaluation."""
    by_id = {row["query_id"]: row for row in captures}
    state = ReplayState({}, {}, [])
    for row in prior:
        state.prior[row["query_id"]] = row
        state.completed[row["query_id"]] = {"scores": dict(row["scores"])}
    order = (
        ("faithfulness", "faithfulness"),
        ("answer_relevancy", "answer_relevancy"),
        ("contextual_relevancy", "contextual_relevancy"),
        ("contextual_precision", "contextual_precision"),
        ("contextual_recall", "contextual_recall"),
    )
    for metric_name, _ in order:
        for query_id in [row["query_id"] for row in captures]:
            previous = state.completed[query_id]["scores"].get(metric_name, {})
            if previous.get("status") == "EVALUATED":
                continue
            if not budget_check():
                state.blocked.append(f"{metric_name}:{query_id}:BUDGET_BLOCKED")
                continue
            record = EvaluationRecord(
                query_id=query_id,
                input=by_id[query_id]["input"],
                actual_output=by_id[query_id]["answer"],
                retrieval_context=tuple(by_id[query_id]["contexts"]),
                expected_output=references[query_id],
                status=by_id[query_id]["status"],
                runtime_fingerprint=by_id[query_id]["runtime_fingerprint"],
                evaluator_fingerprint="deepeval-4.2.4",
            )
            try:
                score = evaluator(metric_name, record)
                state.completed[query_id]["scores"][metric_name] = {
                    "score": score.score,
                    "reason": score.reason,
                    "status": score.status,
                }
            except Exception as error:
                state.completed[query_id]["scores"][metric_name] = {
                    "score": None,
                    "reason": type(error).__name__,
                    "status": "EVALUATION_ERROR",
                }
            output_path.write_text(
                json.dumps(
                    {
                        "captures_sha256": _sha(captures),
                        "results": list(state.completed.values()),
                        "blocked": state.blocked,
                    },
                    indent=2,
                )
                + "\n"
            )
    return state


def _sha(value: object) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def real_evaluator(judge: object) -> Callable[[str, EvaluationRecord], MetricScore]:
    from benchmarks.evaluation.deepeval_adapter import MetricScore, _metrics

    def evaluate(metric_name: str, record: EvaluationRecord) -> MetricScore:
        from deepeval.test_case import LLMTestCase  # type: ignore[import-not-found]

        test_case = LLMTestCase(
            input=record.input,
            actual_output=record.actual_output,
            expected_output=record.expected_output,
            retrieval_context=list(record.retrieval_context),
            context=list(record.retrieval_context),
        )
        metric = dict(_metrics(judge, 0.5))[metric_name]
        metric.measure(test_case)
        return MetricScore(metric_name, float(metric.score), metric.reason, "EVALUATED")

    return evaluate
