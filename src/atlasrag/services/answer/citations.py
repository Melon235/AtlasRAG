"""Authoritative citation resolution from reviewed output + AnswerInput."""

from __future__ import annotations

import re

from atlasrag.domain.answer import AnswerInput, GenerationResult
from atlasrag.domain.results import (
    FinalLocalCitation,
    FinalResponse,
    FinalWebCitation,
)
from atlasrag.services.answer.generation_input import local_display_names

_CITATION = re.compile(r"\[([LW])([1-9][0-9]*)\]")


class CitationResolutionError(ValueError):
    """The reviewed citation set cannot be resolved authoritatively."""


class CitationResolver:
    def resolve(
        self, generation: GenerationResult, answer_input: AnswerInput
    ) -> FinalResponse:
        generation = GenerationResult.model_validate(generation)
        answer_input = AnswerInput.model_validate(answer_input)
        display_names = local_display_names(answer_input)
        local = {item.citation_id: item for item in answer_input.local_evidence}
        web = {item.citation_id: item for item in answer_input.web_evidence}
        allowed = set(local) | set(web)
        order: list[str] = []
        for match in _CITATION.finditer(generation.answer_text):
            citation_id = f"{match.group(1)}{match.group(2)}"
            if citation_id not in allowed:
                raise CitationResolutionError
            if citation_id not in order:
                order.append(citation_id)
        if set(order) != set(generation.used_citation_ids):
            raise CitationResolutionError
        citations: list[FinalLocalCitation | FinalWebCitation] = []
        for citation_id in order:
            if citation_id in local:
                item = local[citation_id]
                evidence = item.evidence
                citations.append(
                    FinalLocalCitation(
                        citation_id=citation_id,
                        file_name=display_names[citation_id],
                        relative_source_path=evidence.provenance.relative_source_path,
                        evidence_type=evidence.evidence_type,
                        source_anchor=evidence.provenance.source_anchor,
                    )
                )
            else:
                web_item = web[citation_id]
                web_evidence = web_item.evidence
                citations.append(
                    FinalWebCitation(
                        citation_id=citation_id,
                        title=web_evidence.title,
                        url=web_evidence.url,
                        domain=web_evidence.domain,
                    )
                )
        return FinalResponse(
            answer_text=generation.answer_text, citations=tuple(citations)
        )
