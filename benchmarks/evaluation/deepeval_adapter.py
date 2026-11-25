"""Minimal DeepEval adapter for benchmark-owned captured production answers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class DeepEvalUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvaluationRecord:
    query_id: str
    input: str
    actual_output: str | None
    retrieval_context: tuple[str, ...]
    expected_output: str | None
    status: str
    runtime_fingerprint: str
    evaluator_fingerprint: str

    def validate(self) -> str | None:
        if self.status != "COMPLETED":
            return f"case status is {self.status}"
        if not self.actual_output:
            return "missing actual answer"
        if not self.retrieval_context:
            return "missing production retrieval contexts"
        return None


@dataclass(frozen=True)
class MetricScore:
    metric: str
    score: float | None
    reason: str | None
    status: str


def _metrics(judge: object, threshold: float) -> list[tuple[str, Any]]:
    try:
        from deepeval.metrics import (  # type: ignore[import-not-found]
            AnswerRelevancyMetric,
            ContextualPrecisionMetric,
            ContextualRecallMetric,
            ContextualRelevancyMetric,
            FaithfulnessMetric,
        )
    except ImportError as error:
        raise DeepEvalUnavailableError("deepeval is not installed") from error
    return [
        ("faithfulness", FaithfulnessMetric(threshold=threshold, model=judge)),
        ("answer_relevancy", AnswerRelevancyMetric(threshold=threshold, model=judge)),
        (
            "contextual_relevancy",
            ContextualRelevancyMetric(threshold=threshold, model=judge),
        ),
        (
            "contextual_precision",
            ContextualPrecisionMetric(threshold=threshold, model=judge),
        ),
        ("contextual_recall", ContextualRecallMetric(threshold=threshold, model=judge)),
    ]


def evaluate_record(
    record: EvaluationRecord,
    judge: object,
    *,
    threshold: float = 0.5,
    metric_factory: Callable[[object, float], list[tuple[str, Any]]] | None = None,
    test_case_factory: Callable[..., object] | None = None,
) -> list[MetricScore]:
    unavailable = record.validate()
    if unavailable is not None:
        return [MetricScore("deepeval", None, unavailable, "NOT_EVALUABLE")]
    if test_case_factory is None:
        try:
            from deepeval.test_case import LLMTestCase  # type: ignore[import-not-found]
        except ImportError as error:
            raise DeepEvalUnavailableError("deepeval is not installed") from error
        test_case_factory = LLMTestCase
    test_case = test_case_factory(
        input=record.input,
        actual_output=record.actual_output,
        expected_output=record.expected_output,
        retrieval_context=list(record.retrieval_context),
        context=list(record.retrieval_context),
    )
    output: list[MetricScore] = []
    metrics = (
        metric_factory(judge, threshold)
        if metric_factory is not None
        else _metrics(judge, threshold)
    )
    for name, metric in metrics:
        try:
            metric.measure(test_case)
            output.append(
                MetricScore(name, float(metric.score), metric.reason, "EVALUATED")
            )
        except Exception as error:
            output.append(
                MetricScore(name, None, type(error).__name__, "EVALUATION_ERROR")
            )
    return output


def make_budgeted_judge(
    *, generate: Callable[[str], Any], name: str, reserve: Callable[[], None]
) -> object:
    """Build a DeepEvalBaseLLM wrapper; reserve is called before every judge call."""
    try:
        from deepeval.models.base_model import (  # type: ignore[import-not-found]
            DeepEvalBaseLLM,
        )
    except ImportError as error:
        raise DeepEvalUnavailableError("deepeval is not installed") from error

    class _Judge(DeepEvalBaseLLM):  # type: ignore[misc]
        def __init__(self) -> None:
            self._name = name

        def load_model(self, *args: object, **kwargs: object) -> object:
            return self

        def generate(self, *args: object, **kwargs: object) -> str:
            reserve()
            prompt = str(args[0]) if args else str(kwargs.get("prompt", ""))
            value = generate(prompt)
            if asyncio.iscoroutine(value):
                value = asyncio.run(value)
            return str(value)

        async def a_generate(self, *args: object, **kwargs: object) -> str:
            reserve()
            prompt = str(args[0]) if args else str(kwargs.get("prompt", ""))
            value = generate(prompt)
            if asyncio.iscoroutine(value):
                value = await value
            return str(value)

        def get_model_name(self, *args: object, **kwargs: object) -> str:
            return self._name

    return _Judge()
