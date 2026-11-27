"""WP-C tests for InvocationEventBus, StreamChannel, SseStreamResponse, and /api/v1/query/stream."""

from __future__ import annotations

import asyncio
import json
import threading
import warnings
from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from starlette.types import Message, Scope

from atlasrag.application.rag_core import RagCoreExecution
from atlasrag.application.retrieval import RetrievalRunner
from atlasrag.domain.enums import (
    EvidenceType,
    RagCompletionStatus,
)
from atlasrag.domain.errors import (
    DependencyTimeoutError,
    RuntimeShutdownFailedError,
)
from atlasrag.domain.observation import (
    ObservationStage,
    ObservedCandidate,
    ProductionObservation,
    ProductionObserver,
    current_invocation_id,
    deliver_observation,
)
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import (
    FinalLocalCitation,
    FinalResponse,
    RagCoreResult,
)
from atlasrag.graphs.retrieval.graph import RetrievalGraphServices
from atlasrag.providers.cache.keys import query_cache_key
from atlasrag.providers.cache.models import CachedQueryResult
from atlasrag.runtime.composition import (
    ApplicationComposition,
    BusinessBundle,
    SharedResources,
)
from atlasrag.runtime.resources import ResourceGuard
from atlasrag.runtime.tasks import TaskKind
from atlasrag.server.app import create_app
from atlasrag.server.config import ServerConfig
from atlasrag.server.events import InvocationEventBus, StreamChannel
from atlasrag.server.schemas import ClientKind
from atlasrag.server.service import ApplicationServerService
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.cache.session import QueryCacheStatus, SessionQueryCache
from atlasrag.services.sync.models import (
    GlobalSafetyResult,
    ReconciliationOutcome,
    ReconciliationResult,
    ServingSafety,
    SyncOutcome,
    SyncResult,
)
from atlasrag.services.sync.source_safety import SourceSafetyResult
from stage11.conftest import (
    FakeRuntimeMetadataRepo,
    FakeSyncUow,
    _isolated_ownership_lock,
    sample_response,
    sample_runtime_config,
    start_test_coordinator,
)


def _local_headers(client_kind: str = "web") -> dict[str, str]:
    return {
        "host": "127.0.0.1:8000",
        "origin": "http://127.0.0.1:8000",
        "x-atlasrag-client": client_kind,
    }


def _parse_sse_events(raw_bytes: bytes) -> list[tuple[str | None, str]]:
    """Parse wire SSE chunks into (event_name_or_comment, data_text) tuples."""

    text = raw_bytes.decode("utf-8")
    parsed: list[tuple[str | None, str]] = []
    for block in text.split("\n\n"):
        if not block:
            continue
        lines = block.split("\n")
        if lines == [": heartbeat"]:
            parsed.append(("comment", "heartbeat"))
            continue
        event_name: str | None = None
        data_lines: list[str] = []
        for line in lines:
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: ") :])
        parsed.append((event_name, "\n".join(data_lines)))
    return parsed


class _FakeRedisQueryCache:
    """In-memory fake Redis matching SessionQueryCache's Stage 8 protocol."""

    def __init__(self) -> None:
        self.entries: dict[str, CachedQueryResult] = {}
        self.write_fence = "fence-1"

    async def get_query_write_fence(self) -> str | None:
        return self.write_fence

    async def get_fenced_query(
        self,
        session_id: str,
        runtime_fingerprint: str,
        request_hash_val: str,
    ) -> tuple[str | None, CachedQueryResult | None]:
        return (
            self.write_fence,
            self.entries.get(
                query_cache_key(session_id, runtime_fingerprint, request_hash_val)
            ),
        )

    async def put_fenced_query(
        self,
        session_id: str,
        runtime_fingerprint: str,
        request_hash_val: str,
        value: CachedQueryResult,
        *,
        ttl_seconds: int,
        write_fence: str,
    ) -> bool:
        del ttl_seconds
        if write_fence != self.write_fence:
            return False
        self.entries[
            query_cache_key(session_id, runtime_fingerprint, request_hash_val)
        ] = value
        return True


def _sample_final_with_citation(text: str = "Cited answer [L1].") -> FinalResponse:
    return FinalResponse(
        answer_text=text,
        citations=(
            FinalLocalCitation(
                citation_id="L1",
                file_name="manual.md",
                relative_source_path="docs/manual.md",
                evidence_type=EvidenceType.TEXT_PARENT,
            ),
        ),
    )


def _make_http_scope(
    path: str = "/api/v1/query/stream",
    *,
    method: str = "POST",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Scope:
    default_headers = [
        (b"host", b"127.0.0.1:8000"),
        (b"origin", b"http://127.0.0.1:8000"),
        (b"x-atlasrag-client", b"web"),
        (b"content-type", b"application/json"),
    ]
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": default_headers if headers is None else headers,
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8000),
    }


@asynccontextmanager
async def _open_client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client,
    ):
        yield client


async def _create_session_async(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        "/api/v1/sessions",
        json={"client_kind": "web"},
        headers=_local_headers("web"),
    )
    assert resp.status_code == 201
    return cast(str, resp.json()["session_id"])


