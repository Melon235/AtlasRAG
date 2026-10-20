"""Classify controlled indexing errors and preserve the last-good revision."""

from datetime import datetime, timedelta

from atlasrag.domain.errors import InvariantViolationError
from atlasrag.domain.indexing.failures import (
    EmbeddingInputLimitError,
    EmbeddingOutputError,
    IndexVerificationError,
    RepresentationInputLimitError,
)
from atlasrag.domain.ingestion.source import CandidateBuildContext
from atlasrag.providers.index.projection import IndexWriteRejectedError
from atlasrag.repositories.postgres.records import (
    FailedStage,
    IngestionStatus,
    StoredDocument,
)
from atlasrag.services.indexing.ports import Clock, IndexingUnitOfWorkFactory
from atlasrag.services.ingestion.mapping import load_document_parse_metadata_envelope


def classify_index_failure(error: BaseException) -> FailedStage | None:
    """Admit only positively classified data failures, never message matching.

    Generic provider errors, SDK unavailability, timeout, cancellation, and
    programming/configuration errors are technical failures and must propagate.
    PARSE/CHUNK are owned by the frozen ingestion failure recorder.
    """
    if isinstance(error, RepresentationInputLimitError):
        return FailedStage.REPRESENTATION
    if isinstance(error, (EmbeddingInputLimitError, EmbeddingOutputError)):
        return FailedStage.EMBEDDING
    if isinstance(error, IndexWriteRejectedError):
        return FailedStage.INDEX_WRITE
    if isinstance(error, IndexVerificationError):
        return FailedStage.INDEX_VERIFY
    return None


async def record_index_failure(
    uow_factory: IndexingUnitOfWorkFactory,
    context: CandidateBuildContext,
    error: BaseException,
    *,
    clock: Clock,
) -> StoredDocument:
    """Fail only the matching INDEXING candidate in one short explicit commit.

    The caller holds the whole-build document lease. Provider operations and
    retries are outside this capability. No error contents are persisted;
    unknown errors and storage failures propagate unchanged.
    """
    stage = classify_index_failure(error)
    if stage is None:
        raise error
    canonical = CandidateBuildContext.model_validate(context)
    timestamp = clock()
    if (
        not isinstance(timestamp, datetime)
        or timestamp.tzinfo is None
        or timestamp.utcoffset() != timedelta(0)
    ):
        raise ValueError("clock must return an aware UTC timestamp")

    async with uow_factory() as unit:
        document = await unit.documents.get_by_id(canonical.document_id)
        if document is None:
            raise InvariantViolationError
        document = StoredDocument.model_validate(document)
        source = canonical.source
        if (
            document.ingestion_status is not IngestionStatus.INDEXING
            or document.document_id != canonical.document_id
            or document.relative_source_path != source.relative_source_path
            or document.file_name != source.file_name
            or document.source_type is not source.source_type
            or document.observed_content_hash != canonical.content_hash
            or document.building_revision_id != canonical.revision_id
            or document.building_content_hash != canonical.content_hash
            or document.building_pipeline_fingerprint != canonical.pipeline_fingerprint
            or document.current_revision_id == canonical.revision_id
        ):
            raise InvariantViolationError
        load_document_parse_metadata_envelope(document)
        payload = document.model_dump(mode="python")
        payload.update(
            ingestion_status=IngestionStatus.FAILED,
            failed_stage=stage,
            updated_at=timestamp,
        )
        failed = StoredDocument.model_validate(payload)
        load_document_parse_metadata_envelope(failed)
        await unit.documents.put(failed)
        await unit.commit()
    return failed
