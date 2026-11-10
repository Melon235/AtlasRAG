"""Global published-knowledge verification against the derived index."""

from __future__ import annotations

import json
from contextlib import aclosing
from dataclasses import dataclass

from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import CanonicalDataError
from atlasrag.domain.indexing.models import RepresentationProfileV1
from atlasrag.domain.ingestion.chunks import ChunkRecord, ChunkType
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.validation import ValidationOutcome
from atlasrag.repositories.postgres.records import StoredDocument
from atlasrag.services.indexing.records import build_expected_projection
from atlasrag.services.indexing.representation import RepresentationBuilder
from atlasrag.services.ingestion.mapping import (
    map_chunks_from_stored,
    map_document_from_stored,
)
from atlasrag.services.ingestion.validation import validate_chunk_set
from atlasrag.services.sync.models import (
    BuildExecutionSummary,
    DeletionSummary,
    GlobalSafetyResult,
    ServingSafety,
)
from atlasrag.services.sync.ports import SyncUnitOfWork, SyncUnitOfWorkFactory


def _json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def expected_current_projection(
    document: DocumentContainer,
    chunks: tuple[ChunkRecord, ...],
    *,
    representation_version: str,
) -> dict[str, str]:
    builder = RepresentationBuilder(
        RepresentationProfileV1(), representation_version=representation_version
    )
    return {
        chunk.chunk_id: _json(
            build_expected_projection(document, chunk, builder.build(chunk)).model_dump(
                mode="json"
            )
        )
        for chunk in chunks
        if chunk.chunk_type is not ChunkType.TEXT_PARENT
    }


@dataclass(frozen=True)
class PublishedExpectation:
    document_id: str
    revision_id: str
    projection: dict[str, str]


async def load_published_expectations(
    uow_factory: SyncUnitOfWorkFactory,
    runtime: RuntimeConfig,
) -> tuple[PublishedExpectation, ...]:
    expectations: list[PublishedExpectation] = []
    async with uow_factory() as unit:
        documents = await unit.documents.list()
        for stored in documents:
            if stored.current_revision_id is None:
                continue
            expected = await _load_one(
                unit, stored, stored.current_revision_id, runtime
            )
            expectations.append(expected)
    return tuple(sorted(expectations, key=lambda item: item.document_id))


async def _load_one(
    unit: SyncUnitOfWork,
    stored: StoredDocument,
    revision_id: str,
    runtime: RuntimeConfig,
) -> PublishedExpectation:
    elements_repo = unit.elements
    chunks_repo = unit.chunks
    elements = await elements_repo.list_for_revision(stored.document_id, revision_id)
    stored_chunks = await chunks_repo.list_for_revision(stored.document_id, revision_id)
    try:
        document = map_document_from_stored(stored, elements, revision_id=revision_id)
        chunks = map_chunks_from_stored(stored_chunks)
        if validate_chunk_set(document, chunks).outcome is ValidationOutcome.FAIL:
            raise CanonicalDataError
    except (CanonicalDataError, ValueError):
        raise
    return PublishedExpectation(
        document_id=stored.document_id,
        revision_id=revision_id,
        projection=expected_current_projection(
            document,
            chunks,
            representation_version=runtime.part_i.representation_version,
        ),
    )


class GlobalKnowledgeVerificationService:
    """Compare every published revision and the complete actual row set."""

    def __init__(
        self,
        uow_factory: SyncUnitOfWorkFactory,
        index: object,
        runtime: RuntimeConfig,
    ) -> None:
        self._uow_factory = uow_factory
        self._index = index
        self._runtime = RuntimeConfig.model_validate(runtime)

    async def verify(
        self,
        *,
        deletion_summary: DeletionSummary,
        build_summary: BuildExecutionSummary,
    ) -> GlobalSafetyResult:
        reasons: set[str] = set()
        expected: list[PublishedExpectation] = []
        try:
            expected = list(
                await load_published_expectations(self._uow_factory, self._runtime)
            )
        except CanonicalDataError:
            reasons.add("CURRENT_CANONICAL_INVALID")
        expected_scopes = {
            (item.document_id, item.revision_id): item for item in expected
        }
        actual: dict[tuple[str, str], dict[str, str]] = {}
        duplicates = 0
        parent_searchable = 0
        iterator = getattr(self._index, "iter_all_projection", None)
        if not callable(iterator):
            raise TypeError("index must provide iter_all_projection")
        async with aclosing(iterator(batch_size=1_000)) as rows:
            async for row in rows:
                scope = (row.document_id, row.revision_id)
                payload = _json(row.model_dump(mode="json"))
                bucket = actual.setdefault(scope, {})
                if row.chunk_id in bucket:
                    duplicates += 1
                else:
                    bucket[row.chunk_id] = payload
                if row.chunk_type == ChunkType.TEXT_PARENT.value:
                    parent_searchable += 1
        if duplicates:
            reasons.add("DUPLICATE_RECORDS")
        if parent_searchable:
            reasons.add("TEXT_PARENT_SEARCHABLE")
        orphan_records = 0
        missing_records = 0
        mismatched_records = 0
        verified_records = 0
        for scope, item in expected_scopes.items():
            observed = actual.get(scope)
            if observed is None:
                missing_records += len(item.projection)
                reasons.add("MISSING_RECORDS")
                continue
            if set(observed) != set(item.projection):
                missing_records += len(set(item.projection) - set(observed))
                orphan_records += len(set(observed) - set(item.projection))
                reasons.add("EXACT_SET_MISMATCH")
            for chunk_id, expected_payload in item.projection.items():
                observed_payload = observed.get(chunk_id)
                if observed_payload is None:
                    continue
                if observed_payload != expected_payload:
                    mismatched_records += 1
                    reasons.add("PROJECTION_MISMATCH")
                else:
                    verified_records += 1
        for scope, rows in actual.items():
            if scope not in expected_scopes:
                orphan_records += len(rows)
                reasons.add("ORPHAN_RECORDS")
        if deletion_summary.failed:
            reasons.add("REQUIRED_DELETION_FAILED")
        if build_summary.unsafe_residue:
            reasons.add("CANDIDATE_RESIDUE_UNSAFE")
        safe = not reasons
        return GlobalSafetyResult(
            serving_safety=(
                ServingSafety.SAFE_TO_SERVE if safe else ServingSafety.UNSAFE_TO_SERVE
            ),
            unsafe_reasons=tuple(sorted(reasons)),
            verified_documents=len(expected_scopes),
            verified_records=verified_records,
            orphan_records=orphan_records,
            missing_records=missing_records,
            mismatched_records=mismatched_records,
            required_deletion_failures=len(deletion_summary.failed),
        )
