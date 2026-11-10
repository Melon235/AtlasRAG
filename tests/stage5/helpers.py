"""Small immutable fixtures for Stage 5 planner/scanner tests."""

from __future__ import annotations

from pathlib import Path

from atlasrag.domain.ingestion.source import (
    SourceFile,
    derive_document_id,
    derive_revision_id,
)
from atlasrag.repositories.postgres.records import FailedStage, IngestionStatus
from atlasrag.services.ingestion.identity import content_hash
from atlasrag.services.sync.models import (
    CanonicalDatabaseSnapshot,
    DatabaseDocumentSnapshot,
    ScanEntry,
    ScanSnapshot,
)

PIPELINE = "a" * 64


def entry(root: Path, relative_path: str, content: str) -> ScanEntry:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    source = SourceFile.from_knowledge_directory(root, relative_path)
    return ScanEntry(source=source, content_hash=content_hash(source))


def scan(*entries: ScanEntry) -> ScanSnapshot:
    return ScanSnapshot(
        entries=tuple(sorted(entries, key=lambda e: e.source.relative_source_path))
    )


def document(
    entry_value: ScanEntry,
    *,
    current: bool = True,
    status: IngestionStatus | None = None,
    failed_stage: FailedStage | None = None,
    current_hash: str | None = None,
    current_pipeline: str = PIPELINE,
    building_hash: str | None = None,
    building_pipeline: str | None = None,
) -> DatabaseDocumentSnapshot:
    path = entry_value.source.relative_source_path
    doc_id = derive_document_id(path)
    content = entry_value.content_hash
    if current:
        revision = derive_revision_id(doc_id, current_hash or content, current_pipeline)
        return DatabaseDocumentSnapshot(
            document_id=doc_id,
            relative_source_path=path,
            file_name=entry_value.source.file_name,
            source_type=entry_value.source.source_type,
            current_revision_id=revision,
            current_content_hash=current_hash or content,
            current_pipeline_fingerprint=current_pipeline,
            ingestion_status=status or IngestionStatus.READY,
            failed_stage=failed_stage,
        )
    effective_status = status or IngestionStatus.RECEIVED
    building_content = building_hash or content
    building_pipeline = building_pipeline or current_pipeline
    revision = derive_revision_id(doc_id, building_content, building_pipeline)
    return DatabaseDocumentSnapshot(
        document_id=doc_id,
        relative_source_path=path,
        file_name=entry_value.source.file_name,
        source_type=entry_value.source.source_type,
        building_revision_id=revision,
        building_content_hash=building_content,
        building_pipeline_fingerprint=building_pipeline,
        ingestion_status=effective_status,
        failed_stage=failed_stage,
    )


def database(*documents: DatabaseDocumentSnapshot) -> CanonicalDatabaseSnapshot:
    return CanonicalDatabaseSnapshot(
        documents=tuple(sorted(documents, key=lambda d: d.relative_source_path))
    )
