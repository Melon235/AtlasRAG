"""Session Query Cache with explicit completed-result commit boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from atlasrag.application.hashing import request_hash
from atlasrag.domain.errors import DependencyError, ProviderResponseError
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse, RagCoreResult
from atlasrag.providers.cache.models import CachedQueryResult
from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.sync.ports import SyncUnitOfWorkFactory


class CacheCommitOutcome(StrEnum):
    STORED = "STORED"
    BYPASSED = "BYPASSED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PreparedCacheWrite:
    request: UserTurnRequest
    payload: CachedQueryResult


async def _settle_write(write: asyncio.Task[object]) -> bool:
    cancelled = False
    while not write.done():
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            cancelled = True
            continue
        except BaseException:
            break
    if not write.cancelled():
        # The caller classifies failures; settlement must not raise out of
        # the cancellation-cleanup boundary.
        try:
            write.result()
        except BaseException:
            pass
    return cancelled


class SessionQueryCache:
    def __init__(
        self,
        *,
        redis: RedisCacheProvider,
        uow_factory: SyncUnitOfWorkFactory,
        runtime_fingerprint: str,
        ttl_seconds: int,
        policy: QueryCachePolicy,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(runtime_fingerprint, str) or not runtime_fingerprint:
            raise ValueError("runtime_fingerprint must be nonempty")
        if type(ttl_seconds) is not int or ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self._redis = redis
        self._uow_factory = uow_factory
        self._runtime_fingerprint = runtime_fingerprint
        self._ttl_seconds = ttl_seconds
        self._policy = policy
        self._clock = clock

    async def lookup(self, request: UserTurnRequest) -> FinalResponse | None:
        if not self._policy.request_is_cacheable(request):
            return None
        if not await self._trusted():
            return None
        try:
            cached = await self._redis.get_query(
                request.session_id,
                self._runtime_fingerprint,
                request_hash(request),
            )
        except (DependencyError, ProviderResponseError):
            return None
        if cached is None:
            return None
        return cached.final_response

    async def prepare(
        self,
        request: UserTurnRequest,
        result: RagCoreResult,
        *,
        web_invoked: bool | None = None,
    ) -> PreparedCacheWrite | None:
        if web_invoked is None:
            return None
        if type(web_invoked) is not bool:
            raise ValueError("web_invoked must be boolean")
        decision = self._policy.decide(request, result, web_invoked=web_invoked)
        if not decision.eligible:
            return None
        if not await self._trusted():
            return None
        payload = CachedQueryResult(
            created_at=self._clock(),
            final_response=result.final_response,
        )
        if (
            len(payload.model_dump_json().encode("utf-8"))
            > self._policy.max_entry_bytes
        ):
            return None
        return PreparedCacheWrite(request=request, payload=payload)

    async def commit(self, prepared: PreparedCacheWrite) -> CacheCommitOutcome:
        write = asyncio.create_task(
            self._redis.put_query(
                prepared.request.session_id,
                self._runtime_fingerprint,
                request_hash(prepared.request),
                prepared.payload,
                ttl_seconds=self._ttl_seconds,
            )
        )
        cancelled = await _settle_write(write)
        if cancelled:
            raise asyncio.CancelledError
        if write.cancelled():
            return CacheCommitOutcome.UNKNOWN
        try:
            write.result()
        except ProviderResponseError:
            return CacheCommitOutcome.UNKNOWN
        except DependencyError:
            # The SET coroutine was submitted, but a transport failure does not
            # establish that Redis did not execute it. Classify conservatively.
            return CacheCommitOutcome.UNKNOWN
        return CacheCommitOutcome.STORED

    async def store(
        self,
        request: UserTurnRequest,
        result: RagCoreResult,
        *,
        web_invoked: bool | None = None,
    ) -> bool:
        prepared = await self.prepare(request, result, web_invoked=web_invoked)
        if prepared is None:
            return False
        return await self.commit(prepared) is CacheCommitOutcome.STORED

    async def _trusted(self) -> bool:
        try:
            async with self._uow_factory() as unit:
                metadata = await unit.runtime_metadata.get()
        except DependencyError:
            return False
        return not metadata.query_cache_invalidation_required
