"""Publish one independently verified revision and its cache marker atomically."""

from datetime import datetime, timedelta

from atlasrag.domain.errors import InvariantViolationError
from atlasrag.domain.indexing.models import PublishResult
from atlasrag.domain.ingestion.source import CandidateBuildContext
from atlasrag.repositories.postgres.records import (
    IngestionStatus,
    RuntimeMetadata,
    StoredDocument,
)
from atlasrag.services.indexing.ports import Clock, PublicationUnitOfWorkFactory
from atlasrag.services.indexing.verification import CandidateVerificationAttempt
from atlasrag.services.ingestion.mapping import (
    DocumentParseMetadataEnvelopeV1,
    load_document_parse_metadata_envelope,
    serialize_document_parse_metadata_envelope,
)


async def publish_revision(
    uow_factory: PublicationUnitOfWorkFactory,
    context: CandidateBuildContext,
    *,
    verification: CandidateVerificationAttempt,
    clock: Clock,
) -> PublishResult:
    """Revalidate proof and promote the building triple/slot in one transaction.

    The caller holds the whole-build document lease. Indexing and independent
    verification precede this operation; no provider calls or old-revision
    cleanup belong here. Any failed attempt requires fresh verification,
    including errors before transaction entry. Commit errors propagate with
    their original semantics; this capability does not retry publication.
    """
    try:
        async with uow_factory() as unit:
            candidate = await verification.consume_verified(unit, context)
            stored = candidate.stored_document
            envelope = load_document_parse_metadata_envelope(stored)
            timestamp = clock()
            if (
                not isinstance(timestamp, datetime)
                or timestamp.tzinfo is None
                or timestamp.utcoffset() != timedelta(0)
            ):
                raise ValueError("clock must return an aware UTC timestamp")
            promoted = DocumentParseMetadataEnvelopeV1(
                schema_version=1, current=envelope.building, building=None
            )
            payload = stored.model_dump(mode="python")
            payload.update(
                current_revision_id=stored.building_revision_id,
                current_content_hash=stored.building_content_hash,
                current_pipeline_fingerprint=stored.building_pipeline_fingerprint,
                building_revision_id=None,
                building_content_hash=None,
                building_pipeline_fingerprint=None,
                ingestion_status=IngestionStatus.READY,
                failed_stage=None,
                parse_metadata=serialize_document_parse_metadata_envelope(promoted),
                updated_at=timestamp,
            )
            published = StoredDocument.model_validate(payload)
            load_document_parse_metadata_envelope(published)
            result = PublishResult(
                document_id=candidate.context.document_id,
                published_revision_id=candidate.context.revision_id,
                previous_current_revision_id=stored.current_revision_id,
            )
            await unit.documents.put(published)
            metadata = RuntimeMetadata.model_validate(
                await unit.runtime_metadata.set_query_cache_invalidation_required(True)
            )
            if not metadata.query_cache_invalidation_required:
                raise InvariantViolationError
            await unit.commit()
        return result
    finally:
        verification.invalidate_verified()
