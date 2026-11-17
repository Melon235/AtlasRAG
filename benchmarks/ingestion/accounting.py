"""Derive benchmark ingestion accounting from the actual Stage 5 result."""

from __future__ import annotations

from atlasrag.services.sync.models import SyncOutcome, SyncResult
from benchmarks.ingestion.runner import IngestionRecord, IngestionSummary


def ingestion_summary_from_sync_result(
    result: SyncResult, expected_source_paths: tuple[str, ...]
) -> IngestionSummary:
    expected = tuple(sorted(expected_source_paths))
    if result.outcome is SyncOutcome.SUCCESS and result.ready_builds == len(expected):
        records = tuple(IngestionRecord(path, "READY") for path in expected)
        return IngestionSummary(
            records=records,
            expected_count=len(expected),
            complete=True,
            failure_reasons=(),
        )
    reasons: list[str] = []
    if result.failed_builds:
        reasons.append("BUSINESS_FAILURES")
    if result.technical_build_failures:
        reasons.append("TECHNICAL_FAILURES")
    if result.source_mutated_builds:
        reasons.append("SOURCE_MUTATIONS")
    if result.ready_builds != len(expected):
        reasons.append("INCOMPLETE_CORPUS")
    return IngestionSummary(
        records=(),
        expected_count=len(expected),
        complete=False,
        failure_reasons=tuple(sorted(set(reasons))),
    )
