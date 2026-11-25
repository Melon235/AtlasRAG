from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from atlasrag.domain.enums import SourceType
from atlasrag.repositories.postgres.records import IngestionStatus, StoredDocument
from atlasrag.runtime.health import DependencyHealth, HealthRegistry
from atlasrag.services.sync.models import SourceAvailability
from atlasrag.services.sync.source_safety import StartupSourceSafetyService


class _Documents:
    def __init__(self, documents: tuple[StoredDocument, ...]) -> None:
        self._documents = documents

    async def list(self) -> tuple[StoredDocument, ...]:
        return self._documents


class _Unit:
    def __init__(self, documents: _Documents) -> None:
        self.documents = documents

    async def __aenter__(self) -> _Unit:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _Factory:
    def __init__(self, documents: tuple[StoredDocument, ...]) -> None:
        self._documents = documents

    def __call__(self) -> _Unit:
        return _Unit(_Documents(self._documents))


class _Classification:
    def __init__(self, availability: SourceAvailability) -> None:
        self._availability = availability

    def classify_source(
        self, document: StoredDocument
    ) -> tuple[SourceAvailability, None]:
        return self._availability, None


def _document() -> StoredDocument:
    return StoredDocument(
        document_id="d" * 64,
        file_name="guide.md",
        relative_source_path="guide.md",
        source_type=SourceType.MD,
        observed_content_hash="a" * 64,
        current_revision_id="r" * 64,
        current_content_hash="a" * 64,
        current_pipeline_fingerprint="f" * 64,
        ingestion_status=IngestionStatus.READY,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_source_safety_blocks_missing_published_source(tmp_path: Path) -> None:
    service = StartupSourceSafetyService(
        _Factory((_document(),)),  # type: ignore[arg-type]
        knowledge_directory=tmp_path,
        reconciliation=_Classification(SourceAvailability.MISSING),  # type: ignore[arg-type]
    )
    result = await service.check()
    assert not result.safe
    assert result.missing_documents == 1
    assert result.findings[0].availability is SourceAvailability.MISSING


@pytest.mark.asyncio
async def test_source_safety_allows_available_changed_source(tmp_path: Path) -> None:
    service = StartupSourceSafetyService(
        _Factory((_document(),)),  # type: ignore[arg-type]
        knowledge_directory=tmp_path,
        reconciliation=_Classification(SourceAvailability.AVAILABLE),  # type: ignore[arg-type]
    )
    result = await service.check()
    assert result.safe
    assert result.findings == ()


@pytest.mark.asyncio
async def test_health_registry_reports_soft_unknown_without_blocking() -> None:
    async def healthy() -> None:
        return None

    registry = HealthRegistry(
        mode=lambda: "SERVING",  # type: ignore[arg-type,return-value]
        postgres=healthy,
        milvus=healthy,
        redis=healthy,
        embedding=healthy,
        searxng=None,
        reranker=None,
        probe_timeout=0.1,
        refresh_interval=10,
    )
    registry.set_knowledge_verified(True)
    snapshot = await registry.refresh()
    assert snapshot.hard_healthy
    assert snapshot.searxng is DependencyHealth.UNKNOWN
