import json
from pathlib import Path

from benchmarks.evaluation.deepeval_adapter import EvaluationRecord, MetricScore
from benchmarks.evaluation.judge_replay import execute_missing


def test_execute_skips_successes_and_persists_missing(tmp_path: Path) -> None:
    captures = [
        {
            "query_id": "q1",
            "input": "question",
            "answer": "answer",
            "contexts": ["context"],
            "status": "COMPLETED",
            "runtime_fingerprint": "runtime",
        }
    ]
    prior = [
        {
            "query_id": "q1",
            "scores": {
                "faithfulness": {"score": 1.0, "reason": "old", "status": "EVALUATED"},
                "answer_relevancy": {
                    "score": None,
                    "reason": "budget",
                    "status": "EVALUATION_ERROR",
                },
                "contextual_relevancy": {
                    "score": None,
                    "reason": "budget",
                    "status": "EVALUATION_ERROR",
                },
                "contextual_precision": {
                    "score": None,
                    "reason": "budget",
                    "status": "EVALUATION_ERROR",
                },
                "contextual_recall": {
                    "score": None,
                    "reason": "budget",
                    "status": "EVALUATION_ERROR",
                },
            },
        }
    ]
    calls = []

    def evaluator(name: str, record: EvaluationRecord) -> MetricScore:
        calls.append(name)
        return MetricScore(name, 0.5, "ok", "EVALUATED")

    out = tmp_path / "out.json"
    state = execute_missing(
        captures=captures,
        prior=prior,
        references={"q1": "ref"},
        evaluator=evaluator,
        budget_check=lambda: True,
        output_path=out,
    )
    assert state.completed["q1"]["scores"]["faithfulness"]["score"] == 1.0
    assert state.completed["q1"]["scores"]["answer_relevancy"]["score"] == 0.5
    assert "faithfulness" not in calls
    assert len(calls) == 4
    assert (
        json.loads(out.read_text())["results"][0]["scores"]["faithfulness"]["score"]
        == 1.0
    )
