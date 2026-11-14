"""Session Query Cache read/write with persisted freshness trust checks."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from atlasrag.application.hashing import request_hash
from atlasrag.domain.errors import DependencyError, ProviderResponseError
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalResponse, RagCoreResult
from atlasrag.providers.cache.models import CachedQueryResult
from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.sync.ports import SyncUnitOfWorkFactory


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

    async def store(self, request: UserTurnRequest, result: RagCoreResult) -> bool:
        decision = self._policy.decide(request, result)
        if not decision.eligible:
            return False
        if not await self._trusted():
            return False
        payload = CachedQueryResult(
            created_at=self._clock(),
            final_response=result.final_response,
        )
        if (
            len(payload.model_dump_json().encode("utf-8"))
            > self._policy.max_entry_bytes
        ):
            return False
        try:
            await self._redis.put_query(
                request.session_id,
                self._runtime_fingerprint,
                request_hash(request),
                payload,
                ttl_seconds=self._ttl_seconds,
            )
        except DependencyError:
            return False
        return True

    async def _trusted(self) -> bool:
        try:
            async with self._uow_factory() as unit:
                metadata = await unit.runtime_metadata.get()
        except DependencyError:
            return False
        return not metadata.query_cache_invalidation_required
