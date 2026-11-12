"""Shared frozen-contract fixtures for Stage 7 tests."""

from __future__ import annotations

from typing import Any

from pydantic import HttpUrl

from atlasrag.domain.answer import (
    AnswerInput,
    AnswerLocalEvidence,
    AnswerWebEvidence,
    GenerationResult,
)
from atlasrag.domain.enums import (
    EvidenceType,
    LocalAnswerEvidenceStatus,
    WebEvidenceRepresentation,
)
from atlasrag.domain.evidence import LocalEvidence, LocalProvenance
from atlasrag.domain.web import WebEvidence
from atlasrag.services.answer.citations import CitationResolver
from atlasrag.services.answer.compression import IdentityCompression
from atlasrag.services.answer.generation_input import GenerationInputBuilder
from atlasrag.services.answer.profile import AnswerProfile
from atlasrag.services.answer.review import OutputReviewService


def local_evidence(
    *,
    context: str = "chunk_" + "a" * 64,
    content: str = "Canonical local evidence content.",
    file_name: str = "guide.md",
    relative_path: str = "docs/guide.md",
) -> LocalEvidence:
    return LocalEvidence(
        evidence_type=EvidenceType.TEXT_PARENT,
        context_id=context,
        document_id="doc_" + "b" * 64,
        revision_id="rev_" + "c" * 64,
        content=content,
        provenance=LocalProvenance(
            file_name=file_name,
            relative_source_path=relative_path,
        ),
    )


def web_evidence(
    *,
    url: str = "https://example.com/page",
    title: str = "Example page",
    content: str = "External evidence content.",
) -> WebEvidence:
    host = url.split("://", 1)[1].split("/", 1)[0]
    return WebEvidence(
        title=title,
        url=HttpUrl(url),
        domain=host,
        search_rank=1,
        content=content,
        representation=WebEvidenceRepresentation.SANITIZED_CONTENT,
    )


def answer_input(
    *,
    local: tuple[LocalEvidence, ...] | None = None,
    web: tuple[WebEvidence, ...] | None = None,
    status: LocalAnswerEvidenceStatus = LocalAnswerEvidenceStatus.SUFFICIENT,
) -> AnswerInput:
    local_values = (local_evidence(),) if local is None else local
    web_values = () if web is None else web
    return AnswerInput(
        original_query="original question",
        local_evidence_status=status,
        local_evidence=tuple(
            AnswerLocalEvidence(citation_id=f"L{index}", evidence=evidence)
            for index, evidence in enumerate(local_values, start=1)
        ),
        web_evidence=tuple(
            AnswerWebEvidence(citation_id=f"W{index}", evidence=evidence)
            for index, evidence in enumerate(web_values, start=1)
        ),
    )


def generation(text: str, ids: tuple[str, ...] = ()) -> GenerationResult:
    return GenerationResult(answer_text=text, used_citation_ids=ids)


def services(generation_provider: Any, *, profile: AnswerProfile | None = None) -> Any:
    from atlasrag.graphs.answer.subgraph import AnswerServices

    profile = profile or AnswerProfile()
    return AnswerServices(
        compression=IdentityCompression(),
        builder=GenerationInputBuilder(profile=profile),
        generation=generation_provider,
        review=OutputReviewService(),
        resolver=CitationResolver(),
        profile=profile,
    )