@pytest.mark.asyncio
async def test_sse_pre_admission_rejections_return_json_never_sse() -> None:
    """Pre-admission rejections (session, readiness, timeout, shutdown) return JSON, not 200 SSE."""

    coordinator, harness = await start_test_coordinator()
    bus = InvocationEventBus()
    app = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        coordinator=coordinator,
        event_bus=bus,
    )
    try:
        async with _open_client(app) as client:
            # Missing session -> 404 JSON
            missing = await client.post(
                "/api/v1/query/stream",
                json={"session_id": "web_missing", "query": "hello"},
                headers=_local_headers("web"),
            )
            assert missing.status_code == 404
            assert missing.headers["content-type"].startswith("application/json")
            assert missing.json()["error_code"] == "SESSION_NOT_FOUND"

            sid = await _create_session_async(client)

            # BLOCKED -> 503 RUNTIME_NOT_READY JSON
            await harness.gate.mark_blocked()
            blocked = await client.post(
                "/api/v1/query/stream",
                json={"session_id": sid, "query": "hello"},
                headers=_local_headers("web"),
            )
            assert blocked.status_code == 503
            assert blocked.headers["content-type"].startswith("application/json")
            assert blocked.json()["error_code"] == "RUNTIME_NOT_READY"

            # SHUTTING_DOWN -> 503 RUNTIME_SHUTTING_DOWN JSON
            await harness.gate.begin_shutdown()
            shut = await client.post(
                "/api/v1/query/stream",
                json={"session_id": sid, "query": "hello"},
                headers=_local_headers("web"),
            )
            assert shut.status_code == 503
            assert shut.headers["content-type"].startswith("application/json")
            assert shut.json()["error_code"] == "RUNTIME_SHUTTING_DOWN"
            assert bus.bound_count() == 0
    finally:
        await coordinator.shutdown()

    # Probe failure during pre-admission health check -> 503/504 JSON, NO 200 headers
    fail_probe = {"fail": False}

    async def dynamic_pg() -> None:
        if fail_probe["fail"]:
            raise DependencyTimeoutError

    bad_coord, _ = await start_test_coordinator(
        probes={"postgres": dynamic_pg},
        refresh_interval=0.0,
    )
    app2 = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        coordinator=bad_coord,
    )
    try:
        async with _open_client(app2) as client:
            sid2 = await _create_session_async(client)
            fail_probe["fail"] = True
            timeout_resp = await client.post(
                "/api/v1/query/stream",
                json={"session_id": sid2, "query": "timeout check"},
                headers=_local_headers("web"),
            )
            assert timeout_resp.status_code == 503
            assert timeout_resp.headers["content-type"].startswith("application/json")
    finally:
        await bad_coord.shutdown()


@pytest.mark.asyncio
async def test_sse_headers_commit_only_after_query_lease_and_accepted_is_first() -> (
    None
):
    """HTTP 200 headers wait for real QueryLease admission; accepted is always frame #1."""

    bus = InvocationEventBus()
    observed_timeline: list[str] = []
    captured_trace_ids: list[str] = []
    retrieval_enter = asyncio.Event()

    async def retrieval(req: UserTurnRequest) -> FinalResponse:
        del req
        trace_id = current_invocation_id()
        assert trace_id is not None
        captured_trace_ids.append(trace_id)
        observed_timeline.append("retrieval_enter")
        retrieval_enter.set()
        deliver_observation(
            bus,
            ProductionObservation(
                stage=ObservationStage.DENSE,
                status="OK",
                candidates=(
                    ObservedCandidate(identity="secret-chunk-1", rank=1, score=0.9),
                ),
                selected_identities=("secret-ctx-1",),
                invocation_id=trace_id,
                attempt_index=1,
                degraded=False,
            ),
        )
        deliver_observation(
            bus,
            ProductionObservation(
                stage=ObservationStage.ANSWER,
                status="SUCCESS",
                invocation_id=trace_id,
                attempt_index=1,
            ),
        )
        return _sample_final_with_citation("Streamed answer [L1].")

    coordinator, harness = await start_test_coordinator(retrieval=retrieval)
    real_acquire = harness.gate.acquire_query

    async def tracked_acquire() -> Any:
        lease = await real_acquire()
        observed_timeline.append("lease_acquired")
        return lease

    harness.gate.acquire_query = tracked_acquire  # type: ignore[method-assign]
    app = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        coordinator=coordinator,
        event_bus=bus,
    )
    try:
        async with _open_client(app) as client:
            sid = await _create_session_async(client)
            async with client.stream(
                "POST",
                "/api/v1/query/stream",
                json={"session_id": sid, "query": "What is AtlasRAG?"},
                headers=_local_headers("web"),
            ) as response:
                observed_timeline.append("http_200_received")
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                raw = await response.aread()
        assert observed_timeline[0] == "lease_acquired"
        events = _parse_sse_events(raw)
        assert [name for name, _ in events] == [
            "accepted",
            "progress",
            "progress",
            "final",
        ]
        trace_id = captured_trace_ids[0]
        accepted_data = json.loads(events[0][1])
        assert accepted_data == {"trace_id": trace_id, "session_id": sid}
        dense_data = json.loads(events[1][1])
        assert dense_data == {
            "trace_id": trace_id,
            "stage": "DENSE",
            "status": "OK",
            "attempt": 1,
            "degraded": False,
        }
        assert "secret-chunk-1" not in events[1][1]
        assert "secret-ctx-1" not in events[1][1]
        final_data = json.loads(events[3][1])
        assert final_data["trace_id"] == trace_id
        assert final_data["session_id"] == sid
        assert final_data["response"]["answer_text"] == "Streamed answer [L1]."
        assert bus.bound_count() == 0
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_sse_immediate_completion_and_post_admission_error_before_observation() -> (
    None
):
    """Both immediate completion and post-admission failure before any observation retain accepted + terminal."""

    bus = InvocationEventBus()

    async def instant_ok(_req: UserTurnRequest) -> FinalResponse:
        return sample_response("Instant completion.")

    coord_ok, _ = await start_test_coordinator(retrieval=instant_ok)
    app_ok = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        coordinator=coord_ok,
        event_bus=bus,
    )
    try:
        async with _open_client(app_ok) as client:
            sid = await _create_session_async(client)
            async with client.stream(
                "POST",
                "/api/v1/query/stream",
                json={"session_id": sid, "query": "instant"},
                headers=_local_headers("web"),
            ) as resp:
                assert resp.status_code == 200
                events = _parse_sse_events(await resp.aread())
                assert [name for name, _ in events] == ["accepted", "final"]
                accepted_trace = json.loads(events[0][1])["trace_id"]
                assert json.loads(events[1][1])["trace_id"] == accepted_trace
                assert bus.bound_count() == 0
    finally:
        await coord_ok.shutdown()

    async def fail_no_obs(_req: UserTurnRequest) -> FinalResponse:
        raise RuntimeError("secret postgresql://user:pass@postgres/db unavailable")

    coord_err, _ = await start_test_coordinator(retrieval=fail_no_obs)
    app_err = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        coordinator=coord_err,
        event_bus=bus,
    )
    try:
        async with _open_client(app_err) as client:
            sid = await _create_session_async(client)
            async with client.stream(
                "POST",
                "/api/v1/query/stream",
                json={"session_id": sid, "query": "fail"},
                headers=_local_headers("web"),
            ) as resp:
                assert resp.status_code == 200
                events = _parse_sse_events(await resp.aread())
                assert [name for name, _ in events] == ["accepted", "error"]
                accepted_trace = json.loads(events[0][1])["trace_id"]
                err_payload = json.loads(events[1][1])
                assert err_payload["trace_id"] == accepted_trace
                assert err_payload["error_code"] == "INTERNAL_ERROR"
                assert "postgres" not in err_payload["message"].lower()
                assert "pass" not in err_payload["message"].lower()
                assert bus.bound_count() == 0
    finally:
        await coord_err.shutdown()


