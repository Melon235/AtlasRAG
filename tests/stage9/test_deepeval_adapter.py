import pytest

from benchmarks.evaluation.deepeval_adapter import (
    DeepEvalUnavailableError,
    EvaluationRecord,
    evaluate_record,
)


def record(
    status: str = "COMPLETED",
    answer: str | None = "Answer",
    context: tuple[str, ...] = ("ctx",),
) -> EvaluationRecord:
    return EvaluationRecord(
        query_id="q1",
        input="question",
        actual_output=answer,
        retrieval_context=context,
        expected_output="ref",
        status=status,
        runtime_fingerprint="runtime",
        evaluator_fingerprint="deepeval-4.2.4",
    )


def test_missing_answer_is_not_evaluable_without_deepeval() -> None:
    scores = evaluate_record(record(answer=None), object())
    assert scores[0].status == "NOT_EVALUABLE"
    assert scores[0].score is None
    assert "missing actual answer" in (scores[0].reason or "")


def test_adapter_requires_deepeval_when_record_is_complete() -> None:
    try:
        import deepeval  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        with pytest.raises(DeepEvalUnavailableError):
            evaluate_record(record(), object())
    else:
        pytest.skip(
            "DeepEval is installed in this environment; see installed-adapter test"
        )


def test_complete_fake_metric_evaluation_path() -> None:
    class Metric:
        score = 0.75
        reason = "fake judge"

        def measure(self, test_case: object) -> None:
            assert test_case is not None

    scores = evaluate_record(
        record(),
        object(),
        metric_factory=lambda judge, threshold: [("faithfulness", Metric())],
        test_case_factory=lambda **kwargs: kwargs,
    )
    assert scores[0].metric == "faithfulness"
    assert scores[0].score == 0.75
    assert scores[0].status == "EVALUATED"
