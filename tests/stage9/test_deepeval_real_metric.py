import pytest


def test_real_deepeval_faithfulness_with_mocked_judge() -> None:
    pytest.importorskip("deepeval")
    from deepeval.metrics import FaithfulnessMetric  # type: ignore[import-not-found]
    from deepeval.models.base_model import (  # type: ignore[import-not-found]
        DeepEvalBaseLLM,
    )
    from deepeval.test_case import LLMTestCase  # type: ignore[import-not-found]

    class FakeJudge(DeepEvalBaseLLM):  # type: ignore[misc]
        def __init__(self) -> None:
            self.calls = 0

        def load_model(self, *args: object, **kwargs: object) -> object:
            return self

        def generate(self, *args: object, **kwargs: object) -> str:
            self.calls += 1
            return (
                '{"truths":["answer"],"claims":["answer"],'
                '"verdicts":[{"claim":"answer","verdict":"yes","reason":"supported"}],'
                '"reason":"supported"}'
            )

        async def a_generate(self, *args: object, **kwargs: object) -> str:
            return self.generate(*args, **kwargs)

        def get_model_name(self, *args: object, **kwargs: object) -> str:
            return "mocked-deepeval-judge"

    judge = FakeJudge()
    metric = FaithfulnessMetric(model=judge, threshold=0.5, async_mode=False)
    metric.measure(
        LLMTestCase(
            input="question", actual_output="answer", retrieval_context=["answer"]
        )
    )
    assert metric.score == 1.0
    assert judge.calls >= 1