@pytest.mark.asyncio
async def test_sse_concurrent_invocations_isolate_events_by_authoritative_trace() -> (
    None
):
    """Concurrent SSE queries never cross-deliver observations across channels."""

    bus = InvocationEventBus()
    both_started = asyncio.Event()
    proceed = asyncio.Event()
    seen_traces: dict[str, str] = {}

    async def staggered_retrieval(req: UserTurnRequest) -> FinalResponse:
        trace_id = current_invocation_id()
        assert trace_id is not None
        seen_traces[req.query] = trace_id
        if len(seen_traces) == 2:
            both_started.set()
        await both_started.wait()
        if req.query == "query-A":
            deliver_observation(
                bus,
                ProductionObservation(
                    stage=ObservationStage.DENSE,
                    status="OK",
                    invocation_id=trace_id,
                ),
            )
            await proceed.wait()
        else:
            deliver_observation(
                bus,
                ProductionObservation(
                    stage=ObservationStage.BM25,
                    status="EMPTY",
                    degraded=True,
                    invocation_id=trace_id,
                ),
            )
            proceed.set()
        return sample_response(f"answer:{req.query}")

    coordinator, _ = await start_test_coordinator(retrieval=staggered_retrieval)
    app = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        coordinator=coordinator,
        event_bus=bus,
    )
    try:
        async with _open_client(app) as client:
            sid_a = await _create_session_async(client)
            sid_b = await _create_session_async(client)

            async def run_one(sid: str, q: str) -> list[tuple[str | None, str]]:
                async with client.stream(
                    "POST",
                    "/api/v1/query/stream",
                    json={"session_id": sid, "query": q},
                    headers=_local_headers("web"),
                ) as resp:
                    assert resp.status_code == 200
                    return _parse_sse_events(await resp.aread())

            ev_a, ev_b = await asyncio.gather(
                run_one(sid_a, "query-A"),
                run_one(sid_b, "query-B"),
            )
        trace_a = seen_traces["query-A"]
        trace_b = seen_traces["query-B"]
        assert trace_a != trace_b
        assert [json.loads(d)["stage"] for n, d in ev_a if n == "progress"] == ["DENSE"]
        assert [json.loads(d)["stage"] for n, d in ev_b if n == "progress"] == ["BM25"]
        for name, data in ev_a:
            if name in {"accepted", "progress", "final"}:
                assert json.loads(data)["trace_id"] == trace_a
        for name, data in ev_b:
            if name in {"accepted", "progress", "final"}:
                assert json.loads(data)["trace_id"] == trace_b
        assert bus.bound_count() == 0
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_production_composition_wires_invocation_event_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_app production lifespan passes InvocationEventBus to ApplicationComposition.start."""

    observed_wiring: dict[str, ProductionObserver | None] = {"observer": None}
    coordinator, _ = await start_test_coordinator()

    async def fake_comp_start(
        self: ApplicationComposition,
        build_business: Any = None,
        *,
        observer: ProductionObserver | None = None,
        on_shutdown_settled: Any = None,
    ) -> Any:
        del self, build_business, on_shutdown_settled
        observed_wiring["observer"] = observer
        return coordinator

    monkeypatch.setattr(ApplicationComposition, "start", fake_comp_start)
    bus = InvocationEventBus()
    app = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        event_bus=bus,
    )
    async with app.router.lifespan_context(app):
        assert observed_wiring["observer"] is bus
        assert app.state.event_bus is bus
    monkeypatch.undo()
    await coordinator.shutdown()

    # Also verify SharedResources / ApplicationComposition forwarding contract
    comp = ApplicationComposition(sample_runtime_config())
    comp._ownership = _isolated_ownership_lock()  # noqa: SLF001

    class _FakePool:
        async def open(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def health(self) -> None:
            return None

    class _FakeMilvus:
        embedding_dimension = sample_runtime_config().part_i.embedding_dimension
        analyzer = "standard"

        async def initialize(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def health(self) -> None:
            return None

    class _FakeRedis:
        async def aclose(self) -> None:
            return None

    meta_repo = FakeRuntimeMetadataRepo(invalidation_required=False)

    class _FakeRedisProv:
        def __init__(self, client: Any) -> None:
            del client

        async def health(self) -> None:
            return None

        async def invalidate_queries(self) -> int:
            return 0

        async def establish_query_write_fence(self) -> str:
            return "fence-1"

        async def ensure_query_write_fence(self) -> str:
            return "fence-1"

        async def revoke_query_write_fence(self) -> None:
            return None

    class _FakeQwen:
        def __init__(self, device: str) -> None:
            del device

    class _FakeGuardedEmb:
        def __init__(self, inner: Any, guard: Any) -> None:
            del inner, guard
            from atlasrag.domain.indexing.models import EmbeddingModelIdentity

            self.identity = EmbeddingModelIdentity(
                model_id=sample_runtime_config().models.embedding_model_id,
                model_revision=sample_runtime_config().models.embedding_model_revision,
                tokenizer_id=sample_runtime_config().models.embedding_tokenizer_id,
                tokenizer_revision=sample_runtime_config().models.embedding_tokenizer_revision,
                dimension=sample_runtime_config().part_i.embedding_dimension,
                device="cuda",
            )

        async def initialize(self, *, allow_download: bool = False) -> None:
            del allow_download

    class _FakeBge:
        def __init__(self, device: str) -> None:
            del device

    class _FakeGuardedRerank:
        def __init__(self, inner: Any, guard: Any) -> None:
            del inner, guard

        async def initialize(self, *, allow_download: bool = False) -> None:
            raise ConnectionError("optional")

    import atlasrag.runtime.composition as comp_mod

    monkeypatch.setattr(comp_mod, "PostgresPool", lambda *a, **kw: _FakePool())
    monkeypatch.setattr(
        comp_mod, "PostgresUnitOfWork", lambda _pool: FakeSyncUow(meta_repo)
    )
    monkeypatch.setattr("redis.asyncio.from_url", lambda *a, **kw: _FakeRedis())
    monkeypatch.setattr(comp_mod, "RedisCacheProvider", _FakeRedisProv)
    monkeypatch.setattr(comp_mod, "MilvusClient", lambda *a, **kw: object())
    monkeypatch.setattr(comp_mod, "MilvusIndexProvider", lambda *a, **kw: _FakeMilvus())
    monkeypatch.setattr(comp_mod, "QwenEmbeddingProvider", _FakeQwen)
    monkeypatch.setattr(comp_mod, "GuardedEmbeddingProvider", _FakeGuardedEmb)
    monkeypatch.setattr(comp_mod, "BgeRerankerProvider", _FakeBge)
    monkeypatch.setattr(comp_mod, "GuardedRerankerProvider", _FakeGuardedRerank)

    shared_observers: list[ProductionObserver] = []

    async def capture_business(shared: SharedResources) -> BusinessBundle:
        shared_observers.append(shared.observer)

        async def observed_retrieval(_r: UserTurnRequest) -> FinalResponse:
            deliver_observation(
                shared.observer,
                ProductionObservation(
                    stage=ObservationStage.DENSE,
                    status="OK",
                    invocation_id=current_invocation_id(),
                ),
            )
            return sample_response("Wired production observer.")

        return BusinessBundle(
            retrieval=observed_retrieval,
            sync=lambda: asyncio.sleep(
                0,
                result=SyncResult(
                    outcome=SyncOutcome.SUCCESS,
                    serving_safety=ServingSafety.SAFE_TO_SERVE,
                ),
            ),
            reconcile=lambda: asyncio.sleep(
                0, result=ReconciliationResult(outcome=ReconciliationOutcome.CLEAN)
            ),
            source_safety=lambda: asyncio.sleep(
                0, result=SourceSafetyResult(safe=True)
            ),
            verify=lambda: asyncio.sleep(
                0,
                result=GlobalSafetyResult(serving_safety=ServingSafety.SAFE_TO_SERVE),
            ),
            close=lambda: asyncio.sleep(0),
        )

    coord_comp = await comp.start(capture_business, observer=bus)
    app_comp = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        coordinator=coord_comp,
        event_bus=bus,
    )
    try:
        assert shared_observers == [bus]
        async with _open_client(app_comp) as client:
            sid = await _create_session_async(client)
            async with client.stream(
                "POST",
                "/api/v1/query/stream",
                json={"session_id": sid, "query": "prod wire"},
                headers=_local_headers("web"),
            ) as resp:
                events = _parse_sse_events(await resp.aread())
            assert [name for name, _ in events] == ["accepted", "progress", "final"]
            assert json.loads(events[1][1])["stage"] == "DENSE"
    finally:
        await coord_comp.shutdown()


@pytest.mark.asyncio
async def test_sse_projection_allowlist_bounds_overflow_and_thread_handoff() -> None:
    """Drop unallowlisted/oversized progress, evict oldest on overflow, retain terminal, handle worker thread."""

    channel = StreamChannel(
        session_id="web_sess1",
        server_config=ServerConfig(
            sse_queue_maxsize=16,
            max_progress_event_bytes=128,
        ),
    )
    channel.mark_admitted("trace-1")

    # Unallowlisted stage/status string must be suppressed completely
    channel.observe(
        ProductionObservation(
            stage=ObservationStage.DENSE,
            status="INTERNAL_EXCEPTION: /etc/passwd",
            invocation_id="trace-1",
        )
    )
    assert not channel.has_pending_progress()

    # Cross-invocation event rejected
    channel.observe(
        ProductionObservation(
            stage=ObservationStage.DENSE,
            status="OK",
            invocation_id="trace-OTHER",
        )
    )
    assert not channel.has_pending_progress()

    # Worker thread handoff safely delivers into loop queue
    thread_done = threading.Event()

    def worker() -> None:
        channel.observe(
            ProductionObservation(
                stage=ObservationStage.BM25,
                status="OK",
                invocation_id="trace-1",
            )
        )
        thread_done.set()

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=2.0)
    assert thread_done.is_set()
    await asyncio.sleep(0.02)
    assert channel.has_pending_progress()
    first = channel.pop_progress_nowait()
    assert first is not None and b'"stage":"BM25"' in first

    # Progress byte limit enforcement (trace_id too large -> progress > 128 bytes -> dropped safely)
    long_channel = StreamChannel(
        session_id="web_s",
        server_config=ServerConfig(max_progress_event_bytes=128, sse_queue_maxsize=16),
    )
    long_channel.mark_admitted("t" * 120)
    long_channel.observe(
        ProductionObservation(
            stage=ObservationStage.DENSE,
            status="OK",
            invocation_id="t" * 120,
        )
    )
    assert not long_channel.has_pending_progress()

    # Queue overflow drops ONLY oldest progress while preserving terminal final
    flood_channel = StreamChannel(
        session_id="web_s",
        server_config=ServerConfig(sse_queue_maxsize=16),
    )
    flood_channel.mark_admitted("trace-flood")
    for idx in range(25):
        flood_channel.observe(
            ProductionObservation(
                stage=ObservationStage.DENSE,
                status="OK",
                attempt_index=idx + 1,
                invocation_id="trace-flood",
            )
        )
    assert flood_channel.dropped_progress_count == 9
    from atlasrag.server.schemas import QueryResponse

    flood_channel.publish_final(
        QueryResponse(
            trace_id="trace-flood",
            session_id="web_s",
            cache_status=QueryCacheStatus.UNKNOWN,
            elapsed_ms=1.0,
            response=sample_response("Retained terminal."),
        )
    )
    # Late observation after terminal MUST NOT enqueue
    flood_channel.observe(
        ProductionObservation(
            stage=ObservationStage.ANSWER,
            status="SUCCESS",
            attempt_index=99,
            invocation_id="trace-flood",
        )
    )
    # Duplicate terminal publish MUST NOT overwrite terminal
    flood_channel.publish_error_payload(error_code="INTERNAL_ERROR", message="ignored")
    attempts: list[int] = []
    while True:
        item = flood_channel.pop_progress_nowait()
        if item is None:
            break
        attempts.append(json.loads(item.split(b"data: ", 1)[1])["attempt"])
    assert attempts == list(range(10, 26))
    assert flood_channel.terminal_frame is not None
    assert flood_channel.terminal_frame.startswith(b"event: final\n")


@pytest.mark.asyncio
async def test_sse_cache_hit_emits_no_fabricated_stages_and_oversized_response_errors_safely() -> (
    None
):
    """Genuine SessionQueryCache hit emits accepted -> final only; oversized final emits safe terminal error."""

    redis = _FakeRedisQueryCache()
    repo = FakeRuntimeMetadataRepo(invalidation_required=False)
    cache = SessionQueryCache(
        redis=cast(Any, redis),
        uow_factory=cast(Any, lambda: FakeSyncUow(repo)),
        runtime_fingerprint="b" * 64,
        ttl_seconds=600,
        policy=QueryCachePolicy(enabled=True),
        clock=lambda: datetime.now(UTC),
    )
    bus = InvocationEventBus()
    rag_core_calls = 0

    class _FakeRagCore:
        async def run_with_facts(self, req: UserTurnRequest) -> RagCoreExecution:
            nonlocal rag_core_calls
            rag_core_calls += 1
            trace_id = current_invocation_id()
            assert trace_id is not None
            deliver_observation(
                bus,
                ProductionObservation(
                    stage=ObservationStage.DENSE, status="OK", invocation_id=trace_id
                ),
            )
            deliver_observation(
                bus,
                ProductionObservation(
                    stage=ObservationStage.ANSWER,
                    status="SUCCESS",
                    invocation_id=trace_id,
                ),
            )
            return RagCoreExecution(
                result=RagCoreResult(
                    final_response=_sample_final_with_citation(
                        f"Fresh: {req.query} [L1]."
                    ),
                    completion_status=RagCompletionStatus.SUCCESS,
                ),
                web_invoked=False,
            )

    runner = RetrievalRunner(
        RetrievalGraphServices(cache=cache, rag_core=cast(Any, _FakeRagCore()))
    )
    coord, _ = await start_test_coordinator(retrieval_with_facts=runner.run_with_facts)
    app = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(),
        coordinator=coord,
        event_bus=bus,
    )
    try:
        async with _open_client(app) as client:
            sid = await _create_session_async(client)
            async with client.stream(
                "POST",
                "/api/v1/query/stream",
                json={"session_id": sid, "query": "cache sse question"},
                headers=_local_headers("web"),
            ) as r1:
                ev1 = _parse_sse_events(await r1.aread())
            assert [n for n, _ in ev1] == ["accepted", "progress", "progress", "final"]
            assert json.loads(ev1[-1][1])["cache_status"] == "MISS"

            async with client.stream(
                "POST",
                "/api/v1/query/stream",
                json={"session_id": sid, "query": "cache sse question"},
                headers=_local_headers("web"),
            ) as r2:
                ev2 = _parse_sse_events(await r2.aread())
            assert [n for n, _ in ev2] == ["accepted", "final"]
            assert json.loads(ev2[-1][1])["cache_status"] == "HIT"
            assert (
                json.loads(ev2[0][1])["trace_id"] != json.loads(ev1[0][1])["trace_id"]
            )
            assert rag_core_calls == 1
    finally:
        await coord.shutdown()

    # Oversized FinalResponse (> max_response_bytes) emits safe terminal error, never truncated JSON
    async def oversized_retrieval(_req: UserTurnRequest) -> FinalResponse:
        return sample_response("X" * 2_000)

    coord_big, _ = await start_test_coordinator(retrieval=oversized_retrieval)
    app_big = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(max_response_bytes=1_024),
        coordinator=coord_big,
        event_bus=bus,
    )
    try:
        async with _open_client(app_big) as client:
            sid = await _create_session_async(client)
            async with client.stream(
                "POST",
                "/api/v1/query/stream",
                json={"session_id": sid, "query": "too big"},
                headers=_local_headers("web"),
            ) as resp:
                assert resp.status_code == 200
                ev_big = _parse_sse_events(await resp.aread())
                assert [n for n, _ in ev_big] == ["accepted", "error"]
                err = json.loads(ev_big[1][1])
                assert err["error_code"] == "INVARIANT_VIOLATION"
    finally:
        await coord_big.shutdown()


@pytest.mark.asyncio
async def test_sse_single_receive_consumer_and_actual_send_timeout_and_send_failure() -> (
    None
):
    """ASGI harness checks single receive consumer, actual send timeout/failure, heartbeats, and full cleanup."""

    guard = ResourceGuard(capacity=1)
    bus = InvocationEventBus()
    worker_started = asyncio.Event()
    settling_started = asyncio.Event()
    worker_release = asyncio.Event()

    async def shielded_retrieval(req: UserTurnRequest) -> FinalResponse:
        del req
        trace_id = current_invocation_id()
        assert trace_id is not None
        async with guard.slot():
            inner = asyncio.create_task(worker_release.wait())
            worker_started.set()
            cancelled = False
            while not inner.done():
                try:
                    await asyncio.shield(inner)
                except asyncio.CancelledError:
                    settling_started.set()
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
        return sample_response()

    coordinator, harness = await start_test_coordinator(retrieval=shielded_retrieval)
    app = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(
            sse_send_timeout_seconds=0.05,
            sse_heartbeat_seconds=0.02,
            query_timeout_seconds=10.0,
        ),
        coordinator=coordinator,
        event_bus=bus,
    )
    service: ApplicationServerService | None = None
    try:
        async with app.router.lifespan_context(app):
            service = app.state.server_service
            assert service is not None
            sid = (await service.sessions.create_session(ClientKind.WEB)).session_id
            payload = json.dumps({"session_id": sid, "query": "send timeout"}).encode(
                "utf-8"
            )

            # Drive ASGI directly to verify single receive() consumer + blocked ASGI send() timeout
            in_flight_receive_calls = 0
            max_concurrent_receive = 0
            disconnect_gate = asyncio.Event()
            body_sent = False

            async def single_receive() -> Message:
                nonlocal in_flight_receive_calls, max_concurrent_receive, body_sent
                if not body_sent:
                    body_sent = True
                    return {"type": "http.request", "body": payload, "more_body": False}
                in_flight_receive_calls += 1
                max_concurrent_receive = max(
                    max_concurrent_receive, in_flight_receive_calls
                )
                try:
                    await disconnect_gate.wait()
                    return {"type": "http.disconnect"}
                finally:
                    in_flight_receive_calls -= 1

            send_block = asyncio.Event()
            sent_messages: list[MutableMapping[str, Any]] = []

            async def stalled_send(message: Message) -> None:
                sent_messages.append(dict(message))
                if message["type"] == "http.response.body":
                    # Stall inside actual ASGI send() past sse_send_timeout_seconds
                    await send_block.wait()

            with warnings.catch_warnings(record=True) as caught_warnings:
                warnings.simplefilter("always")
                asgi_task = asyncio.create_task(
                    app(_make_http_scope(), single_receive, stalled_send)
                )
                await worker_started.wait()
                # Wait until send timeout cancels query_task -> shielded settlement begins
                await settling_started.wait()
                assert not asgi_task.done()
                assert harness.gate.active_queries == 1
                assert guard._semaphore.locked()  # noqa: SLF001
                assert service.supervisor.in_flight_streams == 1
                assert max_concurrent_receive == 1

                # Unblock worker -> everything settles & cleans up cleanly
                worker_release.set()
                await asgi_task
                assert not [
                    w
                    for w in caught_warnings
                    if "Task exception was never retrieved" in str(w.message)
                ]

            assert any(
                m["type"] == "http.response.start" and m["status"] == 200
                for m in sent_messages
            )
            assert harness.gate.active_queries == 0
            assert harness.tasks.snapshot(TaskKind.QUERY) == ()
            assert not guard._semaphore.locked()  # noqa: SLF001
            assert service.sessions.in_flight(sid) == 0
            assert service.supervisor.in_flight_queries == 0
            assert service.supervisor.in_flight_streams == 0
            assert bus.bound_count() == 0

            # Reset for ASGI send failure (ConnectionResetError on first body frame)
            worker_started.clear()
            settling_started.clear()
            worker_release.clear()
            body_sent = False

            async def failing_send(message: Message) -> None:
                if message["type"] == "http.response.body":
                    raise ConnectionResetError("broken pipe")

            asgi_fail = asyncio.create_task(
                app(_make_http_scope(), single_receive, failing_send)
            )
            await worker_started.wait()
            await settling_started.wait()
            assert harness.gate.active_queries == 1
            worker_release.set()
            await asgi_fail
            assert harness.gate.active_queries == 0
            assert service.supervisor.in_flight_streams == 0
            assert bus.bound_count() == 0
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_sse_pre_and_post_admission_disconnect_and_stream_capacity_after_query_finishes() -> (
    None
):
    """Test disconnect before/after admission, bounded stream capacity after query finish, and heartbeats."""

    guard = ResourceGuard(capacity=1)
    bus = InvocationEventBus()
    health_gate = asyncio.Event()
    probe_armed = {"armed": False}
    probe_entered = asyncio.Event()

    async def controllable_pg() -> None:
        if probe_armed["armed"]:
            probe_entered.set()
            await health_gate.wait()

    coord1, harness1 = await start_test_coordinator(
        probes={"postgres": controllable_pg},
        refresh_interval=0.0,
    )
    app1 = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(
            sse_heartbeat_seconds=0.02, query_timeout_seconds=10.0
        ),
        coordinator=coord1,
        event_bus=bus,
    )
    try:
        async with app1.router.lifespan_context(app1):
            svc1: ApplicationServerService = app1.state.server_service
            sid1 = (await svc1.sessions.create_session(ClientKind.WEB)).session_id
            # Arm pre-admission health probe to block, then disconnect before admission
            probe_armed["armed"] = True
            body = json.dumps({"session_id": sid1, "query": "pre-disconnect"}).encode()
            body_consumed = False

            async def disc_receive() -> Message:
                nonlocal body_consumed
                if not body_consumed:
                    body_consumed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await probe_entered.wait()
                return {"type": "http.disconnect"}

            sent: list[MutableMapping[str, Any]] = []

            async def capture_send(msg: Message) -> None:
                sent.append(dict(msg))

            await app1(_make_http_scope(), disc_receive, capture_send)
            start_msg = next(m for m in sent if m["type"] == "http.response.start")
            assert start_msg["status"] == 499
            assert svc1.supervisor.in_flight_queries == 0
            assert svc1.supervisor.in_flight_streams == 0
            assert svc1.sessions.in_flight(sid1) == 0
            assert bus.bound_count() == 0
            health_gate.set()
    finally:
        await coord1.shutdown()

    # Stream capacity remains held after query completes while slow consumer holds ASGI send()
    slow_consumer_gate = asyncio.Event()
    query_finished = asyncio.Event()

    async def fast_retrieval(_req: UserTurnRequest) -> FinalResponse:
        async with guard.slot():
            return sample_response("Completed fast.")

    coord2, harness2 = await start_test_coordinator(retrieval=fast_retrieval)
    app2 = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(
            max_concurrent_streams=1,
            max_concurrent_queries=4,
            sse_send_timeout_seconds=2.0,
        ),
        coordinator=coord2,
        event_bus=bus,
    )
    try:
        async with app2.router.lifespan_context(app2):
            svc2: ApplicationServerService = app2.state.server_service
            sid2 = (await svc2.sessions.create_session(ClientKind.WEB)).session_id
            payload2 = json.dumps({"session_id": sid2, "query": "slow stream"}).encode()
            b2 = False

            async def receive2() -> Message:
                nonlocal b2
                if not b2:
                    b2 = True
                    return {
                        "type": "http.request",
                        "body": payload2,
                        "more_body": False,
                    }
                await asyncio.sleep(5.0)
                return {"type": "http.disconnect"}

            async def slow_final_send(msg: Message) -> None:
                if msg["type"] == "http.response.body" and b"event: final" in msg.get(
                    "body", b""
                ):
                    query_finished.set()
                    await slow_consumer_gate.wait()

            t1 = asyncio.create_task(
                app2(_make_http_scope(), receive2, slow_final_send)
            )
            await query_finished.wait()
            # Query task already finished, yet stream slot MUST remain held until stream closes
            assert harness2.gate.active_queries == 0
            assert svc2.supervisor.in_flight_queries == 0
            assert svc2.supervisor.in_flight_streams == 1

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app2),
                base_url="http://127.0.0.1:8000",
            ) as client:
                second = await client.post(
                    "/api/v1/query/stream",
                    json={"session_id": sid2, "query": "exceeds stream cap"},
                    headers=_local_headers("web"),
                )
                assert second.status_code == 503
                assert second.json()["error_code"] == "RESOURCE_EXHAUSTED"

            slow_consumer_gate.set()
            await t1
            assert svc2.supervisor.in_flight_streams == 0
            assert bus.bound_count() == 0
    finally:
        await coord2.shutdown()


@pytest.mark.asyncio
async def test_sse_concurrent_disconnect_shutdown_and_repeated_cancel_no_leaks() -> (
    None
):
    """Verify heartbeat emission, repeated outer cancel on stream, and shutdown+disconnect race on SSE."""

    guard = ResourceGuard(capacity=1)
    bus = InvocationEventBus()
    worker_started = asyncio.Event()
    settling_started = asyncio.Event()
    worker_release = asyncio.Event()
    cancel_deliveries = 0

    async def shielded_retrieval(_req: UserTurnRequest) -> FinalResponse:
        nonlocal cancel_deliveries
        async with guard.slot():
            inner = asyncio.create_task(worker_release.wait())
            worker_started.set()
            cancelled = False
            while not inner.done():
                try:
                    await asyncio.shield(inner)
                except asyncio.CancelledError:
                    cancel_deliveries += 1
                    settling_started.set()
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
        return sample_response()

    coordinator, harness = await start_test_coordinator(
        retrieval=shielded_retrieval,
        shutdown_timeout_seconds=0.02,
    )
    app = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(
            sse_heartbeat_seconds=0.015,
            sse_send_timeout_seconds=1.0,
            query_timeout_seconds=10.0,
        ),
        coordinator=coordinator,
        event_bus=bus,
    )
    baseline_tasks = len(asyncio.all_tasks())
    try:
        async with app.router.lifespan_context(app):
            svc: ApplicationServerService = app.state.server_service
            sid = (await svc.sessions.create_session(ClientKind.WEB)).session_id
            payload = json.dumps({"session_id": sid, "query": "race sse"}).encode()
            b_done = False
            disc_gate = asyncio.Event()
            heartbeat_seen = asyncio.Event()

            async def recv() -> Message:
                nonlocal b_done
                if not b_done:
                    b_done = True
                    return {"type": "http.request", "body": payload, "more_body": False}
                await disc_gate.wait()
                return {"type": "http.disconnect"}

            async def send_cap(msg: Message) -> None:
                if (
                    msg["type"] == "http.response.body"
                    and msg.get("body") == b": heartbeat\n\n"
                ):
                    heartbeat_seen.set()

            stream_task = asyncio.create_task(app(_make_http_scope(), recv, send_cap))
            await worker_started.wait()
            await heartbeat_seen.wait()

            # Concurrent disconnect + repeated outer cancellation + coordinator shutdown
            disc_gate.set()
            await settling_started.wait()
            stream_task.cancel()
            await asyncio.sleep(0)
            stream_task.cancel()
            with pytest.raises(RuntimeShutdownFailedError):
                await coordinator.shutdown()

            assert cancel_deliveries == 1
            assert harness.gate.active_queries == 1
            assert guard._semaphore.locked()  # noqa: SLF001
            assert svc.supervisor.in_flight_streams == 1

            worker_release.set()
            with pytest.raises(asyncio.CancelledError):
                await stream_task

            await coordinator.shutdown()
            assert harness.gate.active_queries == 0
            assert not guard._semaphore.locked()  # noqa: SLF001
            assert svc.sessions.in_flight(sid) == 0
            assert svc.supervisor.in_flight_queries == 0
            assert svc.supervisor.in_flight_streams == 0
            assert harness.tasks.snapshot() == ()
            assert bus.bound_count() == 0
            assert not harness.ownership.acquired
    finally:
        await coordinator.shutdown()
    assert len(asyncio.all_tasks()) <= baseline_tasks


@pytest.mark.asyncio
async def test_sse_slow_consumer_overflow_during_active_stream_completes_unblocked() -> (
    None
):
    """Paused client ASGI send() drops oldest progress cleanly while RAG completes unblocked, then delivers buffered newest + final."""

    bus = InvocationEventBus()
    accepted_sent = asyncio.Event()
    send_resume = asyncio.Event()
    backend_finished = asyncio.Event()

    async def burst_retrieval(_req: UserTurnRequest) -> FinalResponse:
        trace_id = current_invocation_id()
        assert trace_id is not None
        await accepted_sent.wait()
        for idx in range(25):
            deliver_observation(
                bus,
                ProductionObservation(
                    stage=ObservationStage.DENSE,
                    status="OK",
                    attempt_index=idx + 1,
                    invocation_id=trace_id,
                ),
            )
        backend_finished.set()
        return sample_response("Unblocked completion after progress burst.")

    coord, _ = await start_test_coordinator(retrieval=burst_retrieval)
    app = create_app(
        runtime_config=sample_runtime_config(),
        server_config=ServerConfig(
            sse_queue_maxsize=16,
            sse_send_timeout_seconds=2.0,
        ),
        coordinator=coord,
        event_bus=bus,
    )
    try:
        async with app.router.lifespan_context(app):
            svc: ApplicationServerService = app.state.server_service
            sid = (await svc.sessions.create_session(ClientKind.WEB)).session_id
            payload = json.dumps({"session_id": sid, "query": "slow overflow"}).encode()
            body_done = False

            async def recv() -> Message:
                nonlocal body_done
                if not body_done:
                    body_done = True
                    return {"type": "http.request", "body": payload, "more_body": False}
                await asyncio.sleep(5.0)
                return {"type": "http.disconnect"}

            wire_chunks: list[bytes] = []
            seen_accepted = False

            async def paused_send(msg: Message) -> None:
                nonlocal seen_accepted
                if msg["type"] == "http.response.body":
                    chunk = msg.get("body", b"")
                    wire_chunks.append(chunk)
                    if not seen_accepted and b"event: accepted" in chunk:
                        seen_accepted = True
                        accepted_sent.set()
                        # Hold consumer paused until backend bursts all 25 progress events + finishes RAG
                        await send_resume.wait()

            stream_task = asyncio.create_task(
                app(_make_http_scope(), recv, paused_send)
            )
            await backend_finished.wait()
            # RAG finished unblocked despite paused consumer; now allow consumer to resume within send timeout
            send_resume.set()
            await stream_task

            parsed = _parse_sse_events(b"".join(wire_chunks))
            assert parsed[0][0] == "accepted"
            assert parsed[-1][0] == "final"
            progress_attempts = [
                json.loads(data)["attempt"]
                for name, data in parsed
                if name == "progress"
            ]
            assert progress_attempts == list(range(10, 26))
            assert json.loads(parsed[-1][1])["response"]["answer_text"] == (
                "Unblocked completion after progress burst."
            )
            assert bus.bound_count() == 0
    finally:
        await coord.shutdown()
