from benchmarks.evaluation.judge_replay import build_plan


def test_judge_plan_preserves_prior_scores_and_lists_only_missing() -> None:
    captures = [{"query_id": f"q{i}"} for i in range(20)]
    prior = [
        {
            "query_id": f"q{i}",
            "scores": {
                metric: {
                    "status": "EVALUATED"
                    if metric == "faithfulness"
                    else "EVALUATION_ERROR"
                }
                for metric in (
                    "faithfulness",
                    "answer_relevancy",
                    "contextual_relevancy",
                    "contextual_precision",
                    "contextual_recall",
                )
            },
        }
        for i in range(20)
    ]
    refs = {f"q{i}": "reference" for i in range(20)}
    plan = build_plan(captures, prior, refs)
    assert plan.missing["faithfulness"] == []
    assert len(plan.missing["answer_relevancy"]) == 20
    assert plan.attempts_estimate > 0
