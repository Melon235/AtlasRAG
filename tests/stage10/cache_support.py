"""A-R4 deterministic cache, Redis transport, marker and lifecycle harness.

Redis is faked at the raw command boundary so the real ``RedisCacheProvider``
key validation, SCAN/UNLINK batching and response contracts are exercised. The
PostgreSQL marker uses an explicit transaction fake with committed and working
state plus commit/rollback/ambiguity injection. Query Cache, freshness barrier,
retrieval wrapper, RagCore wrapper, gate, maintenance coordinator and
application coordinator are the real production implementations.
"""

from __future__ import annotations

import asyncio
import fnmatch
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError as RedisResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError
from runtime_harness import CloseTracker, RecordingOwnership, build_health, request
from stage7.helpers import local_evidence

from atlasrag.application.rag_core import RagCoreRunner
from atlasrag.application.retrieval import RetrievalRunner
from atlasrag.domain.answer import AnswerInput
from atlasrag.domain.enums import (
    AnswerExecutionOutcome,
    EvidenceType,
    LocalEvidenceStatus,
    LocalExecutionStatus,
    RagCompletionStatus,
    WebExecutionStatus,
)
from atlasrag.domain.errors import (
    CanonicalDataError,
    DependencyTimeoutError,
    DependencyUnavailableError,
)
from atlasrag.domain.evidence import LocalEvidenceResult
from atlasrag.domain.requests import LocalRetrievalRequest, UserTurnRequest
from atlasrag.domain.results import FinalLocalCitation, FinalResponse, RagCoreResult
from atlasrag.domain.web import WebEvidenceResult
from atlasrag.graphs.rag_core.subgraph import RagCoreServices
from atlasrag.graphs.retrieval.graph import RetrievalGraphServices
from atlasrag.providers.cache.keys import QUERY_WRITE_FENCE_KEY
from atlasrag.providers.cache.models import CachedQueryResult
from atlasrag.providers.cache.redis import (
    _ENSURE_QUERY_WRITE_FENCE_SCRIPT,
    _GET_FENCED_QUERY_SCRIPT,
    _PUT_FENCED_QUERY_SCRIPT,
    _PUT_UNFENCED_IF_UNPROTECTED_SCRIPT,
    RedisCacheProvider,
)
from atlasrag.repositories.postgres.records import RuntimeMetadata
from atlasrag.runtime.cache_freshness import CacheFreshnessBarrier
from atlasrag.runtime.coordinator import (
    ApplicationCoordinator,
    CoordinatorDependencies,
    QueryResponseEnvelope,
)
from atlasrag.runtime.gate import RuntimeGate
from atlasrag.runtime.health import HealthRegistry
from atlasrag.runtime.maintenance import MaintenanceCoordinator
from atlasrag.runtime.tasks import InFlightTaskRegistry
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.cache.session import PreparedCacheWrite, SessionQueryCache
from atlasrag.services.rag_core.boundary import AnswerInputBuilder
from atlasrag.services.rag_core.completion import CompletionAssessor
from atlasrag.services.rag_core.query import QueryConstructionResult
from atlasrag.services.rag_core.routing import CorrectiveRoutingService
from atlasrag.services.sync.models import (
    GlobalSafetyResult,
    ReconciliationOutcome,
    ReconciliationResult,
    ServingSafety,
    SyncOutcome,
    SyncResult,
)
from atlasrag.services.sync.source_safety import SourceSafetyResult

