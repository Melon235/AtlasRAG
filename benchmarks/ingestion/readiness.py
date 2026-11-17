"""Corpus Readiness Gate; metadata validity is not corpus readiness."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from benchmarks.ingestion.runner import IngestionSummary


class ReadinessStatus(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"
    PARTIAL = "PARTIAL"


@dataclass(frozen=True)
class ProductionReadinessEvidence:
    canonical_lineage_verified: bool
    milvus_exact_set_verified: bool
    serving_safety: str
    published_source_paths: tuple[str, ...]
    pipeline_fingerprint_compatible: bool
    stage5_verified_documents: int
    verification_id: str


@dataclass(frozen=True)
class CorpusReadinessResult:
    status: ReadinessStatus
    expected_sources: int
    published_sources: int
    reasons: tuple[str, ...]


class CorpusReadinessGate:
    def assess(
        self,
        ingestion: IngestionSummary,
        evidence: ProductionReadinessEvidence,
        *,
        expected_source_paths: tuple[str, ...],
    ) -> CorpusReadinessResult:
        expected = tuple(sorted(expected_source_paths))
        reasons: list[str] = list(ingestion.failure_reasons)
        if not ingestion.complete:
            reasons.append("INGESTION_INCOMPLETE")
        if tuple(sorted(evidence.published_source_paths)) != expected:
            reasons.append("PUBLISHED_SOURCE_SET_MISMATCH")
        if not evidence.canonical_lineage_verified:
            reasons.append("CANONICAL_LINEAGE_UNVERIFIED")
        if not evidence.milvus_exact_set_verified:
            reasons.append("MILVUS_EXACT_SET_UNVERIFIED")
        if evidence.serving_safety != "SAFE_TO_SERVE":
            reasons.append("STAGE5_NOT_SAFE_TO_SERVE")
        if not evidence.pipeline_fingerprint_compatible:
            reasons.append("PIPELINE_FINGERPRINT_MISMATCH")
        if evidence.stage5_verified_documents != len(expected):
            reasons.append("STAGE5_CORPUS_COUNT_MISMATCH")
        if not evidence.verification_id:
            reasons.append("STAGE5_VERIFICATION_UNBOUND")
        status = (
            ReadinessStatus.READY
            if not reasons
            else ReadinessStatus.PARTIAL
            if ingestion.records
            else ReadinessStatus.NOT_READY
        )
        return CorpusReadinessResult(
            status=status,
            expected_sources=len(expected),
            published_sources=len(evidence.published_source_paths),
            reasons=tuple(sorted(set(reasons))),
        )
