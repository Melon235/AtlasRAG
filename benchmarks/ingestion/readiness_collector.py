"""Collect readiness facts from actual production records and Stage 5 result."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from hashlib import sha256
from typing import Any, Protocol
from uuid import uuid4

from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.ingestion.source import derive_document_id, derive_revision_id
from atlasrag.repositories.postgres.records import IngestionStatus, StoredDocument
from atlasrag.services.sync.models import GlobalSafetyResult, ServingSafety
from benchmarks.ingestion.readiness import ProductionReadinessEvidence


class ReadinessDocuments(Protocol):
    async def list(self) -> Sequence[Any]: ...


class ReadinessUnit(Protocol):
    documents: ReadinessDocuments

    async def __aenter__(self) -> ReadinessUnit: ...

    async def __aexit__(self, *args: object) -> None: ...


async def collect_production_readiness(
    uow_factory: Callable[[], ReadinessUnit],
    *,
    runtime: RuntimeConfig,
    verifier: Callable[[], Awaitable[GlobalSafetyResult]],
    expected_source_paths: tuple[str, ...],
    expected_pipeline_fingerprint: str,
    snapshot_identity: str,
) -> ProductionReadinessEvidence:
    expected = tuple(sorted(expected_source_paths))
    if not expected or len(set(expected)) != len(expected):
        raise ValueError("expected source paths must be nonempty and unique")
    verification = await verifier()
    published: list[str] = []
    lineage_ok = True
    async with uow_factory() as unit:
        documents = await unit.documents.list()
        by_path = {stored.relative_source_path: stored for stored in documents}
        if set(by_path) != set(expected):
            lineage_ok = False
        for stored in documents:
            if stored.current_revision_id is not None:
                published.append(stored.relative_source_path)
            if stored.relative_source_path not in set(expected):
                lineage_ok = False
        for relative in expected:
            stored = by_path.get(relative)
            if stored is None:
                lineage_ok = False
                continue
            if not _canonical_current(
                stored,
                expected_pipeline_fingerprint=expected_pipeline_fingerprint,
            ):
                lineage_ok = False
    pipeline_compatible = pipeline_fingerprint(
        runtime
    ) == expected_pipeline_fingerprint and all(
        stored.current_pipeline_fingerprint == expected_pipeline_fingerprint
        for stored in by_path.values()
    )
    stage5_safe = verification.serving_safety is ServingSafety.SAFE_TO_SERVE
    stage5_consistent = (
        verification.verified_documents == len(expected)
        and not verification.unsafe_reasons
        and verification.required_deletion_failures == 0
    )
    if not snapshot_identity.strip():
        raise ValueError("snapshot_identity must be nonempty")
    evidence_payload = (
        expected,
        expected_pipeline_fingerprint,
        snapshot_identity,
        verification.serving_safety.value,
        verification.verified_documents,
        verification.verified_records,
        tuple(
            (
                stored.relative_source_path,
                stored.document_id,
                stored.current_revision_id,
                stored.current_pipeline_fingerprint,
            )
            for stored in by_path.values()
        ),
    )
    verification_digest = sha256(repr(evidence_payload).encode("utf-8")).hexdigest()
    verification_id = verification_digest
    return ProductionReadinessEvidence(
        canonical_lineage_verified=lineage_ok,
        milvus_exact_set_verified=stage5_safe and stage5_consistent,
        serving_safety=(
            "SAFE_TO_SERVE" if stage5_safe and stage5_consistent else "UNSAFE_TO_SERVE"
        ),
        published_source_paths=tuple(sorted(published)),
        pipeline_fingerprint_compatible=pipeline_compatible,
        stage5_verified_documents=verification.verified_documents,
        verification_id=verification_id,
        verification_execution_id=uuid4().hex,
        verification_evidence_digest=verification_digest,
        snapshot_identity=snapshot_identity,
    )


def _canonical_current(
    stored: StoredDocument, *, expected_pipeline_fingerprint: str
) -> bool:
    if stored.current_revision_id is None:
        return False
    if (
        stored.current_content_hash is None
        or stored.current_pipeline_fingerprint is None
    ):
        return False
    if stored.current_pipeline_fingerprint != expected_pipeline_fingerprint:
        return False
    if stored.building_revision_id is not None:
        return False
    if stored.ingestion_status is not IngestionStatus.READY:
        return False
    if stored.failed_stage is not None:
        return False
    document_id = derive_document_id(stored.relative_source_path)
    if stored.document_id != document_id:
        return False
    return (
        derive_revision_id(
            document_id,
            stored.current_content_hash,
            stored.current_pipeline_fingerprint,
        )
        == stored.current_revision_id
    )
