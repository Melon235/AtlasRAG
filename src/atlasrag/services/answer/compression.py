"""Optional compression boundary; identity baseline preserves all identity."""

from __future__ import annotations

from typing import Protocol

from atlasrag.domain.answer import AnswerInput, WorkingEvidenceBundle


class CompressionError(ValueError):
    """Optional compression could not produce a usable working bundle."""


class CompressionStrategy(Protocol):
    async def prepare(self, answer_input: AnswerInput) -> WorkingEvidenceBundle: ...


class IdentityCompression:
    async def prepare(self, answer_input: AnswerInput) -> WorkingEvidenceBundle:
        return WorkingEvidenceBundle(
            local_evidence=answer_input.local_evidence,
            web_evidence=answer_input.web_evidence,
        )
