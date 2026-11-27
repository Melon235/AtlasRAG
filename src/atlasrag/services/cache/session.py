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
from atlasrag.providers.cache.keys import is_cache_key_component_safe
from atlasrag.providers.cache.models import CachedQueryResult
from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.sync.ports import SyncUnitOfWorkFactory


class QueryCacheStatus(StrEnum):
    """Lookup outcome reported truthfully at the retrieval boundary."""

    HIT = "HIT"
    MISS = "MISS"
    BYPASSED = "BYPASSED"
    UNKNOWN = "UNKNOWN"


class CacheCommitOutcome(StrEnum):
    STORED = "STORED"
    BYPASSED = "BYPASSED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PreparedCacheWrite:
    request: UserTurnRequest
    payload: CachedQueryResult
    write_fence: str | None = None


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
        require_write_fence: bool = False,
        expected_write_fence: Callable[[], str | None] | None = None,
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
        self._require_write_fence = require_write_fence
        self._expected_write_fence = expected_write_fence

    async def lookup_with_status(
        self, request: UserTurnRequest
    ) -> tuple[FinalResponse | None, QueryCacheStatus]:
        """Look up a cached response and report whether the cache hit, missed, or bypassed."""

        if not self._policy.request_is_cacheable(request):
            return None, QueryCacheStatus.BYPASSED
        expected_fence: str | None = None
        if self._expected_write_fence is not None:
            expected_fence = self._expected_write_fence()
            if expected_fence is None:
                return None, QueryCacheStatus.BYPASSED
        if not await self._trusted():
            return None, QueryCacheStatus.BYPASSED
        key_hash = request_hash(request)
        try:
            if hasattr(self._redis, "get_fenced_query"):
                fence, cached = await self._redis.get_fenced_query(
                    request.session_id,
                    self._runtime_fingerprint,
                    key_hash,
                )
                if self._require_write_fence and fence is None:
                    return None, QueryCacheStatus.BYPASSED
                if expected_fence is not None and fence != expected_fence:
                    return None, QueryCacheStatus.BYPASSED
            else:
                cached = await self._redis.get_query(
                    request.session_id,
                    self._runtime_fingerprint,
                    key_hash,
                )
        except (DependencyError, ProviderResponseError):
            return None, QueryCacheStatus.BYPASSED
        if cached is None:
            return None, QueryCacheStatus.MISS
        return cached.final_response, QueryCacheStatus.HIT

    async def lookup(self, request: UserTurnRequest) -> FinalResponse | None:
        response, _ = await self.lookup_with_status(request)
        return response

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
        expected_fence: str | None = None
        if self._expected_write_fence is not None:
            expected_fence = self._expected_write_fence()
            if expected_fence is None:
                return None
        if not await self._trusted():
            return None
        write_fence: str | None = None
        if hasattr(self._redis, "get_query_write_fence"):
            try:
                write_fence = await self._redis.get_query_write_fence()
            except (DependencyError, ProviderResponseError):
                if self._require_write_fence:
                    return None
                write_fence = None
            if self._require_write_fence and write_fence is None:
                return None
            if expected_fence is not None and write_fence != expected_fence:
                return None
        elif self._require_write_fence:
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
        return PreparedCacheWrite(
            request=request,
            payload=payload,
            write_fence=write_fence,
        )

    async def commit(self, prepared: PreparedCacheWrite) -> CacheCommitOutcome:
        if not is_cache_key_component_safe(prepared.request.session_id):
            return CacheCommitOutcome.BYPASSED
        key_hash = request_hash(prepared.request)
        if hasattr(self._redis, "put_fenced_query"):
            if self._require_write_fence and prepared.write_fence is None:
                return CacheCommitOutcome.BYPASSED
            write: asyncio.Task[object] = asyncio.create_task(
                self._redis.put_fenced_query(
                    prepared.request.session_id,
                    self._runtime_fingerprint,
                    key_hash,
                    prepared.payload,
                    ttl_seconds=self._ttl_seconds,
                    write_fence=prepared.write_fence,
                )
            )
            fenced = True
        else:
            if self._require_write_fence:
                return CacheCommitOutcome.BYPASSED
            write = asyncio.create_task(
                self._redis.put_query(
                    prepared.request.session_id,
                    self._runtime_fingerprint,
                    key_hash,
                    prepared.payload,
                    ttl_seconds=self._ttl_seconds,
                )
            )
            fenced = False
        cancelled = await _settle_write(write)
        if cancelled:
            raise asyncio.CancelledError
        if write.cancelled():
            return CacheCommitOutcome.UNKNOWN
        try:
            outcome = write.result()
        except ProviderResponseError:
            return CacheCommitOutcome.UNKNOWN
        except DependencyError:
            # The write coroutine was submitted, but a transport failure does not
            # establish that Redis did not execute it. Classify conservatively.
            return CacheCommitOutcome.UNKNOWN
        if fenced:
            if outcome is True:
                return CacheCommitOutcome.STORED
            if outcome is False:
                return CacheCommitOutcome.BYPASSED
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
