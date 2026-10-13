"""Thin orchestration for parse work outside candidate transactions."""

from __future__ import annotations

from typing import Protocol

from atlasrag.domain.errors import SourceMutationError
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.source import CandidateBuildContext, SourceFile
from atlasrag.services.ingestion.persistence import (
    CandidateUnitOfWork,
    Clock,
    UnitOfWorkFactory,
    discard_candidate,
    mark_parsing,
    persist_parsed,
)
from atlasrag.services.ingestion.source_guard import ensure_source_unchanged


class ParserCallback(Protocol):
    """One injected parser entrypoint; provider selection stays outside here."""

    async def __call__(
        self,
        source: SourceFile,
        context: CandidateBuildContext,
    ) -> DocumentContainer: ...


async def parse_candidate(
    uow_factory: UnitOfWorkFactory[CandidateUnitOfWork],
    context: CandidateBuildContext,
    parser: ParserCallback,
    *,
    clock: Clock,
) -> DocumentContainer | None:
    """Parse, rehash, then persist; mutation discards and ends this attempt."""
    canonical = CandidateBuildContext.model_validate(context)
    await mark_parsing(uow_factory, canonical, clock=clock)
    document = await parser(canonical.source, canonical)
    try:
        ensure_source_unchanged(canonical.source, canonical.content_hash)
    except SourceMutationError:
        await discard_candidate(uow_factory, canonical, clock=clock)
        return None
    await persist_parsed(
        uow_factory,
        canonical,
        document,
        clock=clock,
    )
    return document