NOW = datetime(2026, 11, 26, 0, 0, tzinfo=UTC)
SAFE = SyncResult(
    outcome=SyncOutcome.SUCCESS, serving_safety=ServingSafety.SAFE_TO_SERVE
)
UNSAFE = SyncResult(
    outcome=SyncOutcome.FAILED,
    serving_safety=ServingSafety.UNSAFE_TO_SERVE,
    unsafe_reasons=("KNOWLEDGE_UNSAFE",),
    blocked_code="KNOWLEDGE_UNSAFE",
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRawRedis:
    """Raw Redis command surface with state, ordering and fault injection."""

    def __init__(self, *, page_size: int = 100) -> None:
        self.values: dict[str, str] = {}
        self.page_size = page_size
        self.get_calls = 0
        self.set_calls = 0
        self.scan_calls = 0
        self.unlink_calls = 0
        self.delete_calls = 0
        self.eval_calls = 0
        self.fence_get_calls = 0
        self.fence_set_calls = 0
        self.fence_delete_calls = 0
        self.ping_calls = 0
        self.set_modes: list[str] = []
        self.set_mode = "store"
        self.get_mode = "ok"
        self.scan_mode = "ok"
        self.unlink_mode = "ok"
        self.delete_mode = "ok"
        self.eval_mode = "ok"
        self.set_started = asyncio.Event()
        self.set_release = asyncio.Event()
        self.unlink_started = asyncio.Event()
        self.unlink_release = asyncio.Event()
        self.events: list[str] = []
        self.unlink_batches: list[tuple[str, ...]] = []
        self._scan_snapshot: list[str] | None = None
        self._scan_cursor = 0
        self.observer: Any = None

    def query_values(self) -> dict[str, str]:
        return {
            key: value
            for key, value in self.values.items()
            if key.startswith("atlasrag:query:")
        }

    def seed(self, mapping: dict[str, str]) -> None:
        self.values.update(mapping)

    def _notify(self, name: str) -> None:
        self.events.append(name)
        if self.observer is not None:
            self.observer(name)

    async def get(self, key: str) -> str | None:
        if key == QUERY_WRITE_FENCE_KEY:
            self.fence_get_calls += 1
            self._notify("fence_get")
            if self.get_mode == "unavailable":
                raise RedisConnectionError("redis unavailable")
            if self.get_mode == "timeout":
                raise RedisTimeoutError("redis get timed out")
            return self.values.get(key)
        self.get_calls += 1
        self._notify("get")
        if self.get_mode == "unavailable":
            raise RedisConnectionError("redis unavailable")
        if self.get_mode == "timeout":
            raise RedisTimeoutError("redis get timed out")
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> object:
        if key == QUERY_WRITE_FENCE_KEY:
            self.fence_set_calls += 1
            self._notify("fence_set")
            if self.set_mode in {"unavailable_before", "unavailable"}:
                raise RedisConnectionError("connect failed")
            if self.set_mode == "timeout":
                raise RedisTimeoutError("redis set timed out")
            if self.set_mode == "reject":
                raise RedisResponseError("ERR rejected")
            self.values[key] = value
            return True
        self.set_calls += 1
        self._notify("set")
        mode = self.set_modes.pop(0) if self.set_modes else self.set_mode
        if mode == "store":
            self.values[key] = value
            return True
        if mode == "reject":
            raise RedisResponseError("ERR rejected")
        if mode in {"unavailable_before", "unavailable"}:
            raise RedisConnectionError("connect failed")
        if mode == "timeout":
            raise RedisTimeoutError("redis set timed out")
        if mode == "apply_timeout":
            self.values[key] = value
            raise RedisTimeoutError("ack lost")
        if mode == "apply_connection":
            self.values[key] = value
            raise RedisConnectionError("connection lost")
        if mode == "apply_bad_ack":
            self.values[key] = value
            return None
        if mode in {"delay", "apply_delay"}:
            if mode == "apply_delay":
                self.values[key] = value
            self.set_started.set()
            await self.set_release.wait()
            if mode == "delay":
                self.values[key] = value
            return True
        raise AssertionError(f"unknown set mode {mode}")

    async def scan(
        self, cursor: int = 0, match: str | None = None, count: int | None = None
    ) -> object:
        self.scan_calls += 1
        self._notify("scan")
        if self.scan_mode == "unavailable":
            raise RedisConnectionError("redis unavailable")
        if self.scan_mode == "timeout":
            raise RedisTimeoutError("redis scan timed out")
        if self.scan_mode == "malformed":
            return "not-a-page"
        if self.scan_mode == "loop":
            return 1, []
        if self._scan_snapshot is None:
            self._scan_snapshot = sorted(
                key for key in self.values if fnmatch.fnmatchcase(key, match or "*")
            )
            self._scan_cursor = 0
        start = self._scan_cursor
        page = self._scan_snapshot[start : start + self.page_size]
        self._scan_cursor = start + self.page_size
        if self._scan_cursor >= len(self._scan_snapshot):
            self._scan_snapshot = None
            self._scan_cursor = 0
        keys = page
        if self.scan_mode == "invalid_key":
            keys = [*page, "atlasrag:parent:rev:parent"]
        if self.scan_mode == "invalid_namespace":
            keys = ["application:unrelated"]
        next_cursor = 0 if self._scan_snapshot is None else self._scan_cursor
        return next_cursor, [key.encode() for key in keys]

    async def unlink(self, *keys: str) -> int:
        self.unlink_calls += 1
        self._notify("unlink")
        self.unlink_batches.append(tuple(keys))
        if self.unlink_mode == "delay":
            self.unlink_started.set()
            await self.unlink_release.wait()
        if self.unlink_mode == "unavailable":
            raise RedisConnectionError("redis unavailable")
        if self.unlink_mode == "timeout":
            raise RedisTimeoutError("redis unlink timed out")
        if self.unlink_mode == "partial":
            for key in keys[:1]:
                self.values.pop(key, None)
            raise RedisConnectionError("partial unlink")
        if self.unlink_mode == "bad_count":
            return len(keys) + 1
        removed = 0
        for key in keys:
            if key in self.values:
                removed += 1
                self.values.pop(key)
        return removed

    async def delete(self, *keys: str) -> int:
        if keys == (QUERY_WRITE_FENCE_KEY,):
            self.fence_delete_calls += 1
            self._notify("fence_delete")
        else:
            self.delete_calls += 1
            self._notify("delete")
        if self.delete_mode == "unavailable" or (
            self.get_mode == "unavailable" and self.scan_mode == "unavailable"
        ):
            raise RedisConnectionError("redis unavailable")
        if self.delete_mode == "timeout":
            raise RedisTimeoutError("redis delete timed out")
        removed = 0
        for key in keys:
            if key in self.values:
                removed += 1
                self.values.pop(key)
        return removed

    async def ping(self) -> bool:
        self.ping_calls += 1
        if self.get_mode == "unavailable":
            raise RedisConnectionError("redis unavailable")
        if self.get_mode == "timeout":
            raise RedisTimeoutError("redis ping timed out")
        return True

    async def eval(self, script: str, numkeys: int, *args: object) -> object:
        self.eval_calls += 1
        if self.eval_mode == "unavailable":
            raise RedisConnectionError("redis unavailable")
        if self.eval_mode == "timeout":
            raise RedisTimeoutError("redis eval timed out")
        if self.eval_mode == "bad_ack":
            return None
        if self.eval_mode == "malformed":
            return 123
        if script == _GET_FENCED_QUERY_SCRIPT and numkeys == 2 and len(args) == 2:
            query_key = str(args[0])
            fence_key = str(args[1])
            self.get_calls += 1
            self._notify("get")
            if self.get_mode == "unavailable":
                raise RedisConnectionError("redis unavailable")
            if self.get_mode == "timeout":
                raise RedisTimeoutError("redis get timed out")
            return [self.values.get(fence_key), self.values.get(query_key)]
        if (
            script == _ENSURE_QUERY_WRITE_FENCE_SCRIPT
            and numkeys == 1
            and len(args) == 2
        ):
            fence_key = str(args[0])
            candidate = str(args[1])
            self.fence_get_calls += 1
            self._notify("fence_get")
            if self.get_mode == "unavailable":
                raise RedisConnectionError("redis unavailable")
            if self.get_mode == "timeout":
                raise RedisTimeoutError("redis get timed out")
            current = self.values.get(fence_key)
            if current is not None:
                return current
            if self.set_mode in {"unavailable_before", "unavailable"}:
                raise RedisConnectionError("connect failed")
            if self.set_mode in {"timeout", "apply_timeout"}:
                raise RedisTimeoutError("redis set timed out")
            self.values[fence_key] = candidate
            self.fence_set_calls += 1
            self._notify("fence_set")
            return candidate
        if script in {_PUT_FENCED_QUERY_SCRIPT, _PUT_UNFENCED_IF_UNPROTECTED_SCRIPT}:
            query_key = str(args[0])
            fence_key = str(args[1])
            payload = str(args[2])
            expected_fence = (
                str(args[4]) if script == _PUT_FENCED_QUERY_SCRIPT else None
            )

            def _allowed() -> bool:
                if expected_fence is not None:
                    return self.values.get(fence_key) == expected_fence
                return fence_key not in self.values

            self.set_calls += 1
            self._notify("set")
            mode = self.set_modes.pop(0) if self.set_modes else self.set_mode
            if mode == "reject":
                raise RedisResponseError("ERR rejected")
            if mode in {"unavailable_before", "unavailable"}:
                raise RedisConnectionError("connect failed")
            if mode == "timeout":
                raise RedisTimeoutError("redis set timed out")
            if mode == "store":
                if not _allowed():
                    return 0
                self.values[query_key] = payload
                return 1
            if mode == "apply_timeout":
                if _allowed():
                    self.values[query_key] = payload
                raise RedisTimeoutError("ack lost")
            if mode == "apply_connection":
                if _allowed():
                    self.values[query_key] = payload
                raise RedisConnectionError("connection lost")
            if mode == "apply_bad_ack":
                if _allowed():
                    self.values[query_key] = payload
                return None
            if mode in {"delay", "apply_delay"}:
                if mode == "apply_delay" and _allowed():
                    self.values[query_key] = payload
                self.set_started.set()
                await self.set_release.wait()
                allowed_now = _allowed()
                if mode == "delay":
                    if not allowed_now:
                        return 0
                    self.values[query_key] = payload
                    return 1
                return 1 if allowed_now else 0
            raise AssertionError(f"unknown set mode {mode}")
        raise AssertionError(f"unexpected eval script with {numkeys} keys")


class DelayedRemoteRawRedis(FakeRawRedis):
    """Queue write commands after client failure so delayed remote execution runs later."""

    def __init__(self, *, page_size: int = 100) -> None:
        super().__init__(page_size=page_size)
        self.delay_remote_writes = True
        self.remote_failure_mode = "timeout"
        self.pending_remote_writes: list[tuple[str, int, tuple[object, ...]]] = []
        self.flushed_results: list[object] = []

    async def eval(self, script: str, numkeys: int, *args: object) -> object:
        if self.delay_remote_writes and script in {
            _PUT_FENCED_QUERY_SCRIPT,
            _PUT_UNFENCED_IF_UNPROTECTED_SCRIPT,
        }:
            self.eval_calls += 1
            self.set_calls += 1
            self._notify("remote_write_queued")
            self.pending_remote_writes.append((script, numkeys, args))
            if self.remote_failure_mode == "timeout":
                raise RedisTimeoutError("ack lost while command in transit")
            if self.remote_failure_mode == "connection":
                raise RedisConnectionError("connection lost while command in transit")
            if self.remote_failure_mode == "bad_ack":
                return None
            raise AssertionError(
                f"unknown remote_failure_mode {self.remote_failure_mode}"
            )
        return await super().eval(script, numkeys, *args)

    async def flush_pending_remote_writes(self) -> list[object]:
        queued = list(self.pending_remote_writes)
        self.pending_remote_writes.clear()
        results: list[object] = []
        previous = self.delay_remote_writes
        self.delay_remote_writes = False
        try:
            for script, numkeys, args in queued:
                result = await super().eval(script, numkeys, *args)
                results.append(result)
                self.flushed_results.append(result)
        finally:
            self.delay_remote_writes = previous
        return results


@dataclass
class MarkerStore:
    required: bool = False


class MarkerRepo:
    def __init__(self, unit: MarkerUnit) -> None:
        self._unit = unit

    async def get(self) -> RuntimeMetadata:
        factory = self._unit.factory
        factory.read_calls += 1
        factory.events.append("marker_read")
        error = factory.next_read_error()
        if error is not None:
            raise error
        return RuntimeMetadata(query_cache_invalidation_required=self._unit.working)

    async def set_query_cache_invalidation_required(
        self, required: bool
    ) -> RuntimeMetadata:
        factory = self._unit.factory
        factory.write_calls += 1
        factory.events.append(("marker_set", required))
        if factory.set_error is not None:
            raise factory.set_error
        self._unit.working = required
        return RuntimeMetadata(query_cache_invalidation_required=required)


class MarkerUnit:
    def __init__(self, factory: MarkerFactory) -> None:
        self.factory = factory
        self.working = factory.store.required
        self.runtime_metadata = MarkerRepo(self)
        self.applied = False
        self._entered = False

    async def __aenter__(self) -> MarkerUnit:
        self.factory.events.append("unit_enter")
        if self.factory.enter_error is not None:
            raise self.factory.enter_error
        if self.factory.enter_gate is not None:
            self.factory.enter_started.set()
            await self.factory.enter_gate.wait()
        self._entered = True
        return self

    async def __aexit__(self, *args: object) -> None:
        if not self.applied:
            self.factory.rollback_calls += 1

    async def commit(self) -> None:
        factory = self.factory
        factory.commit_calls += 1
        factory.events.append("commit")
        mode = factory.commit_mode
        if mode == "raise_before":
            raise DependencyUnavailableError()
        if mode == "canonical":
            raise CanonicalDataError()
        if mode == "noop":
            self.applied = True
            return
        factory.store.required = self.working
        self.applied = True
        if mode == "apply_then_raise":
            raise DependencyUnavailableError()
        if mode == "apply_then_timeout":
            raise DependencyTimeoutError()


class MarkerFactory:
    def __init__(self, *, required: bool = False) -> None:
        self.store = MarkerStore(required=required)
        self.read_calls = 0
        self.write_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.events: list[object] = []
        self.enter_error: BaseException | None = None
        self.enter_gate: asyncio.Event | None = None
        self.enter_started = asyncio.Event()
        self.read_error: BaseException | None = None
        self.read_errors: list[BaseException | None] = []
        self.set_error: BaseException | None = None
        self.commit_mode = "ok"

    def __call__(self) -> MarkerUnit:
        return MarkerUnit(self)

    def next_read_error(self) -> BaseException | None:
        if self.read_errors:
            return self.read_errors.pop(0)
        return self.read_error

    @property
    def required(self) -> bool:
        return self.store.required

    @required.setter
    def required(self, value: bool) -> None:
        self.store.required = value


@dataclass
class CacheRig:
    raw: FakeRawRedis
    provider: RedisCacheProvider
    marker: MarkerFactory
    cache: SessionQueryCache
    barrier: CacheFreshnessBarrier
    enabled: bool = True
    max_entry_bytes: int = 262_144
    retry_interval: float = 10.0
    runtime_fingerprint: str = "runtime"
    ttl_seconds: int = 60
    require_write_fence: bool = False


def build_cache_rig(
    *,
    marker_required: bool = False,
    enabled: bool = True,
    retry_interval: float = 10.0,
    max_entry_bytes: int = 262_144,
    page_size: int = 100,
    scan_batch_size: int = 100,
    ttl_seconds: int = 60,
    runtime_fingerprint: str = "runtime",
    require_write_fence: bool = False,
    raw: FakeRawRedis | None = None,
) -> CacheRig:
    raw = raw if raw is not None else FakeRawRedis(page_size=page_size)
    provider = RedisCacheProvider(raw, scan_batch_size=scan_batch_size)  # type: ignore[arg-type]
    marker = MarkerFactory(required=marker_required)
    barrier = CacheFreshnessBarrier(
        redis=provider,
        uow_factory=marker,  # type: ignore[arg-type]
        retry_interval_seconds=retry_interval,
    )
    cache = SessionQueryCache(
        redis=provider,
        uow_factory=marker,  # type: ignore[arg-type]
        runtime_fingerprint=runtime_fingerprint,
        ttl_seconds=ttl_seconds,
        policy=QueryCachePolicy(enabled=enabled, max_entry_bytes=max_entry_bytes),
        clock=lambda: NOW,
        require_write_fence=require_write_fence,
        expected_write_fence=(
            (lambda: barrier.active_write_fence) if require_write_fence else None
        ),
    )
    return CacheRig(
        raw=raw,
        provider=provider,
        marker=marker,
        cache=cache,
        barrier=barrier,
        enabled=enabled,
        max_entry_bytes=max_entry_bytes,
        retry_interval=retry_interval,
        runtime_fingerprint=runtime_fingerprint,
        ttl_seconds=ttl_seconds,
        require_write_fence=require_write_fence,
    )


def rebuild_rig(rig: CacheRig, *, require_write_fence: bool | None = None) -> CacheRig:
    """Fresh process-local cache/barrier over the same durable fake state."""

    fenced = (
        rig.require_write_fence if require_write_fence is None else require_write_fence
    )
    barrier = CacheFreshnessBarrier(
        redis=rig.provider,
        uow_factory=rig.marker,  # type: ignore[arg-type]
        retry_interval_seconds=rig.retry_interval,
    )
    cache = SessionQueryCache(
        redis=rig.provider,
        uow_factory=rig.marker,  # type: ignore[arg-type]
        runtime_fingerprint=rig.runtime_fingerprint,
        ttl_seconds=rig.ttl_seconds,
        policy=QueryCachePolicy(
            enabled=rig.enabled, max_entry_bytes=rig.max_entry_bytes
        ),
        clock=lambda: NOW,
        require_write_fence=fenced,
        expected_write_fence=((lambda: barrier.active_write_fence) if fenced else None),
    )
    return CacheRig(
        raw=rig.raw,
        provider=rig.provider,
        marker=rig.marker,
        cache=cache,
        barrier=barrier,
        enabled=rig.enabled,
        max_entry_bytes=rig.max_entry_bytes,
        retry_interval=rig.retry_interval,
        runtime_fingerprint=rig.runtime_fingerprint,
        ttl_seconds=rig.ttl_seconds,
        require_write_fence=fenced,
    )


def final_response(text: str = "Answer v1 [L1].") -> FinalResponse:
    return FinalResponse(
        answer_text=text,
        citations=(
            FinalLocalCitation(
                citation_id="L1",
                file_name="guide.md",
                relative_source_path="docs/guide.md",
                evidence_type=EvidenceType.TEXT_PARENT,
            ),
        ),
    )


def core_result(
    *,
    text: str = "Answer v1 [L1].",
    status: RagCompletionStatus = RagCompletionStatus.SUCCESS,
    cited: bool = True,
) -> RagCoreResult:
    response = final_response(text)
    if not cited:
        response = FinalResponse(answer_text=text, citations=())
    return RagCoreResult(final_response=response, completion_status=status)


def cached_value(text: str = "stale [L1].") -> CachedQueryResult:
    return CachedQueryResult(created_at=NOW, final_response=final_response(text))


class Knowledge:
    def __init__(self) -> None:
        self.version = 1
        self.evidence = local_evidence()

    @property
    def text(self) -> str:
        return f"Answer v{self.version} [L1]."

    def advance(self) -> None:
        self.version += 1


class FakeQuery:
    async def construct(self, value: UserTurnRequest) -> QueryConstructionResult:
        return QueryConstructionResult(
            request=LocalRetrievalRequest(
                original_query=value.query,
                retrieval_query=value.query,
                filters=value.filters,
            ),
            attempts=1,
            used_fallback=False,
        )


class FakeLocal:
    def __init__(self, knowledge: Knowledge) -> None:
        self._knowledge = knowledge
        self.calls = 0

    async def run(self, value: LocalRetrievalRequest) -> LocalEvidenceResult:
        self.calls += 1
        return LocalEvidenceResult(
            execution_status=LocalExecutionStatus.OK,
            evidence_status=LocalEvidenceStatus.SUFFICIENT,
            selected_evidence=(self._knowledge.evidence,),
        )


class FakeWeb:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, value: object) -> WebEvidenceResult:
        self.calls += 1
        return WebEvidenceResult(execution_status=WebExecutionStatus.EMPTY, evidence=())


