"""Answer review, regeneration, citation, compression, and isolation tests."""

from __future__ import annotations

import pytest

from atlasrag.application.answer import AnswerRunner
from atlasrag.domain.answer import GenerationResult
from atlasrag.domain.enums import (
    AnswerExecutionOutcome,
    LocalAnswerEvidenceStatus,
    OutputReviewDecision,
)
from atlasrag.domain.results import FinalLocalCitation
from atlasrag.providers.generation.base import GenerationProtocolError
from atlasrag.services.answer.citations import CitationResolutionError, CitationResolver
from atlasrag.services.answer.compression import CompressionError
from atlasrag.services.answer.review import OutputReviewService
from stage7.helpers import (
    answer_input,
    generation,
    local_evidence,
    services,
    web_evidence,
)


class FakeGeneration:
    def __init__(self, *items: object) -> None:
        self.items = list(items)
        self.inputs: list[object] = []

    async def generate(self, input: object) -> GenerationResult:
        self.inputs.append(input)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, GenerationResult)
        return item


@pytest.mark.asyncio
async def test_mixed_answer_accepts_and_resolves_in_first_appearance_order() -> None:
    provider = FakeGeneration(generation("Local [L1], web [W1].", ("L1", "W1")))
    response, outcome = await AnswerRunner(services(provider)).run(
        answer_input(web=(web_evidence(),))
    )
    assert outcome is AnswerExecutionOutcome.SUCCESS
    assert [item.citation_id for item in response.citations] == ["L1", "W1"]
    assert response.citations[0].source_kind.value == "LOCAL"
    assert response.citations[1].source_kind.value == "WEB"


@pytest.mark.asyncio
async def test_unknown_citation_blocks_without_fabricated_citations() -> None:
    provider = FakeGeneration(generation("Bad [W9].", ("W9",)))
    response, outcome = await AnswerRunner(services(provider)).run(answer_input())
    assert outcome is AnswerExecutionOutcome.SAFE_FAILURE
    assert response.citations == ()
    assert "verified answer" in response.answer_text


@pytest.mark.asyncio
async def test_citation_mismatch_regenerates_once_then_succeeds() -> None:
    provider = FakeGeneration(
        generation("First [L1].", ()),
        generation("Second [L1].", ("L1",)),
    )
    response, outcome = await AnswerRunner(services(provider)).run(answer_input())
    assert outcome is AnswerExecutionOutcome.SUCCESS
    assert len(provider.inputs) == 2
    assert response.citations[0].citation_id == "L1"


@pytest.mark.asyncio
async def test_protocol_error_consumes_bounded_regeneration_then_safe_failure() -> None:
    provider = FakeGeneration(
        GenerationProtocolError("bad json"),
        GenerationProtocolError("still bad"),
    )
    response, outcome = await AnswerRunner(services(provider)).run(answer_input())
    assert outcome is AnswerExecutionOutcome.SAFE_FAILURE
    assert len(provider.inputs) == 2
    assert response.citations == ()


@pytest.mark.asyncio
async def test_control_leakage_and_forged_url_block_but_code_url_is_allowed() -> None:
    review = OutputReviewService()
    source = answer_input(web=(web_evidence(url="https://a.example/authorized"),))
    blocked = await review.review(
        generation("Boundary ATLASRAG_INTERNAL_CONTROL_BOUNDARY [W1].", ("W1",)),
        source,
    )
    assert blocked.decision is OutputReviewDecision.BLOCK
    forged = await review.review(
        generation("See https://evil.example/x [W1].", ("W1",)), source
    )
    assert forged.decision is OutputReviewDecision.BLOCK
    code = await review.review(
        generation("Example: `https://evil.example/x` [W1].", ("W1",)), source
    )
    assert code.decision is OutputReviewDecision.ACCEPT
    uppercase = await review.review(
        generation("See https://A.EXAMPLE/authorized [W1].", ("W1",)), source
    )
    assert uppercase.decision is OutputReviewDecision.ACCEPT
    default_port = await review.review(
        generation("See https://a.example:443/authorized [W1].", ("W1",)), source
    )
    assert default_port.decision is OutputReviewDecision.ACCEPT
    userinfo = await review.review(
        generation("See https://user@a.example/authorized [W1].", ("W1",)), source
    )
    assert userinfo.decision is OutputReviewDecision.BLOCK
    boundary = await review.review(
        generation("Internal doc_" + "a" * 64 + " [L1].", ("L1",)), answer_input()
    )
    assert boundary.decision is OutputReviewDecision.BLOCK
    assert any(
        code.value == "SOURCE_BOUNDARY_VIOLATION" for code in boundary.reason_codes
    )


