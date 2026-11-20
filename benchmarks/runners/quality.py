"""Quality-run adapter around the real production retrieval boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse
from benchmarks.contracts.models import BenchmarkQuery
from benchmarks.runners.manifests import CaseObservation, CaseStatus


@dataclass(frozen=True)
class QualityCase:
    query_id: str
    observation: CaseObservation
    final_response: FinalResponse | None


RetrievalCallable = Callable[[UserTurnRequest], Awaitable[FinalResponse]]


async def run_quality_case(
    query: BenchmarkQuery,
    *,
    run_id: str,
    repetition_index: int,
    retrieve: RetrievalCallable,
) -> QualityCase:
    request = UserTurnRequest(
        session_id=f"benchmark:{run_id}:{query.query_id}:{repetition_index}",
        query=query.text,
    )
    try:
        response = await retrieve(request)
    except Exception as error:
        return QualityCase(
            query_id=query.query_id,
            observation=CaseObservation(
                query_id=query.query_id,
                repetition_index=repetition_index,
                status=CaseStatus.TECHNICAL_FAILURE,
                failure_reason=type(error).__name__,
            ),
            final_response=None,
        )
    return QualityCase(
        query_id=query.query_id,
        observation=CaseObservation(
            query_id=query.query_id,
            repetition_index=repetition_index,
            status=CaseStatus.COMPLETED,
            product_outcome="FINAL_RESPONSE",
        ),
        final_response=response,
    )