class FakeAnswer:
    def __init__(self, knowledge: Knowledge) -> None:
        self._knowledge = knowledge
        self.calls = 0

    async def run(
        self, value: AnswerInput
    ) -> tuple[FinalResponse, AnswerExecutionOutcome]:
        self.calls += 1
        assert value.local_evidence
        evidence = value.local_evidence[0].evidence
        return (
            FinalResponse(
                answer_text=self._knowledge.text,
                citations=(
                    FinalLocalCitation(
                        citation_id="L1",
                        file_name=evidence.provenance.file_name,
                        relative_source_path=(evidence.provenance.relative_source_path),
                        evidence_type=EvidenceType(evidence.evidence_type),
                    ),
                ),
            ),
            AnswerExecutionOutcome.SUCCESS,
        )


def build_rag_core(knowledge: Knowledge) -> RagCoreRunner:
    return RagCoreRunner(
        RagCoreServices(
            query_construction=FakeQuery(),  # type: ignore[arg-type]
            local_evidence=FakeLocal(knowledge),  # type: ignore[arg-type]
            routing=CorrectiveRoutingService(max_retries=1),
            web_evidence=FakeWeb(),  # type: ignore[arg-type]
            answer_input=AnswerInputBuilder(),
            answer=FakeAnswer(knowledge),  # type: ignore[arg-type]
            completion=CompletionAssessor(),
        )
    )