@pytest.mark.asyncio
async def test_prompt_injection_discussion_is_not_crude_blocked() -> None:
    provider = FakeGeneration(
        generation(
            "Discussion of 'ignore previous instructions' is legitimate [L1].", ("L1",)
        )
    )
    _, outcome = await AnswerRunner(services(provider)).run(answer_input())
    assert outcome is AnswerExecutionOutcome.SUCCESS


@pytest.mark.asyncio
async def test_generation_programming_error_propagates() -> None:
    provider = FakeGeneration(RuntimeError("programming bug"))
    with pytest.raises(RuntimeError):
        await AnswerRunner(services(provider)).run(answer_input())


@pytest.mark.asyncio
async def test_web_only_answer_input_is_supported() -> None:
    provider = FakeGeneration(generation("Web [W1].", ("W1",)))
    source = answer_input(
        local=(), status=LocalAnswerEvidenceStatus.NONE, web=(web_evidence(),)
    )
    response, outcome = await AnswerRunner(services(provider)).run(source)
    assert outcome is AnswerExecutionOutcome.SUCCESS
    assert response.citations[0].citation_id == "W1"


@pytest.mark.asyncio
async def test_whitespace_local_content_is_admitted_not_rejected() -> None:
    provider = FakeGeneration(generation("Insufficient [L1].", ("L1",)))
    runner = AnswerRunner(services(provider))
    _, outcome = await runner.run(answer_input(local=(local_evidence(content="   "),)))
    assert outcome is AnswerExecutionOutcome.SUCCESS
    generated = provider.inputs[0]
    assert generated.local_knowledge[0].content == "   "  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_compression_failure_falls_back_to_original_evidence() -> None:
    class FailCompression:
        async def prepare(self, answer_input: object) -> None:
            raise CompressionError

    provider = FakeGeneration(generation("Answer [L1].", ("L1",)))
    bundle = services(provider)
    bundle = type(bundle)(
        compression=FailCompression(),
        builder=bundle.builder,
        generation=bundle.generation,
        review=bundle.review,
        resolver=bundle.resolver,
        profile=bundle.profile,
    )
    response, outcome = await AnswerRunner(bundle).run(answer_input())
    assert outcome is AnswerExecutionOutcome.SUCCESS
    assert response.citations[0].citation_id == "L1"


def test_duplicate_basenames_use_relative_display_and_exclude_absolute_paths() -> None:
    first = local_evidence(file_name="manual.pdf", relative_path="productA/manual.pdf")
    second = local_evidence(file_name="manual.pdf", relative_path="productB/manual.pdf")
    source = answer_input(local=(first, second))
    resolver = CitationResolver()
    response = resolver.resolve(generation("A [L1] B [L2].", ("L1", "L2")), source)
    local_citations = [
        item for item in response.citations if isinstance(item, FinalLocalCitation)
    ]
    assert [item.file_name for item in local_citations] == [
        "productA/manual.pdf",
        "productB/manual.pdf",
    ]
    assert all(not item.file_name.startswith("/") for item in local_citations)


def test_citation_resolution_rejects_unknown_mapping() -> None:
    with pytest.raises(CitationResolutionError):
        CitationResolver().resolve(generation("Bad [W9].", ("W9",)), answer_input())


@pytest.mark.asyncio
async def test_insufficient_best_match_is_preserved() -> None:
    provider = FakeGeneration(generation("Closest [L1].", ("L1",)))
    source = answer_input(
        local=(local_evidence(),),
        status=LocalAnswerEvidenceStatus.INSUFFICIENT_BEST_MATCH,
    )
    response, outcome = await AnswerRunner(services(provider)).run(source)
    assert outcome is AnswerExecutionOutcome.SUCCESS
    assert response.citations[0].citation_id == "L1"


@pytest.mark.asyncio
async def test_protocol_error_on_regeneration_does_not_reuse_stale_result() -> None:
    provider = FakeGeneration(
        generation("First [L1].", ()),
        GenerationProtocolError("second malformed"),
    )
    response, outcome = await AnswerRunner(services(provider)).run(answer_input())
    assert outcome is AnswerExecutionOutcome.SAFE_FAILURE
    assert response.citations == ()
