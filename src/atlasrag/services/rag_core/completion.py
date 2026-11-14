"""RagCore completion-status assessment from frozen results."""

from __future__ import annotations

from atlasrag.domain.answer import AnswerInput
from atlasrag.domain.enums import (
    AnswerExecutionOutcome,
    LocalExecutionStatus,
    RagCompletionStatus,
    WebExecutionStatus,
)
from atlasrag.domain.evidence import LocalEvidenceResult
from atlasrag.domain.web import WebEvidenceResult


class CompletionAssessor:
    def assess(
        self,
        *,
        answer_outcome: AnswerExecutionOutcome,
        answer_input: AnswerInput,
        local_result: LocalEvidenceResult | None,
        web_result: WebEvidenceResult | None,
    ) -> RagCompletionStatus:
        if answer_outcome is AnswerExecutionOutcome.SAFE_FAILURE:
            return RagCompletionStatus.SAFE_FAILURE
        if not answer_input.local_evidence and not answer_input.web_evidence:
            return RagCompletionStatus.SAFE_FAILURE
        degraded = False
        if local_result is not None and local_result.execution_status is (
            LocalExecutionStatus.DEGRADED
        ):
            degraded = True
        if web_result is not None and web_result.execution_status is (
            WebExecutionStatus.DEGRADED
        ):
            degraded = True
        return (
            RagCompletionStatus.DEGRADED_SUCCESS
            if degraded
            else RagCompletionStatus.SUCCESS
        )
