"""Benchmark-owned capture of production answers and generation contexts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class CapturedAnswer:
    query_id: str
    original_query: str
    final_answer: str | None
    evidence_contexts: tuple[str, ...]
    citation_ids: tuple[str, ...]
    citation_valid: bool
    product_status: str
    reference_answer: str | None
    runtime_fingerprint: str
    evaluator_fingerprint: str = "deepeval-4.2.4"


def capture_answer(
    *,
    query_id: str,
    original_query: str,
    final_answer: str | None,
    evidence_contexts: tuple[str, ...],
    citation_ids: tuple[str, ...],
    citation_valid: bool,
    product_status: str,
    reference_answer: str | None,
    runtime_fingerprint: str,
) -> CapturedAnswer:
    if not query_id or not original_query:
        raise ValueError("query identity is required")
    if evidence_contexts and any(not text.strip() for text in evidence_contexts):
        raise ValueError("evidence contexts must contain actual text")
    return CapturedAnswer(
        query_id=query_id,
        original_query=original_query,
        final_answer=final_answer,
        evidence_contexts=evidence_contexts,
        citation_ids=citation_ids,
        citation_valid=citation_valid,
        product_status=product_status,
        reference_answer=reference_answer,
        runtime_fingerprint=runtime_fingerprint,
    )


def write_capture(path: Path, records: tuple[CapturedAnswer, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(asdict(record), sort_keys=True) for record in records)
        + "\n",
        encoding="utf-8",
    )
