"""Query Construction, protected-term, and corrective-routing tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from stage7.helpers import local_evidence

from atlasrag.domain.enums import (
    LocalEvidenceStatus,
    LocalExecutionStatus,
    SourceType,
)
from atlasrag.domain.evidence import LocalEvidenceResult
from atlasrag.domain.requests import (
    LocalRetrievalRequest,
    QueryFilters,
    UserTurnRequest,
)
from atlasrag.services.rag_core.query import (
    QueryConstructionService,
    QueryRewriteOutput,
    protected_terms,
)
from atlasrag.services.rag_core.routing import (
    CorrectiveAction,
    CorrectiveRoutingService,
)


class FakeRewrite:
    def __init__(self, *values: object) -> None:
        self.values = list(values)
        self.calls: list[tuple[str, str, str]] = []

    async def rewrite(
        self, query: str, *, now: str, timezone_name: str
    ) -> QueryRewriteOutput:
        self.calls.append((query, now, timezone_name))
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return QueryRewriteOutput(retrieval_query=str(value))


def request(query: str, filters: QueryFilters | None = None) -> UserTurnRequest:
    return UserTurnRequest(session_id="session-1", query=query, filters=filters)


@pytest.mark.asyncio
async def test_query_construction_preserves_raw_query_filters_and_terms() -> None:
    provider = FakeRewrite("ERR-104 manual-v2.3.pdf troubleshooting")
    service = QueryConstructionService(
        provider=provider,
        clock=lambda: datetime(2026, 11, 14, 12, 0, tzinfo=UTC),
    )
    filters = QueryFilters(file_name="manual-v2.3.pdf", source_type=SourceType.MD)
    result = await service.construct(
        request("查一下 ERR-104 在 manual-v2.3.pdf 中的处理方式", filters)
    )
    assert (
        result.request.original_query
        == "查一下 ERR-104 在 manual-v2.3.pdf 中的处理方式"
    )
    assert result.request.filters == filters
    assert result.request.filters.source_type is SourceType.MD
    assert result.used_fallback is False
    assert provider.calls[0][1].startswith("2026-11-14T12:00:00")


@pytest.mark.asyncio
async def test_protected_term_loss_retries_then_falls_back_to_original() -> None:
    provider = FakeRewrite("troubleshooting", "generic", "again")
    result = await QueryConstructionService(
        provider=provider,
        clock=lambda: datetime(2026, 11, 14, tzinfo=UTC),
    ).construct(request("ERR-104 manual-v2.3.pdf"))
    assert result.used_fallback
    assert result.request.retrieval_query == result.request.original_query
    assert len(provider.calls) == 3


def test_protected_terms_extracts_identifiers() -> None:
    assert "err-104" in protected_terms("ERR-104 manual-v2.3.pdf")
    assert "manual-v2.3.pdf" in protected_terms("ERR-104 manual-v2.3.pdf")


def result(
    status: LocalEvidenceStatus,
    *,
    execution: LocalExecutionStatus = LocalExecutionStatus.OK,
    best: bool = False,
) -> LocalEvidenceResult:
    evidence = (local_evidence(),)
    if execution is LocalExecutionStatus.UNAVAILABLE:
        return LocalEvidenceResult(execution_status=LocalExecutionStatus.UNAVAILABLE)
    if status is LocalEvidenceStatus.SUFFICIENT:
        return LocalEvidenceResult(
            execution_status=execution,
            evidence_status=status,
            selected_evidence=evidence,
        )
    return LocalEvidenceResult(
        execution_status=execution,
        evidence_status=status,
        selected_evidence=(),
        best_available_evidence=evidence[0] if best else None,
    )


def local_request(query: str, *, file: str | None = None) -> LocalRetrievalRequest:
    return LocalRetrievalRequest(
        original_query="original",
        retrieval_query=query,
        filters=None if file is None else QueryFilters(file_name=file),
    )


def test_routing_matrix_and_retry_identity_guard() -> None:
    router = CorrectiveRoutingService(max_retries=1)
    base = local_request("rewritten", file="guide.md")
    assert (
        router.decide(
            base=base,
            current=base,
            result=result(
                LocalEvidenceStatus.SUFFICIENT, execution=LocalExecutionStatus.DEGRADED
            ),
            attempt_index=1,
        ).action
        is CorrectiveAction.ANSWER
    )
    assert (
        router.decide(
            base=base,
            current=base,
            result=result(LocalEvidenceStatus.INSUFFICIENT),
            attempt_index=1,
        ).action
        is CorrectiveAction.RETRY_LOCAL
    )
    retry = router.decide(
        base=base,
        current=base,
        result=result(LocalEvidenceStatus.INSUFFICIENT),
        attempt_index=1,
    ).request
    assert (
        retry is not None
        and retry.filters is not None
        and retry.filters.file_name is None
    )
    assert retry.filters.source_type is None
    assert (
        router.decide(
            base=base,
            current=base,
            result=result(
                LocalEvidenceStatus.INSUFFICIENT,
                execution=LocalExecutionStatus.UNAVAILABLE,
            ),
            attempt_index=1,
        ).action
        is CorrectiveAction.WEB
    )
    assert (
        router.decide(
            base=base,
            current=base,
            result=result(LocalEvidenceStatus.INSUFFICIENT),
            attempt_index=2,
        ).action
        is CorrectiveAction.WEB
    )
    no_file = local_request("rewritten")
    assert (
        router.decide(
            base=no_file,
            current=no_file,
            result=result(LocalEvidenceStatus.INSUFFICIENT),
            attempt_index=1,
        ).action
        is CorrectiveAction.WEB
    )


@pytest.mark.asyncio
async def test_timezone_conversion_crosses_utc_day_boundary() -> None:
    from zoneinfo import ZoneInfo

    provider = FakeRewrite("rewritten query")
    result = await QueryConstructionService(
        provider=provider,
        clock=lambda: datetime(2026, 11, 14, 23, 30, tzinfo=UTC),
        timezone=ZoneInfo("Asia/Tokyo"),
    ).construct(request("maintenance interval"))
    assert result.request.retrieval_query == "rewritten query"
    now, timezone_name = provider.calls[0][1], provider.calls[0][2]
    assert now.startswith("2026-11-15T08:30:00+09:00")
    assert timezone_name in {"JST", "Asia/Tokyo"}


@pytest.mark.asyncio
async def test_unexpected_value_error_propagates() -> None:
    provider = FakeRewrite(ValueError("programming bug"))
    with pytest.raises(ValueError, match="programming bug"):
        await QueryConstructionService(
            provider=provider,
            clock=lambda: datetime(2026, 11, 14, tzinfo=UTC),
        ).construct(request("maintenance interval"))
