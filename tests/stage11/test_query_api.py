"""Tests for POST /api/v1/query, Query Cache wiring, and DisconnectMonitor/QuerySupervisor."""

from __future__ import annotations

import asyncio
import json
import warnings
from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import HttpUrl

from atlasrag.application.hashing import request_hash
from atlasrag.application.rag_core import RagCoreExecution
from atlasrag.application.retrieval import RetrievalRunner
from atlasrag.domain.enums import (
    EvidenceType,
    RagCompletionStatus,
    SourceType,
)
from atlasrag.domain.errors import (
    DependencyUnavailableError,
    ErrorCode,
)
from atlasrag.domain.requests import QueryFilters, UserTurnRequest
from atlasrag.domain.results import (
    FinalLocalCitation,
    FinalResponse,
    FinalWebCitation,
    RagCoreResult,
)
from atlasrag.graphs.retrieval.graph import RetrievalGraphServices
from atlasrag.providers.cache.keys import query_cache_key
from atlasrag.providers.cache.models import CachedQueryResult
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.runtime.resources import ResourceGuard
from atlasrag.server.app import create_app
from atlasrag.server.config import ServerConfig
from atlasrag.server.errors import ServerHttpError, TransportErrorCode
from atlasrag.server.schemas import ClientKind, QueryRequest
from atlasrag.server.service import ApplicationServerService, DisconnectSignal
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.cache.session import QueryCacheStatus, SessionQueryCache
from stage11.conftest import (
    FakeRuntimeMetadataRepo,
    FakeSyncUow,
    sample_runtime_config,
    start_test_coordinator,
)


class _OfflineRedisQueryCache:
    def __init__(self) -> None:
        self.fail_reads = False
        self.fail_writes = False
        self.records: dict[str, tuple[str | None, CachedQueryResult]] = {}
        self.lookup_calls = 0
        self.write_calls = 0

    async def get_query_write_fence(self) -> str | None:
        return "fence-1"

    async def get_fenced_query(
        self, session_id: str, runtime_fingerprint: str, req_hash: str
    ) -> tuple[str | None, CachedQueryResult | None]:
        self.lookup_calls += 1
        if self.fail_reads:
            raise DependencyUnavailableError
        key = query_cache_key(session_id, runtime_fingerprint, req_hash)
        record = self.records.get(key)
        if record is None:
            return "fence-1", None
        return record

    async def put_fenced_query(
        self,
        session_id: str,
        runtime_fingerprint: str,
        req_hash: str,
        value: CachedQueryResult,
        *,
        ttl_seconds: int,
        write_fence: str | None = None,
    ) -> bool:
        self.write_calls += 1
        if self.fail_writes:
            raise DependencyUnavailableError
        key = query_cache_key(session_id, runtime_fingerprint, req_hash)
        self.records[key] = (write_fence, value)
        return True


def _headers(client_kind: str = "web") -> dict[str, str]:
    return {
        "Origin": "http://127.0.0.1:8000",
        "X-AtlasRAG-Client": client_kind,
    }


def _local_only_final_response(answer: str = "Local answer [L1].") -> FinalResponse:
    return FinalResponse(
        answer_text=answer,
        citations=(
            FinalLocalCitation(
                citation_id="L1",
                file_name="handbook.pdf",
                relative_source_path="docs/handbook.pdf",
                evidence_type=EvidenceType.TEXT_PARENT,
            ),
        ),
    )


