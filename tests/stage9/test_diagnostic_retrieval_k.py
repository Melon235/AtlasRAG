from benchmarks.contracts.models import RankedUnit
from benchmarks.diagnostic_retrieval import _stage_metrics


def test_diagnostic_reporting_uses_independent_k_values() -> None:
    ranked = [
        RankedUnit(identity="c1", document_id="distractor", rank=1, score=3.0),
        RankedUnit(identity="c2", document_id="distractor2", rank=2, score=2.0),
        RankedUnit(identity="c3", document_id="gold", rank=3, score=1.0),
        RankedUnit(identity="c4", document_id="distractor4", rank=4, score=0.5),
        RankedUnit(identity="c5", document_id="distractor5", rank=5, score=0.5),
        RankedUnit(identity="c6", document_id="distractor6", rank=6, score=0.5),
        RankedUnit(identity="c7", document_id="distractor7", rank=7, score=0.5),
        RankedUnit(identity="c8", document_id="gold8", rank=8, score=0.5),
    ]
    metrics = _stage_metrics(ranked, {"gold": 1})
    assert metrics["hit1"] == 0.0
    assert metrics["hit5"] == 1.0
    assert metrics["hit10"] == 1.0
    assert metrics["recall5"] == 1.0
    assert metrics["recall10"] == 1.0
    assert metrics["mrr10"] > 0.0