class ControlledSync:
    def __init__(self, rig: CacheRig, knowledge: Knowledge) -> None:
        self.rig = rig
        self.knowledge = knowledge
        self.calls = 0
        self.mode = "safe"
        self.gate: asyncio.Event | None = None
        self.reached = asyncio.Event()

    async def __call__(self) -> SyncResult:
        self.calls += 1
        if self.gate is not None:
            self.reached.set()
            await self.gate.wait()
        if self.mode == "raise":
            raise RuntimeError("maintenance failed")
        if self.mode == "unsafe":
            await self._set_marker()
            return UNSAFE
        await self._set_marker()
        self.knowledge.advance()
        return SAFE

    async def _set_marker(self) -> None:
        async with self.rig.marker() as unit:
            await unit.runtime_metadata.set_query_cache_invalidation_required(True)
            await unit.commit()


@dataclass
class CacheRuntime:
    rig: CacheRig
    knowledge: Knowledge
    runner: RetrievalRunner
    sync: ControlledSync
    gate: RuntimeGate
    tasks: InFlightTaskRegistry
    health: HealthRegistry
    coordinator: ApplicationCoordinator
    ownership: RecordingOwnership
    close: CloseTracker
    order: list[str] = field(default_factory=list)

    @property
    def cache(self) -> SessionQueryCache:
        return self.rig.cache

    @property
    def barrier(self) -> CacheFreshnessBarrier:
        return self.rig.barrier

    @property
    def raw(self) -> FakeRawRedis:
        return self.rig.raw

    @property
    def marker(self) -> MarkerFactory:
        return self.rig.marker

    async def start(self) -> None:
        await self.coordinator._start()

    async def query(self, value: UserTurnRequest) -> QueryResponseEnvelope:
        return await self.coordinator.run_query(value)

    async def maintenance(self) -> SyncResult:
        return await self.coordinator.run_maintenance()

    async def shutdown(self) -> None:
        await self.coordinator.shutdown()