def _rich_final_response(answer: str = "Answer [L1][W1].") -> FinalResponse:
    return FinalResponse(
        answer_text=answer,
        citations=(
            FinalLocalCitation(
                citation_id="L1",
                file_name="handbook.pdf",
                relative_source_path="docs/handbook.pdf",
                evidence_type=EvidenceType.TEXT_PARENT,
            ),
            FinalWebCitation(
                citation_id="W1",
                title="Atlas Spec",
                url=HttpUrl("https://example.org/spec"),
                domain="example.org",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_query_api_contract_forwarding_bounds_and_errors() -> None:
    seen_requests: list[UserTurnRequest] = []
    mode_flag: dict[str, str] = {"mode": "normal"}

    async def retrieval_handler(req: UserTurnRequest) -> FinalResponse:
        seen_requests.append(req)
        if mode_flag["mode"] == "secret_crash":
            raise RuntimeError(
                "postgresql://atlasrag:top-secret@postgres/db /home/private/secret.txt"
            )
        if mode_flag["mode"] == "dep_down":
            raise DependencyUnavailableError
        return _rich_final_response(f"Echo: {req.query} [L1][W1]")

    coordinator, harness = await start_test_coordinator(retrieval=retrieval_handler)
    try:
        cfg = ServerConfig(max_query_chars=128, max_response_bytes=4096)
        app = create_app(
            runtime_config=sample_runtime_config(),
            server_config=cfg,
            coordinator=coordinator,
        )
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            session_id = client.post(
                "/api/v1/sessions", headers=_headers(), json={}
            ).json()["session_id"]

            raw_query = "  Explain   hybrid retrieval — raw!  "
            resp = client.post(
                "/api/v1/query",
                headers=_headers(),
                json={
                    "session_id": session_id,
                    "query": raw_query,
                    "filters": {
                        "file_name": "handbook.pdf",
                        "source_type": "PDF",
                        "sheet_name": None,
                    },
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["session_id"] == session_id
            assert body["cache_status"] == QueryCacheStatus.UNKNOWN.value
            assert body["elapsed_ms"] >= 0.0
            assert len(body["trace_id"]) == 32
            assert body["response"]["answer_text"] == f"Echo: {raw_query} [L1][W1]"
            assert [c["citation_id"] for c in body["response"]["citations"]] == [
                "L1",
                "W1",
            ]
            assert len(seen_requests) == 1
            assert seen_requests[0].query == raw_query
            assert seen_requests[0].filters == QueryFilters(
                file_name="handbook.pdf", source_type=SourceType.PDF
            )

            # Unsupported query knobs fail 422 INVALID_REQUEST
            for bad_key in (
                "top_k",
                "web_enabled",
                "model",
                "milvus_expr",
                "domain_ids",
                "max_security_level",
                "doc_version",
            ):
                bad = client.post(
                    "/api/v1/query",
                    headers=_headers(),
                    json={"session_id": session_id, "query": "hello", bad_key: 1},
                )
                assert bad.status_code == 422
                assert (
                    bad.json()["error_code"] == TransportErrorCode.INVALID_REQUEST.value
                )

            # Configured max_query_chars bound fails 422 without leaking raw query
            secret_long = "secret-overlong-" + ("x" * 130)
            over = client.post(
                "/api/v1/query",
                headers=_headers(),
                json={"session_id": session_id, "query": secret_long},
            )
            assert over.status_code == 422
            assert secret_long not in over.text

            # CSRF and Origin checks are enforced on /api/v1/query
            no_csrf = client.post(
                "/api/v1/query",
                headers={"Origin": "http://127.0.0.1:8000"},
                json={"session_id": session_id, "query": "hi"},
            )
            assert no_csrf.status_code == 403
            bad_origin = client.post(
                "/api/v1/query",
                headers={"Origin": "https://evil.invalid", "X-AtlasRAG-Client": "web"},
                json={"session_id": session_id, "query": "hi"},
            )
            assert bad_origin.status_code == 403

            # Oversized serialized response fails closed 500 INVARIANT_VIOLATION (no truncation)
            small_app = create_app(
                runtime_config=sample_runtime_config(),
                server_config=ServerConfig(max_response_bytes=1024),
                coordinator=coordinator,
            )
            with TestClient(
                small_app, base_url="http://127.0.0.1:8000"
            ) as small_client:
                s2 = small_client.post(
                    "/api/v1/sessions", headers=_headers(), json={}
                ).json()["session_id"]
                big_err = small_client.post(
                    "/api/v1/query",
                    headers=_headers(),
                    json={"session_id": s2, "query": "a" * 900},
                )
                assert big_err.status_code == 500
                assert (
                    big_err.json()["error_code"] == ErrorCode.INVARIANT_VIOLATION.value
                )

            # Dependency failure -> 503 DEPENDENCY_UNAVAILABLE; crash redacts DSN/paths
            mode_flag["mode"] = "dep_down"
            dep_err = client.post(
                "/api/v1/query",
                headers=_headers(),
                json={"session_id": session_id, "query": "hi"},
            )
            assert dep_err.status_code == 503
            assert (
                dep_err.json()["error_code"] == ErrorCode.DEPENDENCY_UNAVAILABLE.value
            )

            mode_flag["mode"] = "secret_crash"
            crash_app = create_app(
                runtime_config=sample_runtime_config(),
                server_config=cfg,
                coordinator=coordinator,
            )
            with TestClient(
                crash_app,
                base_url="http://127.0.0.1:8000",
                raise_server_exceptions=False,
            ) as no_raise_client:
                s_crash = no_raise_client.post(
                    "/api/v1/sessions", headers=_headers(), json={}
                ).json()["session_id"]
                crash_err = no_raise_client.post(
                    "/api/v1/query",
                    headers=_headers(),
                    json={"session_id": s_crash, "query": "hi"},
                )
            assert crash_err.status_code == 500
            assert (
                crash_err.json()["error_code"]
                == TransportErrorCode.INTERNAL_ERROR.value
            )
            assert "top-secret" not in crash_err.text
            assert "/home/private" not in crash_err.text

            # Runtime gate BLOCKED/not ready -> 503 RUNTIME_NOT_READY
            mode_flag["mode"] = "normal"
            await harness.gate.set_admission_ready(
                False, lifecycle_generation=harness.gate.lifecycle_generation
            )
            not_ready = client.post(
                "/api/v1/query",
                headers=_headers(),
                json={"session_id": session_id, "query": "hi"},
            )
            assert not_ready.status_code == 503
            assert not_ready.json()["error_code"] == ErrorCode.RUNTIME_NOT_READY.value

            # SHUTTING_DOWN -> 503 RUNTIME_SHUTTING_DOWN
            await harness.gate.begin_shutdown()
            shut = client.post(
                "/api/v1/query",
                headers=_headers(),
                json={"session_id": session_id, "query": "hi"},
            )
            assert shut.status_code == 503
            assert shut.json()["error_code"] == ErrorCode.RUNTIME_SHUTTING_DOWN.value
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_query_cache_miss_hit_isolation_and_bypass_semantics() -> None:
    redis = _OfflineRedisQueryCache()
    repo = FakeRuntimeMetadataRepo(invalidation_required=False)
    cache = SessionQueryCache(
        redis=cast(Any, redis),
        uow_factory=cast(Any, lambda: FakeSyncUow(repo)),
        runtime_fingerprint="fp-wpb",
        ttl_seconds=120,
        policy=QueryCachePolicy(enabled=True),
        clock=lambda: datetime.now(UTC),
        require_write_fence=True,
        expected_write_fence=lambda: "fence-1",
    )

    rag_mode: dict[str, Any] = {
        "web_invoked": False,
        "status": RagCompletionStatus.SUCCESS,
    }
    rag_calls = 0

    class _MockRagCore:
        async def run(self, request: UserTurnRequest) -> RagCoreResult:
            nonlocal rag_calls
            rag_calls += 1
            return RagCoreResult(
                final_response=FinalResponse(
                    answer_text=f"Answer-{rag_calls} for {request.session_id} [L1].",
                    citations=(
                        FinalLocalCitation(
                            citation_id="L1",
                            file_name="doc.pdf",
                            relative_source_path="doc.pdf",
                            evidence_type=EvidenceType.TEXT_PARENT,
                        ),
                    ),
                ),
                completion_status=rag_mode["status"],
            )

        async def run_with_facts(self, request: UserTurnRequest) -> RagCoreExecution:
            return RagCoreExecution(
                result=await self.run(request),
                web_invoked=bool(rag_mode["web_invoked"]),
            )

    runner = RetrievalRunner(
        RetrievalGraphServices(
            cache=cast(Any, cache), rag_core=cast(Any, _MockRagCore())
        )
    )
    coordinator, harness = await start_test_coordinator(
        retrieval_with_facts=runner.run_with_facts
    )
    try:
        app = create_app(
            runtime_config=sample_runtime_config(),
            server_config=ServerConfig(),
            coordinator=coordinator,
        )
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            sa = client.post("/api/v1/sessions", headers=_headers(), json={}).json()[
                "session_id"
            ]
            sb = client.post("/api/v1/sessions", headers=_headers(), json={}).json()[
                "session_id"
            ]

            # Session A + Q -> MISS, then Session A + Q -> HIT (fresh trace_id)
            a1 = client.post(
                "/api/v1/query",
                headers=_headers(),
                json={"session_id": sa, "query": "explain cache"},
            ).json()
            assert a1["cache_status"] == QueryCacheStatus.MISS.value
            assert rag_calls == 1

            a2 = client.post(
                "/api/v1/query",
                headers=_headers(),
                json={"session_id": sa, "query": "explain cache"},
            ).json()
            assert a2["cache_status"] == QueryCacheStatus.HIT.value
            assert a2["trace_id"] != a1["trace_id"]
            assert a2["response"] == a1["response"]
            assert rag_calls == 1

            # Cross-session isolation: Session B + Q -> MISS, then Session B + Q -> HIT
            b1 = client.post(
                "/api/v1/query",
                headers=_headers(),
                json={"session_id": sb, "query": "explain cache"},
            ).json()
            assert b1["cache_status"] == QueryCacheStatus.MISS.value
            assert rag_calls == 2
            b2 = client.post(
                "/api/v1/query",
                headers=_headers(),
                json={"session_id": sb, "query": "explain cache"},
            ).json()
            assert b2["cache_status"] == QueryCacheStatus.HIT.value
            assert b2["response"]["answer_text"] != a1["response"]["answer_text"]
            assert rag_calls == 2

            # Cache key hash preserved verbatim
            expected_req_hash = request_hash(
                UserTurnRequest(session_id=sa, query="explain cache")
            )
            assert query_cache_key(sa, "fp-wpb", expected_req_hash) in redis.records

            # Web-invoked MISS is not cached on subsequent query
            rag_mode["web_invoked"] = True
            w1 = client.post(
                "/api/v1/query",
                headers=_headers(),
                json={"session_id": sa, "query": "web lookup question"},
            ).json()
            assert w1["cache_status"] == QueryCacheStatus.MISS.value
            rag_mode["web_invoked"] = False
            w2 = client.post(
                "/api/v1/query",
                headers=_headers(),
                json={"session_id": sa, "query": "web lookup question"},
            ).json()
            assert w2["cache_status"] == QueryCacheStatus.MISS.value

            # Ambiguous Redis write failure reports lookup truth MISS cleanly without breaking response
            redis.fail_writes = True
            aw = client.post(
                "/api/v1/query",
                headers=_headers(),
                json={"session_id": sa, "query": "ambiguous write query"},
            ).json()
            assert aw["cache_status"] == QueryCacheStatus.MISS.value
            redis.fail_writes = False

            # Redis read failure -> BYPASSED
            redis.fail_reads = True
            br = client.post(
                "/api/v1/query",
                headers=_headers(),
                json={"session_id": sa, "query": "explain cache"},
            ).json()
            assert br["cache_status"] == QueryCacheStatus.BYPASSED.value
            redis.fail_reads = False

            # Rejected session -> zero cache access
            lookups_before = redis.lookup_calls
            bad_sess = client.post(
                "/api/v1/query",
                headers=_headers(),
                json={"session_id": "web_" + "f" * 32, "query": "explain cache"},
            )
            assert bad_sess.status_code == 404
            assert redis.lookup_calls == lookups_before

            # Cache HIT still obeys RuntimeGate admission
            await harness.gate.set_admission_ready(
                False, lifecycle_generation=harness.gate.lifecycle_generation
            )
            blocked_hit = client.post(
                "/api/v1/query",
                headers=_headers(),
                json={"session_id": sa, "query": "explain cache"},
            )
            assert blocked_hit.status_code == 503
            assert blocked_hit.json()["error_code"] == ErrorCode.RUNTIME_NOT_READY.value
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_http_asgi_disconnect_cancels_query_and_avoids_duplicate_receive() -> (
    None
):
    started = asyncio.Event()
    cleanup_done = asyncio.Event()

    async def slow_retrieval(_req: UserTurnRequest) -> FinalResponse:
        started.set()
        try:
            await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            cleanup_done.set()
            raise
        return _rich_final_response()

    coordinator, harness = await start_test_coordinator(retrieval=slow_retrieval)
    try:
        app = create_app(
            runtime_config=sample_runtime_config(),
            server_config=ServerConfig(),
            coordinator=coordinator,
        )
        async with app.router.lifespan_context(app):
            service: ApplicationServerService = app.state.server_service
            session_id = (
                await service.sessions.create_session(ClientKind.WEB)
            ).session_id

            body_bytes = json.dumps(
                {"session_id": session_id, "query": "disconnect me"}
            ).encode("utf-8")
            receive_calls = 0
            disconnect_gate = asyncio.Event()
            sent_messages: list[MutableMapping[str, Any]] = []

            async def asgi_receive() -> dict[str, Any]:
                nonlocal receive_calls
                receive_calls += 1
                if receive_calls == 1:
                    return {
                        "type": "http.request",
                        "body": body_bytes,
                        "more_body": False,
                    }
                # Only one disconnect monitor task should be awaiting receive()
                assert receive_calls == 2
                await disconnect_gate.wait()
                return {"type": "http.disconnect"}

            async def asgi_send(message: MutableMapping[str, Any]) -> None:
                sent_messages.append(message)

            scope = {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "path": "/api/v1/query",
                "raw_path": b"/api/v1/query",
                "root_path": "",
                "scheme": "http",
                "query_string": b"",
                "headers": [
                    (b"host", b"127.0.0.1:8000"),
                    (b"origin", b"http://127.0.0.1:8000"),
                    (b"x-atlasrag-client", b"web"),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body_bytes)).encode("ascii")),
                ],
                "client": ("127.0.0.1", 55555),
                "server": ("127.0.0.1", 8000),
            }

            request_task = asyncio.create_task(app(scope, asgi_receive, asgi_send))
            await started.wait()
            disconnect_gate.set()
            await request_task

            assert cleanup_done.is_set()
            assert receive_calls == 2
            assert sent_messages[0]["status"] == 499
            assert (
                json.loads(sent_messages[1]["body"].decode("utf-8"))["error_code"]
                == TransportErrorCode.REQUEST_CANCELLED.value
            )
            assert service.supervisor.in_flight_queries == 0
            assert service.sessions.in_flight(session_id) == 0
            assert harness.gate.active_queries == 0
            assert harness.tasks.snapshot() == ()
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_query_supervisor_timeout_repeated_cancel_saturation_and_cache_race() -> (
    None
):
    redis = _OfflineRedisQueryCache()
    repo = FakeRuntimeMetadataRepo(invalidation_required=False)
    cache = SessionQueryCache(
        redis=cast(Any, redis),
        uow_factory=cast(Any, lambda: FakeSyncUow(repo)),
        runtime_fingerprint="fp-cancel",
        ttl_seconds=60,
        policy=QueryCachePolicy(enabled=True),
        clock=lambda: datetime.now(UTC),
        require_write_fence=True,
        expected_write_fence=lambda: "fence-1",
    )
    guard = ResourceGuard(capacity=1)

    events = {
        "worker_started": asyncio.Event(),
        "settle_gate": asyncio.Event(),
        "cancel_count": 0,
        "guard_released_inside_worker": False,
        "mode": "hold",
    }

    async def controlled_retrieval(req: UserTurnRequest) -> FinalResponse:
        events["worker_started"].set()  # type: ignore[attr-defined]
        mode = events["mode"]
        if mode == "guard_hold":
            async with guard.slot():
                try:
                    await asyncio.sleep(10.0)
                except asyncio.CancelledError:
                    await events["settle_gate"].wait()  # type: ignore[attr-defined]
                    raise
            return _rich_final_response()
        if mode == "commit_hold":
            res = RagCoreResult(
                final_response=_local_only_final_response("commit-held [L1]."),
                completion_status=RagCompletionStatus.SUCCESS,
            )
            await cache.store(req, res, web_invoked=False)
            return res.final_response
        if mode == "boom":
            raise RuntimeError("boom inside worker")
        try:
            if mode == "cache_before_cancel":
                await asyncio.sleep(10.0)
                res = RagCoreResult(
                    final_response=_local_only_final_response("partial [L1]."),
                    completion_status=RagCompletionStatus.SUCCESS,
                )
                await cache.store(req, res, web_invoked=False)
                return res.final_response
            await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            events["cancel_count"] = int(events["cancel_count"]) + 1  # type: ignore[call-overload]
            await events["settle_gate"].wait()  # type: ignore[attr-defined]
            raise
        return _rich_final_response()

    coordinator, harness = await start_test_coordinator(retrieval=controlled_retrieval)
    try:
        cfg = ServerConfig(
            query_timeout_seconds=0.05,
            max_concurrent_queries=1,
            max_concurrent_streams=1,
            max_in_flight_per_session=1,
        )
        service = ApplicationServerService(
            coordinator=coordinator,
            runtime_config=sample_runtime_config(),
            server_config=cfg,
        )
        sid = (await service.sessions.create_session(ClientKind.WEB)).session_id

        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")

            # 1. Repeated outer cancellation while worker settles (MUST cancel once & wait for settle)
            events["worker_started"] = asyncio.Event()
            events["settle_gate"] = asyncio.Event()
            events["cancel_count"] = 0
            events["mode"] = "hold"

            outer_task = asyncio.create_task(
                service.query(
                    QueryRequest(session_id=sid, query="q1"),
                    header_client_kind=ClientKind.WEB,
                )
            )
            await events["worker_started"].wait()  # type: ignore[attr-defined]
            outer_task.cancel()
            await asyncio.sleep(0)
            outer_task.cancel()
            await asyncio.sleep(0)
            outer_task.cancel()
            await asyncio.sleep(0)

            assert not outer_task.done()
            assert service.supervisor.in_flight_queries == 1
            assert service.sessions.in_flight(sid) == 1
            assert harness.gate.active_queries == 1

            events["settle_gate"].set()  # type: ignore[attr-defined]
            with pytest.raises(asyncio.CancelledError):
                await outer_task
            assert events["cancel_count"] == 1
            assert service.supervisor.in_flight_queries == 0
            assert service.sessions.in_flight(sid) == 0
            assert harness.gate.active_queries == 0
            assert harness.tasks.snapshot() == ()

            # 2. Query execution timeout -> 504 DEPENDENCY_TIMEOUT after settlement
            events["worker_started"] = asyncio.Event()
            events["settle_gate"] = asyncio.Event()
            events["settle_gate"].set()  # type: ignore[attr-defined]
            with pytest.raises(ServerHttpError if False else Exception) as t_exc:
                await service.query(
                    QueryRequest(session_id=sid, query="timeout q"),
                    header_client_kind=ClientKind.WEB,
                )
            assert getattr(t_exc.value, "code", None) is ErrorCode.DEPENDENCY_TIMEOUT
            assert service.supervisor.in_flight_queries == 0
            assert service.sessions.in_flight(sid) == 0

            # 3. Concurrent disconnect + timeout + shutdown / session deletion while settling
            events["worker_started"] = asyncio.Event()
            events["settle_gate"] = asyncio.Event()
            signal = DisconnectSignal()
            monitor_cancelled = asyncio.Event()

            async def _dummy_monitor(sig: DisconnectSignal) -> None:
                try:
                    await sig.event.wait()
                    await asyncio.sleep(5.0)
                except asyncio.CancelledError:
                    monitor_cancelled.set()
                    raise

            slow_task = asyncio.create_task(
                service.query(
                    QueryRequest(session_id=sid, query="race q"),
                    header_client_kind=ClientKind.WEB,
                    disconnect_signal=signal,
                    monitor_factory=lambda s: asyncio.create_task(_dummy_monitor(s)),
                )
            )
            await events["worker_started"].wait()  # type: ignore[attr-defined]

            # Transport saturation -> immediate 503 RESOURCE_EXHAUSTED, rolling back session refcount!
            sid2 = (await service.sessions.create_session(ClientKind.WEB)).session_id
            with pytest.raises(Exception) as sat_exc:
                await service.query(
                    QueryRequest(session_id=sid2, query="over capacity"),
                    header_client_kind=ClientKind.WEB,
                )
            assert getattr(sat_exc.value, "code", None) is ErrorCode.RESOURCE_EXHAUSTED
            assert service.sessions.in_flight(sid2) == 0

            # Close sid while cancelled query is still settling -> entry retained until settle, then purged!
            await service.close_session(sid, header_client_kind=ClientKind.WEB)
            assert service.sessions.contains(sid)
            signal.trigger("client_disconnect")
            await asyncio.sleep(
                0.06
            )  # Allow timeout timer to race alongside disconnect
            assert not slow_task.done()

            events["settle_gate"].set()  # type: ignore[attr-defined]
            with pytest.raises(ServerHttpError) as race_exc:
                await slow_task
            assert race_exc.value.code is TransportErrorCode.REQUEST_CANCELLED
            assert monitor_cancelled.is_set()
            assert not service.sessions.contains(sid)
            assert service.supervisor.in_flight_queries == 0
            assert harness.gate.active_queries == 0
            assert harness.tasks.snapshot() == ()

            # 4. Pre-completion disconnect writes zero cache and releases ResourceGuard strictly after settle
            sid3 = (await service.sessions.create_session(ClientKind.WEB)).session_id
            events["worker_started"] = asyncio.Event()
            events["settle_gate"] = asyncio.Event()
            events["mode"] = "cache_before_cancel"
            sig3 = DisconnectSignal()
            cache_task = asyncio.create_task(
                service.query(
                    QueryRequest(session_id=sid3, query="no partial cache"),
                    header_client_kind=ClientKind.WEB,
                    disconnect_signal=sig3,
                )
            )
            await events["worker_started"].wait()  # type: ignore[attr-defined]
            sig3.trigger("client_disconnect")
            events["settle_gate"].set()  # type: ignore[attr-defined]
            with pytest.raises(ServerHttpError) as cache_cancel_exc:
                await cache_task
            assert cache_cancel_exc.value.code is TransportErrorCode.REQUEST_CANCELLED
            assert redis.write_calls == 0

            # 5. Cancellation during admission (before query_task starts) rolls back session & slot
            async def slow_acquire(
                _sid: str, *, client_kind: ClientKind | None = None
            ) -> Any:
                await asyncio.sleep(5.0)

            real_acquire = service.sessions.acquire
            service.sessions.acquire = cast(Any, slow_acquire)  # type: ignore[method-assign]
            admit_task = asyncio.create_task(
                service.query(
                    QueryRequest(session_id=sid3, query="admit cancel"),
                    header_client_kind=ClientKind.WEB,
                )
            )
            await asyncio.sleep(0)
            admit_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await admit_task
            service.sessions.acquire = real_acquire  # type: ignore[method-assign]
            assert service.supervisor.in_flight_queries == 0
            assert service.sessions.in_flight(sid3) == 0

            # 6. Disconnect during ResourceGuard.slot() waits for guard release before exit
            events["worker_started"] = asyncio.Event()
            events["settle_gate"] = asyncio.Event()
            events["mode"] = "guard_hold"
            sig_guard = DisconnectSignal()
            guard_task = asyncio.create_task(
                service.query(
                    QueryRequest(session_id=sid3, query="guard slot"),
                    header_client_kind=ClientKind.WEB,
                    disconnect_signal=sig_guard,
                )
            )
            await events["worker_started"].wait()  # type: ignore[attr-defined]
            await asyncio.sleep(0)
            assert guard._semaphore.locked()  # noqa: SLF001
            sig_guard.trigger("client_disconnect")
            await asyncio.sleep(0)
            assert not guard_task.done()
            assert guard._semaphore.locked()  # noqa: SLF001
            events["settle_gate"].set()  # type: ignore[attr-defined]
            with pytest.raises(ServerHttpError) as guard_exc:
                await guard_task
            assert guard_exc.value.code is TransportErrorCode.REQUEST_CANCELLED
            assert not guard._semaphore.locked()  # noqa: SLF001
            assert service.supervisor.in_flight_queries == 0
            assert service.sessions.in_flight(sid3) == 0

            # 7. Disconnect during SessionQueryCache.commit shields Redis commit to settlement
            commit_started = asyncio.Event()
            commit_gate = asyncio.Event()
            commit_finished = False
            real_put = redis.put_fenced_query

            async def delayed_put(
                session_id: str,
                runtime_fingerprint: str,
                req_hash: str,
                value: CachedQueryResult,
                *,
                ttl_seconds: int,
                write_fence: str | None = None,
            ) -> bool:
                nonlocal commit_finished
                commit_started.set()
                await commit_gate.wait()
                ok = await real_put(
                    session_id,
                    runtime_fingerprint,
                    req_hash,
                    value,
                    ttl_seconds=ttl_seconds,
                    write_fence=write_fence,
                )
                commit_finished = True
                return ok

            redis.put_fenced_query = delayed_put  # type: ignore[method-assign]
            events["worker_started"] = asyncio.Event()
            events["mode"] = "commit_hold"
            sig_commit = DisconnectSignal()
            commit_task = asyncio.create_task(
                service.query(
                    QueryRequest(session_id=sid3, query="shield commit"),
                    header_client_kind=ClientKind.WEB,
                    disconnect_signal=sig_commit,
                )
            )
            await commit_started.wait()
            sig_commit.trigger("client_disconnect")
            await asyncio.sleep(0)
            assert not commit_task.done()
            commit_gate.set()
            with pytest.raises(ServerHttpError) as commit_exc:
                await commit_task
            assert commit_exc.value.code is TransportErrorCode.REQUEST_CANCELLED
            assert commit_finished is True
            assert service.supervisor.in_flight_queries == 0
            assert service.sessions.in_flight(sid3) == 0

            # 8. Worker exception frees slot & session refcount cleanly
            events["mode"] = "boom"
            with pytest.raises(RuntimeError, match="boom inside worker"):
                await service.query(
                    QueryRequest(session_id=sid3, query="boom q"),
                    header_client_kind=ClientKind.WEB,
                )
            assert service.supervisor.in_flight_queries == 0
            assert service.sessions.in_flight(sid3) == 0

            # 9. Concurrent disconnect + coordinator.shutdown() cancellation settles cleanly
            events["worker_started"] = asyncio.Event()
            events["settle_gate"] = asyncio.Event()
            events["mode"] = "hold"
            sig_shut = DisconnectSignal()
            shut_query = asyncio.create_task(
                service.query(
                    QueryRequest(session_id=sid3, query="shutdown race"),
                    header_client_kind=ClientKind.WEB,
                    disconnect_signal=sig_shut,
                )
            )
            await events["worker_started"].wait()  # type: ignore[attr-defined]
            sig_shut.trigger("client_disconnect")
            shutdown_task = asyncio.create_task(coordinator.shutdown())
            await asyncio.sleep(0)
            events["settle_gate"].set()  # type: ignore[attr-defined]
            with pytest.raises((ServerHttpError, Exception)):
                await shut_query
            await shutdown_task
            assert harness.gate.mode is RuntimeMode.SHUTTING_DOWN
            assert service.supervisor.in_flight_queries == 0
            assert service.sessions.in_flight(sid3) == 0

            assert not [
                w
                for w in caught_warnings
                if "Task exception was never retrieved" in str(w.message)
            ]
    finally:
        await coordinator.shutdown()
