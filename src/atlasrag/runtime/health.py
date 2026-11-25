"""Process-local health/readiness snapshots, outside business graphs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic

from atlasrag.runtime.modes import RuntimeMode


class DependencyHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class HealthSnapshot:
    runtime: RuntimeMode
    hard_healthy: bool
    knowledge_verified: bool
    postgres: DependencyHealth
    milvus: DependencyHealth
    redis: DependencyHealth
    searxng: DependencyHealth
    embedding: DependencyHealth
    reranker: DependencyHealth
    query_cache: str
    generation: int
    lifecycle_generation: int
    checked_at: datetime
    monotonic_at: float


Probe = Callable[[], Awaitable[None]]
LifecycleGeneration = Callable[[], int]


def _starting_generation() -> int:
    return 0


class HealthRegistry:
    """Bounded single-flight probes with monotonic freshness and generations."""

    def __init__(
        self,
        *,
        mode: Callable[[], RuntimeMode],
        postgres: Probe,
        milvus: Probe,
        redis: Probe,
        embedding: Probe,
        searxng: Probe | None = None,
        reranker: Probe | None = None,
        probe_timeout: float = 3.0,
        refresh_interval: float = 10.0,
        lifecycle_generation: LifecycleGeneration | None = None,
    ) -> None:
        self._mode = mode
        self._lifecycle_generation = (
            _starting_generation
            if lifecycle_generation is None
            else lifecycle_generation
        )
        self._probes = {
            "postgres": postgres,
            "milvus": milvus,
            "redis": redis,
            "embedding": embedding,
            "searxng": searxng,
            "reranker": reranker,
        }
        self._probe_timeout = probe_timeout
        self._refresh_interval = refresh_interval
        self._snapshot: HealthSnapshot | None = None
        self._generation = 0
        self._knowledge_verified = False
        self._refresh_lock = asyncio.Lock()

    def set_knowledge_verified(self, verified: bool) -> None:
        self._knowledge_verified = verified

    @property
    def snapshot(self) -> HealthSnapshot | None:
        return self._snapshot

    async def _probe(self, name: str) -> DependencyHealth:
        probe = self._probes[name]
        if probe is None:
            return DependencyHealth.UNKNOWN
        try:
            await asyncio.wait_for(probe(), timeout=self._probe_timeout)
        except Exception:
            return DependencyHealth.UNAVAILABLE
        return DependencyHealth.HEALTHY

    async def refresh(self) -> HealthSnapshot:
        async with self._refresh_lock:
            lifecycle = self._lifecycle_generation()
            values = await asyncio.gather(*(self._probe(name) for name in self._probes))
            status = dict(zip(self._probes, values, strict=True))
            hard = (
                status["postgres"] is DependencyHealth.HEALTHY
                and status["milvus"] is DependencyHealth.HEALTHY
                and status["embedding"] is DependencyHealth.HEALTHY
                and self._knowledge_verified
            )
            self._generation += 1
            snapshot = HealthSnapshot(
                runtime=self._mode(),
                hard_healthy=hard,
                knowledge_verified=self._knowledge_verified,
                postgres=status["postgres"],
                milvus=status["milvus"],
                redis=status["redis"],
                searxng=status["searxng"],
                embedding=status["embedding"],
                reranker=status["reranker"],
                query_cache="UNKNOWN",
                generation=self._generation,
                lifecycle_generation=lifecycle,
                checked_at=datetime.now(UTC),
                monotonic_at=monotonic(),
            )
            self._snapshot = snapshot
            return snapshot

    async def refresh_if_stale(self) -> HealthSnapshot:
        current = self._snapshot
        if (
            current is not None
            and monotonic() - current.monotonic_at < self._refresh_interval
        ):
            return current
        return await self.refresh()

    async def readiness(self) -> bool:
        snapshot = await self.refresh_if_stale()
        if snapshot.lifecycle_generation != self._lifecycle_generation():
            snapshot = await self.refresh()
        if snapshot.lifecycle_generation != self._lifecycle_generation():
            return False
        return self._mode() is RuntimeMode.SERVING and snapshot.hard_healthy
