"""Deterministic retrieval metrics with explicit ranked-unit identity."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from benchmarks.contracts.models import RankedUnit


@dataclass(frozen=True)
class RetrievalMetrics:
    hit: float
    recall: float
    mrr: float
    ndcg: float


def _dedupe(ranked: Sequence[RankedUnit]) -> tuple[RankedUnit, ...]:
    seen: set[str] = set()
    values: list[RankedUnit] = []
    for item in ranked:
        if item.identity in seen:
            continue
        seen.add(item.identity)
        values.append(item)
    return tuple(values)


def evaluate_ranking(
    ranked: Sequence[RankedUnit],
    relevant: Mapping[str, int],
    *,
    k: int,
) -> RetrievalMetrics:
    if type(k) is not int or k < 1:
        raise ValueError("k must be a positive integer")
    if not relevant:
        raise ValueError("relevant mapping must not be empty")
    ordered = _dedupe(ranked)[:k]
    relevant_ids = {identity for identity, grade in relevant.items() if grade > 0}
    if not relevant_ids:
        raise ValueError("at least one positive relevant unit is required")
    hit = 1.0 if any(item.identity in relevant_ids for item in ordered) else 0.0
    found = {item.identity for item in ordered if item.identity in relevant_ids}
    recall = len(found) / len(relevant_ids)
    mrr = 0.0
    for index, item in enumerate(ordered, start=1):
        if item.identity in relevant_ids:
            mrr = 1.0 / index
            break
    dcg = sum(
        (2 ** relevant.get(item.identity, 0) - 1) / math.log2(index + 1)
        for index, item in enumerate(ordered, start=1)
    )
    ideal_grades = sorted(relevant.values(), reverse=True)[:k]
    idcg = sum(
        (2**grade - 1) / math.log2(index + 1)
        for index, grade in enumerate(ideal_grades, start=1)
    )
    ndcg = dcg / idcg if idcg else 0.0
    return RetrievalMetrics(hit=hit, recall=recall, mrr=mrr, ndcg=ndcg)


def document_ranking(ranked: Sequence[RankedUnit]) -> tuple[RankedUnit, ...]:
    """Collapse child/evidence hits to one rank per document, preserving order."""
    seen: set[str] = set()
    values: list[RankedUnit] = []
    for item in ranked:
        if item.document_id in seen:
            continue
        seen.add(item.document_id)
        values.append(
            RankedUnit(
                identity=item.document_id,
                document_id=item.document_id,
                rank=len(values) + 1,
                score=item.score,
            )
        )
    return tuple(values)


def aggregate(metrics: Sequence[RetrievalMetrics]) -> RetrievalMetrics:
    if not metrics:
        raise ValueError("at least one metric is required")
    count = len(metrics)
    return RetrievalMetrics(
        hit=sum(item.hit for item in metrics) / count,
        recall=sum(item.recall for item in metrics) / count,
        mrr=sum(item.mrr for item in metrics) / count,
        ndcg=sum(item.ndcg for item in metrics) / count,
    )


def evaluate_strict_evidence(
    ranked: Sequence[RankedUnit], aligned_relevant: Mapping[str, int], *, k: int
) -> RetrievalMetrics:
    """Strict metrics over HIGH-aligned canonical evidence identities."""
    return evaluate_ranking(ranked, aligned_relevant, k=k)


def evaluate_relaxed_document(
    ranked: Sequence[RankedUnit], relevant_documents: Mapping[str, int], *, k: int
) -> RetrievalMetrics:
    """Relaxed metrics over unique document identities, never mixed with strict."""
    return evaluate_ranking(document_ranking(ranked), relevant_documents, k=k)
