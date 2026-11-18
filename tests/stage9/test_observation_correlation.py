from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from stage7.helpers import local_evidence

from atlasrag.application.rag_core import RagCoreRunner
from atlasrag.domain.answer import AnswerInput
from atlasrag.domain.enums import (
    AnswerExecutionOutcome,
    LocalEvidenceStatus,
    LocalExecutionStatus,
    WebExecutionStatus,
)
from atlasrag.domain.evidence import LocalEvidenceResult
from atlasrag.domain.observation import ObservationStage, ProductionObservation
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


class Query:
    async def construct(self, request: UserTurnRequest) -> QueryConstructionResult:
        return QueryConstructionResult(
            request=LocalRetrievalRequest(
                original_query=request.query,
                retrieval_query=request.query,
                filters=request.filters,
            ),
            attempts=1,
            used_fallback=False,
        )


class Local:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request: LocalRetrievalRequest) -> LocalEvidenceResult:
        self.calls += 1
        if self.calls == 1:
            return LocalEvidenceResult(
                execution_status=LocalExecutionStatus.OK,
                evidence_status=LocalEvidenceStatus.INSUFFICIENT,
                selected_evidence=(),
                best_available_evidence=local_evidence(),
            )
        return LocalEvidenceResult(
            execution_status=LocalExecutionStatus.OK,
            evidence_status=LocalEvidenceStatus.SUFFICIENT,
            selected_evidence=(local_evidence(),),
        )


class Web:
    async def run(self, request: object) -> WebEvidenceResult:
        return WebEvidenceResult(execution_status=WebExecutionStatus.EMPTY, evidence=())


class Answer:
    async def run(
        self, input: AnswerInput
    ) -> tuple[FinalResponse, AnswerExecutionOutcome]:
        return FinalResponse(
            answer_text="Answer.", citations=()
        ), AnswerExecutionOutcome.SUCCESS


class Capture:
    def __init__(self) -> None:
        self.events: list[ProductionObservation] = []

    def observe(self, event: ProductionObservation) -> None:
        self.events.append(event)


async def _run(capture: Capture) -> None:
    runner = RagCoreRunner(
        RagCoreServices(
            query_construction=cast(Any, Query()),
            local_evidence=cast(Any, Local()),
            routing=CorrectiveRoutingService(max_retries=1),
            web_evidence=cast(Any, Web()),
            answer_input=AnswerInputBuilder(),
            answer=cast(Any, Answer()),
            completion=CompletionAssessor(),
            observer=capture,
        )
    )
    await runner.run(
        UserTurnRequest(
            session_id="s", query="question", filters=QueryFilters(file_name="x.md")
        )
    )


@pytest.mark.asyncio
async def test_invocation_and_attempt_correlation_under_concurrency() -> None:
    first, second = Capture(), Capture()
    await asyncio.gather(_run(first), _run(second))
    for capture in (first, second):
        ids = {event.invocation_id for event in capture.events}
        assert len(ids) == 1 and None not in ids
        assert {event.attempt_index for event in capture.events} == {1, 2}
        assert any(event.stage is ObservationStage.ROUTING for event in capture.events)
        assert any(event.stage is ObservationStage.ANSWER for event in capture.events)
    assert {event.invocation_id for event in first.events}.isdisjoint(
        {event.invocation_id for event in second.events}
    )


class AlwaysInsufficient:
    async def run(self, request: LocalRetrievalRequest) -> LocalEvidenceResult:
        return LocalEvidenceResult(
            execution_status=LocalExecutionStatus.OK,
            evidence_status=LocalEvidenceStatus.INSUFFICIENT,
            selected_evidence=(),
        )


@pytest.mark.asyncio
async def test_safe_failure_uses_terminal_attempt_two() -> None:
    capture = Capture()
    runner = RagCoreRunner(
        RagCoreServices(
            query_construction=cast(Any, Query()),
            local_evidence=cast(Any, AlwaysInsufficient()),
            routing=CorrectiveRoutingService(max_retries=1),
            web_evidence=cast(Any, Web()),
            answer_input=AnswerInputBuilder(),
            answer=cast(Any, Answer()),
            completion=CompletionAssessor(),
            observer=capture,
        )
    )
    await runner.run(
        UserTurnRequest(
            session_id="s", query="question", filters=QueryFilters(file_name="x.md")
        )
    )
    failures = [
        event
        for event in capture.events
        if event.stage is ObservationStage.ANSWER
        and event.status == AnswerExecutionOutcome.SAFE_FAILURE.value
    ]
    assert len(failures) == 1
    assert failures[0].attempt_index == 2


class RaisingObserver:
    def observe(self, event: ProductionObservation) -> None:
        raise RuntimeError("observer unavailable")


@pytest.mark.asyncio
async def test_observer_failure_does_not_change_product_result() -> None:
    runner = RagCoreRunner(
        RagCoreServices(
            query_construction=cast(Any, Query()),
            local_evidence=cast(Any, Local()),
            routing=CorrectiveRoutingService(max_retries=1),
            web_evidence=cast(Any, Web()),
            answer_input=AnswerInputBuilder(),
            answer=cast(Any, Answer()),
            completion=CompletionAssessor(),
            observer=RaisingObserver(),
        )
    )
    with pytest.warns(RuntimeWarning, match="observation delivery failed"):
        result = await runner.run(
            UserTurnRequest(
                session_id="s",
                query="question",
                filters=QueryFilters(file_name="x.md"),
            )
        )
    assert result.final_response.answer_text == "Answer."
