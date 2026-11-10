"""Index only durable canonical candidates using bounded external-work batches."""

import re
from contextlib import aclosing
from datetime import timedelta

from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import ConfigurationError, InvariantViolationError
from atlasrag.domain.indexing.models import (
    EmbeddingModelIdentity,
    RepresentationProfileV1,
)
from atlasrag.domain.ingestion.chunks import ChunkRecord, RetrievalRole
from atlasrag.domain.ingestion.source import CandidateBuildContext
from atlasrag.providers.embedding.base import (
    EmbeddingProvider,
    validate_embedding_output,
)
from atlasrag.repositories.postgres.records import IngestionStatus, StoredDocument
from atlasrag.services.indexing.canonical import (
    CanonicalCandidate,
    load_canonical_candidate,
)
from atlasrag.services.indexing.ports import (
    CandidateIndex,
    Clock,
    IndexingUnitOfWorkFactory,
)
from atlasrag.services.indexing.records import (
    build_retrieval_record,
    map_retrieval_record_to_milvus,
)
from atlasrag.services.indexing.representation import RepresentationBuilder


def _validate_runtime(
    config: RuntimeConfig,
    context: CandidateBuildContext,
    embedding: EmbeddingProvider,
    index: CandidateIndex,
) -> RepresentationBuilder:
    identity = EmbeddingModelIdentity.model_validate(embedding.identity)
    models = config.models
    device_matches = identity.device == models.model_device
    if models.model_device == "cuda":
        device_matches = re.fullmatch(r"cuda:[0-9]+", identity.device) is not None
    if (
        pipeline_fingerprint(config) != context.pipeline_fingerprint
        or identity.model_id != models.embedding_model_id
        or identity.model_revision != models.embedding_model_revision
        or identity.tokenizer_id != models.embedding_tokenizer_id
        or identity.tokenizer_revision != models.embedding_tokenizer_revision
        or identity.dimension != config.part_i.embedding_dimension
        or type(index.embedding_dimension) is not int
        or index.embedding_dimension != identity.dimension
        or index.analyzer != config.part_i.bm25_analyzer
        or not device_matches
        or config.part_i.representation_version
        != RepresentationProfileV1().representation_version
    ):
        raise ConfigurationError
    return RepresentationBuilder(
        RepresentationProfileV1(),
        representation_version=config.part_i.representation_version,
    )


async def _write_batch(
    candidate: CanonicalCandidate,
    chunks: tuple[ChunkRecord, ...],
    *,
    builder: RepresentationBuilder,
    embedding: EmbeddingProvider,
    index: CandidateIndex,
    dimension: int,
) -> None:
    # This frame owns all vectors/records and ends before the next embed call.
    representations = tuple(builder.build(chunk) for chunk in chunks)
    raw_vectors = await embedding.embed(
        tuple(item.dense_text for item in representations)
    )
    vectors = validate_embedding_output(
        raw_vectors, expected_count=len(chunks), dimension=dimension
    )
    records = tuple(
        map_retrieval_record_to_milvus(
            build_retrieval_record(candidate.document, chunk, representation, vector)
        )
        for chunk, representation, vector in zip(
            chunks, representations, vectors, strict=True
        )
    )
    await index.insert_revision_batch(
        records,
        document_id=candidate.context.document_id,
        revision_id=candidate.context.revision_id,
    )


async def index_candidate(
    uow_factory: IndexingUnitOfWorkFactory,
    context: CandidateBuildContext,
    *,
    config: RuntimeConfig,
    embedding: EmbeddingProvider,
    index: CandidateIndex,
    clock: Clock,
    batch_size: int = 1,
) -> CanonicalCandidate:
    """Rebuild R2 only; leave INDEXING on technical or classified failures.

    Callers must hold the document lease for the entire attempt. Successful
    indexing is only a visibility boundary; independent verification/publish
    remain separate capabilities. No caller-provided chunks are accepted. Return
    the exact canonical snapshot used so an invocation-owned verifier can bind
    its proof to indexing authority without trusting caller-supplied digests.
    """
    canonical_context = CandidateBuildContext.model_validate(context)
    runtime = RuntimeConfig.model_validate(config)
    if type(batch_size) is not int or not 1 <= batch_size <= 16_384:
        raise ConfigurationError
    builder = _validate_runtime(runtime, canonical_context, embedding, index)
    timestamp = clock()
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise ValueError("clock must return an aware UTC timestamp")
    async with uow_factory() as unit:
        candidate = await load_canonical_candidate(
            unit,
            canonical_context,
            expected_chunker_behavior_version=runtime.part_i.chunker_behavior_version,
        )
        if candidate.stored_document.ingestion_status is IngestionStatus.CHUNKED:
            payload = candidate.stored_document.model_dump(mode="python")
            payload.update(
                ingestion_status=IngestionStatus.INDEXING, updated_at=timestamp
            )
            await unit.documents.put(StoredDocument.model_validate(payload))
        await unit.commit()

    # initialize inspects actual schema/indexes, not only the configured dimension.
    await index.initialize()
    document_id, revision_id = (
        canonical_context.document_id,
        canonical_context.revision_id,
    )
    await index.delete_revision(document_id, revision_id)
    await index.wait_revision_visible(document_id, revision_id)
    async with aclosing(
        index.iter_revision_projection(document_id, revision_id, batch_size=1)
    ) as rows:
        async for _ in rows:
            raise InvariantViolationError

    retrievable = tuple(
        chunk
        for chunk in candidate.chunks
        if chunk.retrieval_role is RetrievalRole.RETRIEVABLE
    )
    for start in range(0, len(retrievable), batch_size):
        await _write_batch(
            candidate,
            retrievable[start : start + batch_size],
            builder=builder,
            embedding=embedding,
            index=index,
            dimension=runtime.part_i.embedding_dimension,
        )
    await index.wait_revision_visible(document_id, revision_id)
    return candidate
