from benchmarks.contracts.models import RankedUnit
from benchmarks.metrics.retrieval import evaluate_relaxed_document


def _ranked(rank: int) -> list[RankedUnit]:
    return [
        RankedUnit(
            identity=f"c{index}",
            document_id="gold" if index == rank else f"d{index}",
            rank=index,
            score=1.0,
        )
        for index in range(1, 12)
    ]


def test_k_specific_hit_and_recall_for_ranks_1_3_8_and_miss() -> None:
    assert evaluate_relaxed_document(_ranked(1), {"gold": 1}, k=1).hit == 1.0
    assert evaluate_relaxed_document(_ranked(3), {"gold": 1}, k=1).hit == 0.0
    assert evaluate_relaxed_document(_ranked(3), {"gold": 1}, k=5).hit == 1.0
    assert evaluate_relaxed_document(_ranked(8), {"gold": 1}, k=5).hit == 0.0
    assert evaluate_relaxed_document(_ranked(8), {"gold": 1}, k=10).hit == 1.0
    miss = evaluate_relaxed_document([], {"gold": 1}, k=10)
    assert miss.hit == 0.0 and miss.recall == 0.0
