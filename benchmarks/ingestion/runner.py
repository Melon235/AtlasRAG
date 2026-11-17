"""Benchmark ingestion orchestration over the real Part I build boundary."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from atlasrag.domain.ingestion.source import SourceFile
from atlasrag.graphs.indexing.contracts import BuildRequest, BuildResult


class BuildBackend(Protocol):
    async def build(self, request: BuildRequest) -> BuildResult: ...


@dataclass(frozen=True)
class IngestionRecord:
    relative_source_path: str
    outcome: str
    document_id: str | None = None
    revision_id: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class IngestionSummary:
    records: tuple[IngestionRecord, ...]
    expected_count: int
    complete: bool
    failure_reasons: tuple[str, ...]


class BenchmarkIngestionRunner:
    """Iterates an exact declared source set; build/publish stays in Part I."""

    def __init__(self, backend: BuildBackend) -> None:
        self._backend = backend

    async def ingest(
        self,
        knowledge_directory: Path,
        expected_relative_paths: Sequence[str],
        *,
        expected_pipeline_fingerprint: str,
    ) -> IngestionSummary:
        expected = tuple(sorted(expected_relative_paths))
        if not expected or len(set(expected)) != len(expected):
            raise ValueError("expected source paths must be nonempty and unique")
        root = knowledge_directory.resolve()
        actual = tuple(
            sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
            )
        )
        if actual != expected:
            return IngestionSummary(
                records=(),
                expected_count=len(expected),
                complete=False,
                failure_reasons=("SOURCE_SET_MISMATCH",),
            )
        reasons: list[str] = []
        records: list[IngestionRecord] = []
        for relative in expected:
            source = SourceFile.from_knowledge_directory(root, relative)
            if source.relative_source_path != relative:
                raise ValueError("source path canonicalization mismatch")
            try:
                result = await self._backend.build(
                    BuildRequest(
                        source=source,
                        expected_pipeline_fingerprint=expected_pipeline_fingerprint,
                    )
                )
            except Exception as error:
                records.append(
                    IngestionRecord(
                        relative_source_path=relative,
                        outcome="TECHNICAL_FAILURE",
                        failure_reason=type(error).__name__,
                    )
                )
                reasons.append(f"{relative}:TECHNICAL_FAILURE")
                continue
            records.append(
                IngestionRecord(
                    relative_source_path=relative,
                    outcome=result.outcome,
                    document_id=result.document_id,
                    revision_id=result.revision_id,
                )
            )
            if result.outcome != "READY":
                reasons.append(f"{relative}:{result.outcome}")
        return IngestionSummary(
            records=tuple(records),
            expected_count=len(expected),
            complete=not reasons
            and len(records) == len(expected)
            and all(item.outcome == "READY" for item in records),
            failure_reasons=tuple(reasons),
        )
