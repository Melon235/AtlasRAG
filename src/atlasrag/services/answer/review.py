"""Deterministic protocol/source-output review, not a factual judge."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from atlasrag.domain.answer import (
    AnswerInput,
    GenerationResult,
    OutputReviewResult,
)
from atlasrag.domain.enums import OutputReviewDecision, OutputReviewReasonCode
from atlasrag.providers.generation.base import PROMPT_CONTROL_SENTINEL

_CITATION = re.compile(r"\[([LW])([1-9][0-9]*)\]")
_URL = re.compile(r"https?://[^\s<>\"'`\[\]{}|\\^]+", re.IGNORECASE)
_FENCED = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_INTERNAL_ID = re.compile(r"\b(?:doc|rev|chunk|elem)_[0-9a-f]{64}\b")


def _ids_in_text(text: str) -> list[str]:
    return [f"{match.group(1)}{match.group(2)}" for match in _CITATION.finditer(text)]


def _normalise_url(url: str) -> tuple[str, str, int | None, str, str]:
    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None:
        return ("invalid", "", -1, "", "")
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold()
    try:
        port = parsed.port
    except ValueError:
        return ("invalid", "", -1, "", "")
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None
    path = (parsed.path or "/").rstrip("/") or "/"
    query = parsed.query
    return scheme, host, port, path, query


class OutputReviewService:
    async def review(
        self, generation: GenerationResult, answer_input: AnswerInput
    ) -> OutputReviewResult:
        generation = GenerationResult.model_validate(generation)
        answer_input = AnswerInput.model_validate(answer_input)
        known = {item.citation_id for item in answer_input.local_evidence} | {
            item.citation_id for item in answer_input.web_evidence
        }
        markers = _ids_in_text(generation.answer_text)
        declared = set(generation.used_citation_ids)
        marker_set = set(markers)
        reasons: list[OutputReviewReasonCode] = []
        block = False

        if PROMPT_CONTROL_SENTINEL in generation.answer_text:
            reasons.append(OutputReviewReasonCode.CONTROL_LEAKAGE)
            block = True
        if any(
            marker in generation.answer_text for marker in ("<|tool_call|>", "<|tool|>")
        ):
            reasons.append(OutputReviewReasonCode.PROTOCOL_CONTENT)
            block = True
        unknown = (declared | marker_set) - known
        if unknown:
            reasons.append(OutputReviewReasonCode.UNKNOWN_CITATION)
            block = True
        if marker_set != declared:
            reasons.append(OutputReviewReasonCode.CITATION_SET_MISMATCH)

        prose = _INLINE_CODE.sub("", _FENCED.sub("", generation.answer_text))
        if _INTERNAL_ID.search(prose):
            reasons.append(OutputReviewReasonCode.SOURCE_BOUNDARY_VIOLATION)
            block = True
        allowed_urls = {
            _normalise_url(str(item.evidence.url)) for item in answer_input.web_evidence
        }
        for match in _URL.finditer(prose):
            url = match.group(0).rstrip(".,;:!?)]}")
            if _normalise_url(url) not in allowed_urls:
                reasons.append(OutputReviewReasonCode.FORGED_SOURCE_REFERENCE)
                block = True
                break

        reasons = list(dict.fromkeys(reasons))
        if block:
            return OutputReviewResult(
                decision=OutputReviewDecision.BLOCK, reason_codes=tuple(reasons)
            )
        if reasons:
            return OutputReviewResult(
                decision=OutputReviewDecision.REGENERATE,
                reason_codes=tuple(reasons),
            )
        return OutputReviewResult(decision=OutputReviewDecision.ACCEPT)
