from __future__ import annotations

import pytest

from benchmarks.alignment.gold import align_gold_section, alignment_coverage
from benchmarks.contracts.models import (
    AlignmentConfidence,
    CanonicalEvidence,
    GoldSection,
    RankedUnit,
)
from benchmarks.metrics.retrieval import (
    document_ranking,
    evaluate_ranking,
    evaluate_relaxed_document,
    evaluate_strict_evidence,
)


def ranked(*values: tuple[str, str]) -> list[RankedUnit]:
    return [
        RankedUnit(identity=identity, document_id=document, rank=index)
        for index, (identity, document) in enumerate(values, start=1)
    ]


def test_metrics_perfect_partial_duplicate_and_no_hit() -> None:
    perfect = evaluate_ranking(
        ranked(("e1", "d1"), ("e2", "d2")), {"e1": 1, "e2": 1}, k=2
    )
    assert (perfect.hit, perfect.recall, perfect.mrr, perfect.ndcg) == (
        1.0,
        1.0,
        1.0,
        1.0,
    )
    partial = evaluate_ranking(
        ranked(("e2", "d2"), ("x", "d3"), ("e1", "d1")), {"e1": 1, "e3": 1}, k=3
    )
    assert (
        partial.hit == 1.0
        and partial.recall == 0.5
        and partial.mrr == pytest.approx(1 / 3)
    )
    duplicate = evaluate_ranking(
        ranked(("e1", "d1"), ("e1", "d1"), ("e2", "d2")), {"e1": 1, "e2": 1}, k=3
    )
    assert duplicate.recall == 1.0
    no_hit = evaluate_ranking(ranked(("x", "d3")), {"e1": 1}, k=5)
    assert no_hit.hit == 0.0 and no_hit.recall == 0.0 and no_hit.mrr == 0.0


def test_document_ranking_deduplicates_documents() -> None:
    values = document_ranking(ranked(("e1", "d1"), ("e2", "d1"), ("e3", "d2")))
    assert [item.identity for item in values] == ["d1", "d2"]
    assert [item.rank for item in values] == [1, 2]


def test_graded_ndcg_and_empty_relevance() -> None:
    result = evaluate_ranking(
        ranked(("e1", "d1"), ("e2", "d2")), {"e1": 2, "e2": 1}, k=2
    )
    assert 0.0 < result.ndcg <= 1.0
    with pytest.raises(ValueError):
        evaluate_ranking(ranked(), {}, k=5)


def test_alignment_one_to_many_medium_unresolved_and_coverage() -> None:
    gold = GoldSection(gold_section_id="s1", doc_id="d1", content="alpha beta gamma")
    evidence = (
        CanonicalEvidence(
            evidence_id="e1",
            document_id="d1",
            evidence_type="TEXT_PARENT",
            content="alpha beta gamma",
        ),
        CanonicalEvidence(
            evidence_id="e2",
            document_id="d1",
            evidence_type="TABLE",
            content="alpha beta gamma",
        ),
        CanonicalEvidence(
            evidence_id="e3",
            document_id="d2",
            evidence_type="TEXT_PARENT",
            content="alpha beta gamma",
        ),
    )
    high = align_gold_section(query_id="q1", gold=gold, candidates=evidence)
    assert high.confidence is AlignmentConfidence.HIGH
    assert high.aligned_evidence_ids == ("e1", "e2")
    medium_gold = GoldSection(
        gold_section_id="s2", doc_id="d1", content="alpha beta gamma delta epsilon"
    )
    medium = align_gold_section(query_id="q2", gold=medium_gold, candidates=evidence)
    assert (
        medium.confidence is AlignmentConfidence.MEDIUM
        and medium.aligned_evidence_ids == ()
    )
    unresolved = align_gold_section(
        query_id="q3",
        gold=GoldSection(gold_section_id="s3", doc_id="d3", content="zzz"),
        candidates=evidence,
    )
    assert unresolved.confidence is AlignmentConfidence.UNRESOLVED
    coverage = alignment_coverage((high, medium, unresolved))
    assert coverage["HIGH"] == 1 and coverage["UNRESOLVED"] == 1
    assert coverage["HIGH_rate"] == pytest.approx(1 / 3)


def test_strict_and_relaxed_are_separate_and_zero_grade_rejected() -> None:
    evidence = ranked(("e1", "d1"), ("e2", "d1"))
    strict = evaluate_strict_evidence(evidence, {"e1": 1}, k=2)
    relaxed = evaluate_relaxed_document(evidence, {"d1": 1}, k=1)
    assert strict.recall == 1.0 and relaxed.recall == 1.0
    with pytest.raises(ValueError, match="positive relevant unit"):
        evaluate_ranking(evidence, {"e1": 0}, k=2)
