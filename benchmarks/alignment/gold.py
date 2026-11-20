"""Deterministic gold-section to canonical-evidence alignment."""

from __future__ import annotations

import re
from collections.abc import Sequence

from benchmarks.contracts.models import (
    AlignmentConfidence,
    CanonicalEvidence,
    GoldEvidenceAlignment,
    GoldSection,
)

_TOKEN = re.compile(r"[\w]+", re.UNICODE)
_HIGH = 0.80
_MEDIUM = 0.55


def _tokens(text: str) -> set[str]:
    return {token.casefold() for token in _TOKEN.findall(text)}


def similarity(left: str, right: str) -> float:
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def align_gold_section(
    *,
    query_id: str,
    gold: GoldSection,
    candidates: Sequence[CanonicalEvidence],
) -> GoldEvidenceAlignment:
    scored: list[tuple[float, CanonicalEvidence]] = []
    for candidate in candidates:
        if candidate.document_id != gold.doc_id:
            continue
        scored.append((similarity(gold.content, candidate.content), candidate))
    scored.sort(key=lambda item: (-item[0], item[1].evidence_id))
    best_score = scored[0][0] if scored else 0.0
    selected = tuple(
        candidate.evidence_id
        for score, candidate in scored
        if score >= _HIGH and (candidate.evidence_type in {"TEXT_PARENT", "TABLE"})
    )
    if selected:
        confidence = AlignmentConfidence.HIGH
    elif best_score >= _MEDIUM:
        confidence = AlignmentConfidence.MEDIUM
        selected = ()
    else:
        confidence = AlignmentConfidence.UNRESOLVED
        selected = ()
    return GoldEvidenceAlignment(
        query_id=query_id,
        gold_section_id=gold.gold_section_id,
        gold_document_id=gold.doc_id,
        aligned_evidence_ids=selected,
        confidence=confidence,
        score=min(1.0, max(0.0, best_score)),
    )


def alignment_coverage(
    alignments: Sequence[GoldEvidenceAlignment],
) -> dict[str, float | int]:
    total = len(alignments)
    if total == 0:
        return {
            "total": 0,
            "HIGH": 0,
            "MEDIUM": 0,
            "UNRESOLVED": 0,
            "HIGH_rate": 0.0,
            "MEDIUM_rate": 0.0,
            "UNRESOLVED_rate": 0.0,
        }
    counts = {
        confidence.value: sum(1 for item in alignments if item.confidence is confidence)
        for confidence in AlignmentConfidence
    }
    return {
        "total": total,
        **counts,
        "HIGH_rate": counts["HIGH"] / total,
        "MEDIUM_rate": counts["MEDIUM"] / total,
        "UNRESOLVED_rate": counts["UNRESOLVED"] / total,
    }
