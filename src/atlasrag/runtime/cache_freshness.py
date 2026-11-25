"""Persisted Query Cache freshness barrier and throttled recovery."""

from __future__ import annotations

import asyncio
from enum import StrEnum
from time import monotonic

from atlasrag.domain.errors import (
    CanonicalDataError,
    DependencyError,
    ProviderResponseError,
)
from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.services.sync.ports import SyncUnitOfWorkFactory


class CacheFreshnessState(StrEnum):
    TRUSTED = "TRUSTED"
    BYPASSED = "BYPASSED"


class CacheFreshnessBarrier:
    def __init__(
        self,
        *,
        redis: RedisCacheProvider,
        uow_factory: SyncUnitOfWorkFactory,
        retry_interval_seconds: float = 10.0,
    ) -> None:
        self._redis = redis
        self._uow_factory = uow_factory
        self._retry_interval = retry_interval_seconds
        self._lock = asyncio.Lock()
        self._last_attempt = 0.0

    async def _marker_required(self) -> bool:
        async with self._uow_factory() as unit:
            metadata = await unit.runtime_metadata.get()
        return metadata.query_cache_invalidation_required

    async def enforce(self) -> CacheFreshnessState:
        async with self._lock:
            return await self._enforce_locked()

    async def _enforce_locked(self) -> CacheFreshnessState:
        self._last_attempt = monotonic()
        try:
            required = await self._marker_required()
        except CanonicalDataError:
            raise
        except DependencyError:
            return CacheFreshnessState.BYPASSED
        if not required:
            return CacheFreshnessState.TRUSTED
        try:
            await self._redis.invalidate_queries()
        except (DependencyError, ProviderResponseError):
            return CacheFreshnessState.BYPASSED
        try:
            async with self._uow_factory() as unit:
                metadata = (
                    await unit.runtime_metadata.set_query_cache_invalidation_required(
                        False
                    )
                )
                if metadata.query_cache_invalidation_required:
                    raise CanonicalDataError
                await unit.commit()
            if await self._marker_required():
                return CacheFreshnessState.BYPASSED
        except CanonicalDataError:
            raise
        except (DependencyError, ProviderResponseError):
            return CacheFreshnessState.BYPASSED
        return CacheFreshnessState.TRUSTED

    async def recover_if_due(self) -> CacheFreshnessState | None:
        # The due check must be inside the same lock as enforcement so several
        # callers cannot all pass the same stale check and repeat recovery.
        async with self._lock:
            if monotonic() - self._last_attempt < self._retry_interval:
                return None
            return await self._enforce_locked()
