from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from atlasrag.domain.errors import CanonicalDataError, ProviderResponseError
from atlasrag.runtime.cache_freshness import CacheFreshnessBarrier, CacheFreshnessState


@dataclass
class _Metadata:
    query_cache_invalidation_required: bool


class _Repo:
    def __init__(self, unit: _Unit) -> None:
        self._unit = unit

    async def get(self) -> _Metadata:
        return _Metadata(self._unit.required)

    async def set_query_cache_invalidation_required(self, required: bool) -> _Metadata:
        self._unit.required = required
        self._unit.events.append(("clear" if not required else "set", required))
        return _Metadata(required)


class _Unit:
    def __init__(self, required: bool, failure: str | None = None) -> None:
        self.required = required
        self.failure = failure
        self.events: list[tuple[str, bool]] = []
        self.runtime_metadata = _Repo(self)

    async def __aenter__(self) -> _Unit:
        if self.failure == "read":
            raise CanonicalDataError
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        if self.failure == "commit":
            raise RuntimeError("commit failed")


class _Redis:
    def __init__(self, unit: _Unit, failure: bool = False) -> None:
        self._unit = unit
        self.failure = failure
        self.calls = 0

    async def invalidate_queries(self) -> int:
        self.calls += 1
        self._unit.events.append(("invalidate", True))
        if self.failure:
            raise ProviderResponseError
        return 1


def _barrier(unit: _Unit, redis: _Redis) -> CacheFreshnessBarrier:
    return CacheFreshnessBarrier(
        redis=redis,  # type: ignore[arg-type]
        uow_factory=lambda: unit,  # type: ignore[arg-type,return-value]
    )


@pytest.mark.asyncio
async def test_marker_true_invalidates_before_clear() -> None:
    unit = _Unit(True)
    redis = _Redis(unit)
    assert await _barrier(unit, redis).enforce() is CacheFreshnessState.TRUSTED
    assert unit.events == [("invalidate", True), ("clear", False)]
    assert not unit.required


@pytest.mark.asyncio
async def test_partial_invalidation_keeps_marker_true() -> None:
    unit = _Unit(True)
    redis = _Redis(unit, failure=True)
    assert await _barrier(unit, redis).enforce() is CacheFreshnessState.BYPASSED
    assert unit.required
    assert unit.events == [("invalidate", True)]


@pytest.mark.asyncio
async def test_canonical_marker_error_propagates() -> None:
    unit = _Unit(True, failure="read")
    with pytest.raises(CanonicalDataError):
        await _barrier(unit, _Redis(unit)).enforce()


@pytest.mark.asyncio
async def test_concurrent_recovery_is_serialized() -> None:
    unit = _Unit(True)
    redis = _Redis(unit)
    barrier = _barrier(unit, redis)
    await asyncio.gather(barrier.enforce(), barrier.enforce())
    assert redis.calls == 1
    assert not unit.required
