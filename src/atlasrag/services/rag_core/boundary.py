"""Deterministic Evidence -> AnswerInput boundary."""

from __future__ import annotations

from atlasrag.domain.answer import (
    AnswerInput,
    AnswerLocalEvidence,
    AnswerWebEvidence,
)
from atlasrag.domain.enums import LocalAnswerEvidenceStatus, LocalEvidenceStatus
from atlasrag.domain.evidence import LocalEvidence, LocalEvidenceResult
from atlasrag.domain.web import WebEvidenceResult


class AnswerInputBuilder:
    def build(
        self,
        *,
        original_query: str,
        local_result: LocalEvidenceResult,
        web_result: WebEvidenceResult | None,
    ) -> AnswerInput:
        local_result = LocalEvidenceResult.model_validate(local_result)
        status = LocalAnswerEvidenceStatus.NONE
        selected: tuple[LocalEvidence, ...] = ()
        if local_result.evidence_status is LocalEvidenceStatus.SUFFICIENT:
            status = LocalAnswerEvidenceStatus.SUFFICIENT
            selected = local_result.selected_evidence
        elif (
            local_result.evidence_status is LocalEvidenceStatus.INSUFFICIENT
            and local_result.best_available_evidence is not None
        ):
            status = LocalAnswerEvidenceStatus.INSUFFICIENT_BEST_MATCH
            selected = (local_result.best_available_evidence,)
        local_evidence: tuple[AnswerLocalEvidence, ...] = tuple(
            AnswerLocalEvidence(citation_id=f"L{index}", evidence=evidence)
            for index, evidence in enumerate(selected, start=1)
        )
        web_evidence: tuple[AnswerWebEvidence, ...] = ()
        if web_result is not None:
            web_result = WebEvidenceResult.model_validate(web_result)
            web_evidence = tuple(
                AnswerWebEvidence(citation_id=f"W{index}", evidence=evidence)
                for index, evidence in enumerate(web_result.evidence, start=1)
            )
        return AnswerInput(
            original_query=original_query,
            local_evidence_status=status,
            local_evidence=local_evidence,
            web_evidence=web_evidence,
        )
