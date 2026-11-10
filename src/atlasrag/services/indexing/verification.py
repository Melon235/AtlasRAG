"""Independent persisted-candidate verification with invocation-owned proof."""

import hashlib
import json
from contextlib import aclosing
from dataclasses import dataclass

from pydantic import ValidationError

from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import ConfigurationError, InvariantViolationError
from atlasrag.domain.indexing.failures import IndexVerificationError
from atlasrag.domain.indexing.models import (
    IndexStructuredMetadataV1,
    RepresentationProfileV1,
)
from atlasrag.domain.ingestion.chunks import RetrievalRole
from atlasrag.domain.ingestion.source import CandidateBuildContext
from atlasrag.providers.embedding.base import EmbeddingProvider
from atlasrag.providers.index.projection import (
    IndexProjectionMalformedError,
    MilvusRevisionProjection,
)
from atlasrag.repositories.postgres.records import IngestionStatus
from atlasrag.services.indexing.candidate import index_candidate
from atlasrag.services.indexing.canonical import (
    CanonicalCandidate,
    load_canonical_candidate,
)
from atlasrag.services.indexing.ports import (
    CandidateIndex,
    Clock,
    IndexingUnitOfWork,
    IndexingUnitOfWorkFactory,
)
from atlasrag.services.indexing.records import build_expected_projection
from atlasrag.services.indexing.representation import RepresentationBuilder


def _json(value: object) -> str:
    # JSON retains bool/int/string distinctions lost by Python dict equality.
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _expected(candidate: CanonicalCandidate, runtime: RuntimeConfig) -> dict[str, str]:
    builder = RepresentationBuilder(
        RepresentationProfileV1(),
        representation_version=runtime.part_i.representation_version,
    )
    return {
        chunk.chunk_id: _json(
            build_expected_projection(
                candidate.document, chunk, builder.build(chunk)
            ).model_dump(mode="json")
        )
        for chunk in candidate.chunks
        if chunk.retrieval_role is RetrievalRole.RETRIEVABLE
    }


@dataclass(frozen=True)
class _Binding:
    canonical_digest: str
    projection_digest: str


def _binding(candidate: CanonicalCandidate, expected: dict[str, str]) -> _Binding:
    return _Binding(
        candidate.canonical_digest,
        hashlib.sha256(_json(expected).encode("utf-8")).hexdigest(),
    )


