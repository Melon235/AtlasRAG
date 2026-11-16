"""RagCore graph orchestration tests with injected business fakes."""

from __future__ import annotations

from typing import Any, cast

import pytest
from stage7.helpers import local_evidence, web_evidence

from atlasrag.application.rag_core import RagCoreRunner
from atlasrag.domain.answer import AnswerInput
from atlasrag.domain.enums import (
    AnswerExecutionOutcome,
    LocalEvidenceStatus,
    LocalExecutionStatus,
    RagCompletionStatus,
    WebExecutionStatus,
)
from atlasrag.domain.evidence import LocalEvidenceResult
from atlasrag.domain.requests import (
    LocalRetrievalRequest,
    QueryFilters,
    UserTurnRequest,
)
from atlasrag.domain.results import FinalResponse
from atlasrag.domain.web import WebEvidenceResult
from atlasrag.graphs.rag_core.subgraph import RagCoreServices
from atlasrag.services.rag_core.boundary import AnswerInputBuilder
from atlasrag.services.rag_core.completion import CompletionAssessor
from atlasrag.services.rag_core.query import QueryConstructionResult
from atlasrag.services.rag_core.routing import CorrectiveRoutingService


class FakeQuery:
    async def construct(self, request: UserTurnRequest) -> QueryConstructionResult:
        return QueryConstructionResult(
            request=LocalRetrievalRequest(
                original_query=request.query,
                retrieval_query="rewritten",
                filters=request.filters,
            ),
            attempts=1,
            used_fallback=False,
        )


class FakeLocal:
    def __init__(self, results: list[LocalEvidenceResult]) -> None:
        self.results = results
        self.requests: list[LocalRetrievalRequest] = []

    async def run(self, request: LocalRetrievalRequest) -> LocalEvidenceResult:
        self.requests.append(request)
        return self.results.pop(0)


class FakeWeb:
    def __init__(self, result: WebEvidenceResult) -> None:
        self.result = result
        self.calls = 0

    async def run(self, request: object) -> WebEvidenceResult:
        self.calls += 1
        return self.result


class FakeAnswer:
    def __init__(self) -> None:
        self.inputs: list[AnswerInput] = []

    async def run(
        self, input: AnswerInput
    ) -> tuple[FinalResponse, AnswerExecutionOutcome]:
        self.inputs.append(input)
        return FinalResponse(
            answer_text="Answer.", citations=()
        ), AnswerExecutionOutcome.SUCCESS


def local(
    status: LocalEvidenceStatus,
    *,
    execution: LocalExecutionStatus = LocalExecutionStatus.OK,
    best: bool = False,
) -> LocalEvidenceResult:
    ev = local_evidence()
    if status is LocalEvidenceStatus.SUFFICIENT:
        return LocalEvidenceResult(
            execution_status=execution,
            evidence_status=status,
            selected_evidence=(ev,),
        )
    return LocalEvidenceResult(
        execution_status=execution,
        evidence_status=status,
        selected_evidence=(),
        best_available_evidence=ev if best else None,
    )


def services(
    local_results: list[LocalEvidenceResult], web: FakeWeb
) -> tuple[RagCoreServices, FakeLocal, FakeAnswer]:
    local_runner = FakeLocal(list(local_results))
    answer = FakeAnswer()
    bundle = RagCoreServices(
        query_construction=cast(Any, FakeQuery()),
        local_evidence=cast(Any, local_runner),
        routing=CorrectiveRoutingService(max_retries=1),
        web_evidence=cast(Any, web),
        answer_input=AnswerInputBuilder(),
        answer=cast(Any, answer),
        completion=CompletionAssessor(),
    )
    return bundle, local_runner, answer


@pytest.mark.asyncio
async def test_local_sufficient_answers_without_web() -> None:
    web = FakeWeb(
        WebEvidenceResult(execution_status=WebExecutionStatus.EMPTY, evidence=())
    )
    bundle, local_runner, answer = services(
        [local(LocalEvidenceStatus.SUFFICIENT)], web
    )
    result = await RagCoreRunner(bundle).run(UserTurnRequest(session_id="s", query="q"))
    assert result.completion_status is RagCompletionStatus.SUCCESS
    assert web.calls == 0
    assert len(local_runner.requests) == 1
    assert answer.inputs


@pytest.mark.asyncio
async def test_filename_relaxation_retries_once_and_preserves_hard_filters() -> None:
    web = FakeWeb(
        WebEvidenceResult(execution_status=WebExecutionStatus.EMPTY, evidence=())
    )
    bundle, local_runner, _ = services(
        [
            local(LocalEvidenceStatus.INSUFFICIENT, best=True),
            local(LocalEvidenceStatus.SUFFICIENT),
        ],
        web,
    )
    request = UserTurnRequest(
        session_id="s",
        query="q",
        filters=QueryFilters(file_name="guide.md", sheet_name="Sheet1"),
    )
    result = await RagCoreRunner(bundle).run(request)
    assert result.completion_status is RagCompletionStatus.SUCCESS
    assert len(local_runner.requests) == 2
    second = local_runner.requests[1]
    assert second.filters is not None
    assert second.filters.file_name is None
    assert second.filters.sheet_name == "Sheet1"
    assert web.calls == 0


@pytest.mark.asyncio
async def test_web_fallback_and_no_evidence_safe_failure() -> None:
    web = FakeWeb(
        WebEvidenceResult(
            execution_status=WebExecutionStatus.OK, evidence=(web_evidence(),)
        )
    )
    bundle, _, answer = services(
        [local(LocalEvidenceStatus.INSUFFICIENT, best=True)], web
    )
    result = await RagCoreRunner(bundle).run(UserTurnRequest(session_id="s", query="q"))
    assert result.completion_status in {
        RagCompletionStatus.SUCCESS,
        RagCompletionStatus.DEGRADED_SUCCESS,
    }
    assert web.calls == 1
    answer_input = answer.inputs[0]
    assert answer_input.local_evidence_status.value == "INSUFFICIENT_BEST_MATCH"
    assert len(answer_input.web_evidence) == 1

    empty_web = FakeWeb(
        WebEvidenceResult(execution_status=WebExecutionStatus.EMPTY, evidence=())
    )
    bundle, _, _ = services([local(LocalEvidenceStatus.INSUFFICIENT)], empty_web)
    result = await RagCoreRunner(bundle).run(UserTurnRequest(session_id="s", query="q"))
    assert result.completion_status is RagCompletionStatus.SAFE_FAILURE
    assert result.final_response.citations == ()
