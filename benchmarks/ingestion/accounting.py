"""Separate actual build observations from aggregate sync counters and state."""

from __future__ import annotations

from dataclasses import dataclass

from atlasrag.services.sync.models import SyncResult
from benchmarks.ingestion.runner import IngestionRecord


@dataclass(frozen=True)
class IngestionAccounting:
    """No per-source READY record is synthesized from an aggregate count."""

    build_observations: tuple[IngestionRecord, ...]
    sync_outcome: str | None
    sync_ready_builds: int
    sync_failed_builds: int
    sync_technical_failures: int
    sync_source_mutations: int
    verified_published_paths: tuple[str, ...]
    unchanged_source_paths: tuple[str, ...]
    expected_source_paths: tuple[str, ...]

    @classmethod
    def from_actual(
        cls,
        build_observations: tuple[IngestionRecord, ...],
        *,
        sync_result: SyncResult | None,
        verified_published_paths: tuple[str, ...],
        expected_source_paths: tuple[str, ...],
        unchanged_source_paths: tuple[str, ...] = (),
    ) -> IngestionAccounting:
        return cls(
            build_observations=build_observations,
            sync_outcome=None if sync_result is None else sync_result.outcome.value,
            sync_ready_builds=0 if sync_result is None else sync_result.ready_builds,
            sync_failed_builds=0 if sync_result is None else sync_result.failed_builds,
            sync_technical_failures=(
                0 if sync_result is None else sync_result.technical_build_failures
            ),
            sync_source_mutations=(
                0 if sync_result is None else sync_result.source_mutated_builds
            ),
            verified_published_paths=verified_published_paths,
            unchanged_source_paths=unchanged_source_paths,
            expected_source_paths=expected_source_paths,
        )

    @property
    def complete(self) -> bool:
        expected = set(self.expected_source_paths)
        if not expected:
            return False
        if (
            self.sync_failed_builds
            or self.sync_technical_failures
            or self.sync_source_mutations
        ):
            return False
        if any(item.outcome != "READY" for item in self.build_observations):
            return False
        return set(self.verified_published_paths) == expected