def _noop() -> None:
    return None


def build_runtime(
    *,
    rig: CacheRig,
    knowledge: Knowledge,
    drain_timeout: float = 1.0,
    shutdown_timeout: float = 1.0,
) -> CacheRuntime:
    gate = RuntimeGate()
    tasks = InFlightTaskRegistry()
    health = build_health(gate, refresh_interval=60.0)
    order: list[str] = []
    sync = ControlledSync(rig, knowledge)
    maintenance = MaintenanceCoordinator(
        gate=gate,
        run_sync=sync,
        freshness=rig.barrier,
        drain_timeout_seconds=drain_timeout,
    )
    runner = RetrievalRunner(
        RetrievalGraphServices(cache=rig.cache, rag_core=build_rag_core(knowledge))
    )
    ownership = RecordingOwnership(order)
    close = CloseTracker(order)

    async def reconcile() -> ReconciliationResult:
        order.append("reconcile")
        return ReconciliationResult(
            outcome=ReconciliationOutcome.CLEAN, checked_documents=0
        )

    async def source_safety() -> SourceSafetyResult:
        order.append("source_safety")
        return SourceSafetyResult(safe=True)

    async def verify() -> GlobalSafetyResult:
        order.append("verify")
        return GlobalSafetyResult(serving_safety=ServingSafety.SAFE_TO_SERVE)

    deps = CoordinatorDependencies(
        ownership=ownership,  # type: ignore[arg-type]
        gate=gate,
        tasks=tasks,
        health=health,
        freshness=rig.barrier,
        maintenance=maintenance,
        retrieval=runner.run,
        reconcile=reconcile,
        source_safety=source_safety,
        verify=verify,
        close_resources=close,
        shutdown_timeout_seconds=shutdown_timeout,
        cache_recovery_inside_lease=True,
        ownership_acquired=False,
    )
    return CacheRuntime(
        rig=rig,
        knowledge=knowledge,
        runner=runner,
        sync=sync,
        gate=gate,
        tasks=tasks,
        health=health,
        coordinator=ApplicationCoordinator(deps),
        ownership=ownership,
        close=close,
        order=order,
    )


async def prepare(
    rig: CacheRig,
    value: UserTurnRequest | None = None,
    *,
    result: RagCoreResult | None = None,
    web_invoked: bool = False,
) -> PreparedCacheWrite | None:
    return await rig.cache.prepare(
        value if value is not None else request(),
        result if result is not None else core_result(),
        web_invoked=web_invoked,
    )


__all__ = [
    "CacheRig",
    "CacheRuntime",
    "ControlledSync",
    "DelayedRemoteRawRedis",
    "FakeClock",
    "FakeRawRedis",
    "Knowledge",
    "MarkerFactory",
    "NOW",
    "SAFE",
    "UNSAFE",
    "build_cache_rig",
    "build_rag_core",
    "build_runtime",
    "cached_value",
    "core_result",
    "final_response",
    "prepare",
    "rebuild_rig",
    "request",
]
