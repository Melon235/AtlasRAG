"""Bounded generation-only view with authoritative citation identity."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated

from pydantic import BeforeValidator, StrictStr

from atlasrag.domain.answer import AnswerInput, WorkingEvidenceBundle
from atlasrag.domain.base import FrozenModel, validate_ordered_collection_input
from atlasrag.domain.enums import LocalAnswerEvidenceStatus
from atlasrag.services.answer.profile import AnswerProfile


class GenerationLocalItem(FrozenModel):
    citation_id: StrictStr
    document_name: StrictStr
    content: StrictStr


class GenerationWebItem(FrozenModel):
    citation_id: StrictStr
    title: StrictStr
    url: StrictStr
    domain: StrictStr
    content: StrictStr


class GenerationInput(FrozenModel):
    original_query: StrictStr
    local_evidence_status: LocalAnswerEvidenceStatus
    local_knowledge: Annotated[
        tuple[GenerationLocalItem, ...],
        BeforeValidator(validate_ordered_collection_input),
    ] = ()
    web_search: Annotated[
        tuple[GenerationWebItem, ...],
        BeforeValidator(validate_ordered_collection_input),
    ] = ()


def local_display_names(answer_input: AnswerInput) -> dict[str, str]:
    """Use basename when unique, canonical relative path when ambiguous."""
    basenames = [
        PurePosixPath(item.evidence.provenance.file_name).name
        for item in answer_input.local_evidence
    ]
    counts = {name: basenames.count(name) for name in basenames}
    names: dict[str, str] = {}
    for item, basename in zip(answer_input.local_evidence, basenames, strict=True):
        display = (
            basename
            if counts[basename] == 1
            else item.evidence.provenance.relative_source_path
        )
        names[item.citation_id] = display
    return names


class GenerationInputBuilder:
    def __init__(self, *, profile: AnswerProfile) -> None:
        self._profile = profile

    def build(
        self,
        answer_input: AnswerInput,
        working_evidence: WorkingEvidenceBundle | None = None,
    ) -> GenerationInput:
        answer_input = AnswerInput.model_validate(answer_input)
        bundle = working_evidence or WorkingEvidenceBundle(
            local_evidence=answer_input.local_evidence,
            web_evidence=answer_input.web_evidence,
        )
        display_names = local_display_names(
            AnswerInput(
                original_query=answer_input.original_query,
                local_evidence_status=answer_input.local_evidence_status,
                local_evidence=bundle.local_evidence,
                web_evidence=bundle.web_evidence,
            )
        )
        remaining = self._profile.max_total_chars
        local: list[GenerationLocalItem] = []
        for item in bundle.local_evidence:
            budget = min(self._profile.max_local_item_chars, remaining)
            if budget <= 0:
                break
            content = item.evidence.content[:budget]
            remaining -= len(content)
            local.append(
                GenerationLocalItem(
                    citation_id=item.citation_id,
                    document_name=display_names[item.citation_id],
                    content=content,
                )
            )
        web: list[GenerationWebItem] = []
        for web_item in bundle.web_evidence:
            budget = min(self._profile.max_web_item_chars, remaining)
            if budget <= 0:
                break
            content = web_item.evidence.content[:budget]
            remaining -= len(content)
            web.append(
                GenerationWebItem(
                    citation_id=web_item.citation_id,
                    title=web_item.evidence.title,
                    url=str(web_item.evidence.url),
                    domain=web_item.evidence.domain,
                    content=content,
                )
            )
        return GenerationInput(
            original_query=answer_input.original_query,
            local_evidence_status=answer_input.local_evidence_status,
            local_knowledge=tuple(local),
            web_search=tuple(web),
        )
