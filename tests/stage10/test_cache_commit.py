from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from atlasrag.domain.enums import EvidenceType, SourceKind
from atlasrag.domain.errors import DependencyError, ErrorCode, ProviderResponseError
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalLocalCitation, FinalResponse
from atlasrag.providers.cache.models import CachedQueryResult
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.cache.session import (
    CacheCommitOutcome,
    PreparedCacheWrite,
    SessionQueryCache,
)


class _Redis:
    async def put_query(self, *args: object, **kwargs: object) -> None:
        raise DependencyError(ErrorCode.DEPENDENCY_UNAVAILABLE)


class _Unit:
    async def __aenter__(self) -> _Unit:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def _cache(redis: object) -> SessionQueryCache:
    return SessionQueryCache(
        redis=redis,  # type: ignore[arg-type]
        uow_factory=cast(Any, lambda: _Unit()),
        runtime_fingerprint="runtime",
        ttl_seconds=60,
        policy=QueryCachePolicy(enabled=True),
        clock=lambda: datetime.now(UTC),
    )


def _prepared() -> PreparedCacheWrite:
    response = FinalResponse(
        answer_text="Answer [L1].",
        citations=(
            FinalLocalCitation(
                citation_id="L1",
                source_kind=SourceKind.LOCAL,
                file_name="guide.md",
                relative_source_path="guide.md",
                evidence_type=EvidenceType.TEXT_PARENT,
            ),
        ),
    )
    return PreparedCacheWrite(
        request=UserTurnRequest(session_id="s", query="q"),
        payload=CachedQueryResult(
            created_at=datetime.now(UTC), final_response=response
        ),
    )


@pytest.mark.asyncio
async def test_cache_commit_classifies_redis_failure_without_raising() -> None:
    outcome = await _cache(_Redis()).commit(_prepared())
    # A submitted SET whose transport failed cannot be proven not to have run.
    assert outcome is CacheCommitOutcome.UNKNOWN


@pytest.mark.asyncio
async def test_cache_commit_applied_write_with_bad_ack_is_unknown() -> None:
    class Ambiguous:
        async def put_query(self, *args: object, **kwargs: object) -> None:
            raise ProviderResponseError()

    outcome = await _cache(Ambiguous()).commit(_prepared())
    assert outcome is CacheCommitOutcome.UNKNOWN


@pytest.mark.asyncio
async def test_cache_commit_cancellation_settles_write_and_propagates() -> None:
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    class Blocking:
        async def put_query(self, *args: object, **kwargs: object) -> None:
            started.set()
            await release.wait()

    task = asyncio.create_task(_cache(Blocking()).commit(_prepared()))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