class CandidateVerificationAttempt:
    """One runner invocation's private index/verify/publication capability.

    The caller holds the whole-build document lease. Construct a new instance
    for every invocation; never store it, proof, or dependencies in Graph State.
    Provider work occurs outside PostgreSQL transactions. The publisher calls
    consume_verified inside its existing transaction, before publication writes,
    and commits that transaction itself. A failed publication needs reverify.

    Overlapping calls fail immediately and invalidate the active generation;
    no queued waiters, process registry, caller digests, or verified flags exist.
    """

    def __init__(
        self,
        uow_factory: IndexingUnitOfWorkFactory,
        context: CandidateBuildContext,
        *,
        config: RuntimeConfig,
        index: CandidateIndex,
    ) -> None:
        self.__factory = uow_factory
        self.__context = CandidateBuildContext.model_validate(context)
        self.__runtime = RuntimeConfig.model_validate(config)
        self.__index = index
        self.__indexed: _Binding | None = None
        self.__receipt: _Binding | None = None
        self.__generation = 0
        self.__busy = False

    def _begin(self) -> int:
        self.__receipt = None
        self.__generation += 1
        if self.__busy:
            # The overlapping caller must not release the owner's busy flag.
            raise InvariantViolationError
        self.__busy = True
        return self.__generation

    def invalidate_verified(self) -> None:
        """Discard proof, including a pending verifier's ability to rearm it.

        Publication cleanup uses this even if opening its transaction fails.
        It cannot create proof or release another operation's busy guard.
        """
        self.__receipt = None
        self.__generation += 1

    def _check_generation(self, generation: int) -> None:
        if generation != self.__generation:
            raise InvariantViolationError

    async def _load(self, unit: IndexingUnitOfWork) -> CanonicalCandidate:
        candidate = await load_canonical_candidate(
            unit,
            self.__context,
            expected_chunker_behavior_version=self.__runtime.part_i.chunker_behavior_version,
        )
        if candidate.stored_document.ingestion_status is not IngestionStatus.INDEXING:
            raise InvariantViolationError
        return candidate

    async def _snapshot(self) -> CanonicalCandidate:
        async with self.__factory() as unit:
            return await self._load(unit)

    def _check_binding(self, candidate: CanonicalCandidate, binding: _Binding) -> None:
        if candidate.canonical_digest != binding.canonical_digest:
            raise IndexVerificationError("canonical_changed")
        if _binding(candidate, _expected(candidate, self.__runtime)) != binding:
            raise IndexVerificationError("expected_projection_changed")

    async def index(
        self,
        *,
        embedding: EmbeddingProvider,
        clock: Clock,
        batch_size: int = 1,
    ) -> None:
        """Bind proof eligibility to the exact snapshot T4 actually indexed."""
        self.__indexed = None
        generation = self._begin()
        try:
            candidate = await index_candidate(
                self.__factory,
                self.__context,
                config=self.__runtime,
                embedding=embedding,
                index=self.__index,
                clock=clock,
                batch_size=batch_size,
            )
            binding = _binding(candidate, _expected(candidate, self.__runtime))
            self._check_binding(await self._snapshot(), binding)
            self._check_generation(generation)
            self.__indexed = binding
        finally:
            self.__busy = False

    async def verify(self, *, batch_size: int = 1_000) -> None:
        """Read complete persisted authority and every actual Strong projection row."""
        generation = self._begin()
        try:
            if type(batch_size) is not int or not 1 <= batch_size <= 16_384:
                raise ConfigurationError
            binding = self.__indexed
            if binding is None:
                raise InvariantViolationError
            candidate = await self._snapshot()
            self._check_binding(candidate, binding)
            expected = _expected(candidate, self.__runtime)
            seen: set[str] = set()
            async with aclosing(
                self.__index.iter_revision_projection(
                    self.__context.document_id,
                    self.__context.revision_id,
                    batch_size=batch_size,
                )
            ) as rows:
                while True:
                    # Convert only positively classified successful-read data
                    # corruption. aclosing cleanup and SDK errors remain technical.
                    try:
                        observed = await anext(rows)
                    except StopAsyncIteration:
                        break
                    except IndexProjectionMalformedError:
                        raise IndexVerificationError("malformed_projection") from None
                    try:
                        observed = MilvusRevisionProjection.model_validate(observed)
                        payload = observed.model_dump(mode="json")
                        IndexStructuredMetadataV1.model_validate(
                            payload["structured_metadata"]
                        )
                    except ValidationError:
                        raise IndexVerificationError(
                            "invalid_projection_schema"
                        ) from None
                    if observed.chunk_id in seen:
                        raise IndexVerificationError(
                            "duplicate_id", chunk_id=observed.chunk_id
                        )
                    if observed.chunk_id not in expected:
                        raise IndexVerificationError(
                            "unexpected_id", chunk_id=observed.chunk_id
                        )
                    if _json(payload) != expected[observed.chunk_id]:
                        raise IndexVerificationError(
                            "projection_mismatch", chunk_id=observed.chunk_id
                        )
                    seen.add(observed.chunk_id)
            if seen != expected.keys():
                raise IndexVerificationError("missing_ids")
            self._check_binding(await self._snapshot(), binding)
            self._check_generation(generation)
            self.__receipt = binding
        finally:
            self.__busy = False

    async def consume_verified(
        self, unit: IndexingUnitOfWork, context: CandidateBuildContext
    ) -> CanonicalCandidate:
        """Consume proof once and recheck PG in the publisher's existing transaction.

        No provider calls, writes, or commits occur here. Even a failed consume
        invalidates the receipt; callers cannot reuse proof across transactions.
        """
        receipt = self.__receipt
        generation = self._begin()
        try:
            if (
                receipt is None
                or CandidateBuildContext.model_validate(context) != self.__context
            ):
                raise InvariantViolationError
            candidate = await self._load(unit)
            self._check_binding(candidate, receipt)
            self._check_generation(generation)
            return candidate
        finally:
            self.__busy = False
