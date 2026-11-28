"""Unit and HTTP contract tests for Stage 11 SessionRegistry and session routes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from atlasrag.application.retrieval import RetrievalRunner
from atlasrag.domain.enums import EvidenceType, RagCompletionStatus
from atlasrag.domain.errors import ErrorCode
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.domain.results import FinalLocalCitation, FinalResponse, RagCoreResult
from atlasrag.graphs.retrieval.graph import RetrievalGraphServices
from atlasrag.providers.cache.keys import is_cache_key_component_safe, query_cache_key
from atlasrag.providers.cache.models import CachedQueryResult
from atlasrag.server.app import create_app
from atlasrag.server.config import ServerConfig
from atlasrag.server.errors import ServerHttpError, TransportErrorCode
from atlasrag.server.schemas import ClientKind, CreateSessionRequest
from atlasrag.server.sessions import SessionRegistry
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.cache.session import QueryCacheStatus, SessionQueryCache
from stage11.conftest import (
    FakeRuntimeMetadataRepo,
    FakeSyncUow,
    sample_response,
    sample_runtime_config,
    start_test_coordinator,
)


class _SpyRedis:
    def __init__(self) -> None:
        self.scan_or_delete_calls = 0
        self.puts = 0

    async def get_fenced_query(
        self, session_id: str, runtime_fingerprint: str, request_hash: str
    ) -> tuple[str | None, CachedQueryResult | None]:
        return "fence-1", None

    async def put_fenced_query(
        self,
        session_id: str,
        runtime_fingerprint: str,
        request_hash: str,
        value: CachedQueryResult,
        *,
        ttl_seconds: int,
        write_fence: str | None = None,
    ) -> bool:
        self.puts += 1
        return True

    async def get_query_write_fence(self) -> str | None:
        return "fence-1"

    async def invalidate_queries(self) -> int:
        self.scan_or_delete_calls += 1
        return 0

    async def delete_query(self, *_args: object) -> int:
        self.scan_or_delete_calls += 1
        return 0


class _ManualClock:
    def __init__(self) -> None:
        self.mono = 100.0
        self.utc = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.utc = self.utc + timedelta(seconds=seconds)


@pytest.mark.asyncio
async def test_session_registry_identity_bounds_and_lifecycle() -> None:
    clock = _ManualClock()
    cfg = ServerConfig(
        max_sessions=2,
        session_ttl_seconds=30.0,
        max_in_flight_per_session=2,
        max_concurrent_queries=4,
        max_concurrent_streams=4,
    )
    registry = SessionRegistry(
        cfg,
        clock=lambda: clock.utc,
        monotonic_clock=lambda: clock.mono,
    )

    web_info = await registry.create_session(ClientKind.WEB)
    cli_info = await registry.create_session(ClientKind.CLI)
    assert web_info.session_id.startswith("web_")
    assert cli_info.session_id.startswith("cli_")
    assert len(web_info.session_id) == 4 + 32
    assert len(cli_info.session_id) == 4 + 32
    assert web_info.session_id != cli_info.session_id
    assert is_cache_key_component_safe(web_info.session_id)
    assert is_cache_key_component_safe(cli_info.session_id)
    assert web_info.expires_at > web_info.created_at

    # Capacity exhaustion returns 503 RESOURCE_EXHAUSTED
    with pytest.raises(ServerHttpError) as cap_exc:
        await registry.create_session(ClientKind.WEB)
    assert cap_exc.value.status_code == 503
    assert cap_exc.value.code is ErrorCode.RESOURCE_EXHAUSTED

    # Unknown session fails with 404 SESSION_NOT_FOUND
    with pytest.raises(ServerHttpError) as unk_exc:
        await registry.acquire("web_" + "0" * 32, client_kind=ClientKind.WEB)
    assert unk_exc.value.status_code == 404
    assert unk_exc.value.code is TransportErrorCode.SESSION_NOT_FOUND

    # Client classification mismatch fails with 403 FORBIDDEN
    with pytest.raises(ServerHttpError) as kind_exc:
        await registry.acquire(web_info.session_id, client_kind=ClientKind.CLI)
    assert kind_exc.value.status_code == 403
    assert kind_exc.value.code is TransportErrorCode.FORBIDDEN

    # Per-session max_in_flight limit enforced & idempotent release
    l1 = await registry.acquire(web_info.session_id, client_kind=ClientKind.WEB)
    l2 = await registry.acquire(web_info.session_id, client_kind=ClientKind.WEB)
    assert registry.in_flight(web_info.session_id) == 2
    with pytest.raises(ServerHttpError) as inflight_exc:
        await registry.acquire(web_info.session_id, client_kind=ClientKind.WEB)
    assert inflight_exc.value.status_code == 503
    assert inflight_exc.value.code is ErrorCode.RESOURCE_EXHAUSTED

    await registry.release(l1)
    await registry.release(l1)  # Idempotent release must not decrement twice
    assert registry.in_flight(web_info.session_id) == 1

    # Deleting session while query is in flight preserves active entry until settlement
    await registry.close_session(web_info.session_id, client_kind=ClientKind.WEB)
    assert registry.contains(web_info.session_id)
    with pytest.raises(ServerHttpError) as closed_exc:
        await registry.acquire(web_info.session_id, client_kind=ClientKind.WEB)
    assert closed_exc.value.code is TransportErrorCode.SESSION_NOT_FOUND
    await registry.release(l2)
    assert not registry.contains(web_info.session_id)
    assert registry.in_flight(web_info.session_id) == 0

    # Idle expiration during an active query rejects new acquisitions & purges post-settlement
    cli_lease = await registry.acquire(cli_info.session_id, client_kind=ClientKind.CLI)
    clock.advance(31.0)
    with pytest.raises(ServerHttpError) as exp_exc:
        await registry.acquire(cli_info.session_id, client_kind=ClientKind.CLI)
    assert exp_exc.value.code is TransportErrorCode.SESSION_NOT_FOUND
    assert registry.contains(cli_info.session_id)
    await registry.release(cli_lease)
    assert not registry.contains(cli_info.session_id)

    # Opportunistic pruning reclaims idle expired sessions on create_session()
    s_old = await registry.create_session(ClientKind.WEB)
    _s_old2 = await registry.create_session(ClientKind.WEB)
    assert registry.active_count() == 2
    clock.advance(31.0)
    s_new = await registry.create_session(ClientKind.WEB)
    assert not registry.contains(s_old.session_id)
    assert registry.contains(s_new.session_id)
    assert registry.active_count() == 1


@pytest.mark.asyncio
async def test_session_concurrent_acquire_and_delete() -> None:
    cfg = ServerConfig(max_sessions=16, max_in_flight_per_session=4)
    registry = SessionRegistry(cfg)
    info = await registry.create_session(ClientKind.WEB)
    lease = await registry.acquire(info.session_id, client_kind=ClientKind.WEB)

    async def try_acquire() -> str:
        try:
            extra = await registry.acquire(info.session_id, client_kind=ClientKind.WEB)
        except ServerHttpError as exc:
            return exc.code.value
        await registry.release(extra)
        return "ACQUIRED"

    async def close_now() -> str:
        await registry.close_session(info.session_id, client_kind=ClientKind.WEB)
        return "CLOSED"

    outcomes = await asyncio.gather(
        try_acquire(),
        close_now(),
        try_acquire(),
    )
    assert "CLOSED" in outcomes
    assert registry.contains(info.session_id)
    await registry.release(lease)
    assert not registry.contains(info.session_id)


@pytest.mark.asyncio
async def test_session_routes_csrf_redis_isolation_and_restart_invalidation() -> None:
    with pytest.raises(ValidationError):
        CreateSessionRequest.model_validate({"session_id": "caller-id"})
    with pytest.raises(ValidationError):
        CreateSessionRequest.model_validate({"client_kind": "mcp"})

    spy_redis = _SpyRedis()
    repo = FakeRuntimeMetadataRepo(invalidation_required=False)
    cache = SessionQueryCache(
        redis=cast(Any, spy_redis),
        uow_factory=cast(Any, lambda: FakeSyncUow(repo)),
        runtime_fingerprint="fp-session",
        ttl_seconds=60,
        policy=QueryCachePolicy(enabled=True),
        clock=lambda: datetime.now(UTC),
        require_write_fence=True,
        expected_write_fence=lambda: "fence-1",
    )

    class _SimpleRag:
        async def run(self, _req: UserTurnRequest) -> RagCoreResult:
            return RagCoreResult(
                final_response=FinalResponse(
                    answer_text="Answer [L1].",
                    citations=(
                        FinalLocalCitation(
                            citation_id="L1",
                            file_name="doc.pdf",
                            relative_source_path="doc.pdf",
                            evidence_type=EvidenceType.TEXT_PARENT,
                        ),
                    ),
                ),
                completion_status=RagCompletionStatus.SUCCESS,
            )

        async def run_with_facts(self, req: UserTurnRequest) -> Any:
            from atlasrag.application.rag_core import RagCoreExecution

            return RagCoreExecution(result=await self.run(req), web_invoked=False)

    runner = RetrievalRunner(
        RetrievalGraphServices(cache=cast(Any, cache), rag_core=cast(Any, _SimpleRag()))
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
            # Missing or invalid X-AtlasRAG-Client fails 403 even with invalid body
            missing_csrf = client.post(
                "/api/v1/sessions",
                headers={"Origin": "http://127.0.0.1:8000"},
                json={"bad": "payload"},
            )
            assert missing_csrf.status_code == 403
            assert (
                missing_csrf.json()["error_code"] == TransportErrorCode.FORBIDDEN.value
            )

            # Mismatched body client_kind vs header fails 403
            mismatch = client.post(
                "/api/v1/sessions",
                headers={
                    "Origin": "http://127.0.0.1:8000",
                    "X-AtlasRAG-Client": "cli",
                },
                json={"client_kind": "web"},
            )
            assert mismatch.status_code == 403

            created = client.post(
                "/api/v1/sessions",
                headers={
                    "Origin": "http://127.0.0.1:8000",
                    "X-AtlasRAG-Client": "web",
                },
                json={"client_kind": "web"},
            )
            assert created.status_code == 201
            session_id = created.json()["session_id"]
            assert session_id.startswith("web_")
            assert created.json()["client_kind"] == "web"

            # Execute query then delete session: zero Redis cleanup invoked
            q = client.post(
                "/api/v1/query",
                headers={
                    "Origin": "http://127.0.0.1:8000",
                    "X-AtlasRAG-Client": "web",
                },
                json={"session_id": session_id, "query": "test cache write"},
            )
            assert q.status_code == 200
            assert q.json()["cache_status"] == QueryCacheStatus.MISS.value
            assert spy_redis.puts == 1

            # Unknown client header on DELETE rejected 403
            bad_del_hdr = client.delete(
                f"/api/v1/sessions/{session_id}",
                headers={
                    "Origin": "http://127.0.0.1:8000",
                    "X-AtlasRAG-Client": "cli",
                },
            )
            assert bad_del_hdr.status_code == 403

            deleted = client.delete(
                f"/api/v1/sessions/{session_id}",
                headers={
                    "Origin": "http://127.0.0.1:8000",
                    "X-AtlasRAG-Client": "web",
                },
            )
            assert deleted.status_code == 204
            assert spy_redis.scan_or_delete_calls == 0

            # Repeated delete or query against closed session returns 404
            deleted_again = client.delete(
                f"/api/v1/sessions/{session_id}",
                headers={
                    "Origin": "http://127.0.0.1:8000",
                    "X-AtlasRAG-Client": "web",
                },
            )
            assert deleted_again.status_code == 404
            assert (
                deleted_again.json()["error_code"]
                == TransportErrorCode.SESSION_NOT_FOUND.value
            )

            # Active session before simulated server restart becomes 404 after restart
            s_restart = client.post(
                "/api/v1/sessions",
                headers={
                    "Origin": "http://127.0.0.1:8000",
                    "X-AtlasRAG-Client": "web",
                },
                json={},
            ).json()["session_id"]

        # New app instance simulates server restart with fresh process-local SessionRegistry
        app_restarted = create_app(
            runtime_config=sample_runtime_config(),
            server_config=ServerConfig(),
            coordinator=coordinator,
        )
        with TestClient(app_restarted, base_url="http://127.0.0.1:8000") as client2:
            after_restart = client2.post(
                "/api/v1/query",
                headers={
                    "Origin": "http://127.0.0.1:8000",
                    "X-AtlasRAG-Client": "web",
                },
                json={"session_id": s_restart, "query": "after restart"},
            )
            assert after_restart.status_code == 404
            assert (
                after_restart.json()["error_code"]
                == TransportErrorCode.SESSION_NOT_FOUND.value
            )

            # Reject create/close session cleanly when runtime is SHUTTING_DOWN
            await harness.gate.begin_shutdown()
            shut_create = client2.post(
                "/api/v1/sessions",
                headers={
                    "Origin": "http://127.0.0.1:8000",
                    "X-AtlasRAG-Client": "web",
                },
                json={},
            )
            assert shut_create.status_code == 503
            assert (
                shut_create.json()["error_code"]
                == ErrorCode.RUNTIME_SHUTTING_DOWN.value
            )
            assert query_cache_key("sess", "fp", "h")
            assert sample_response().answer_text
    finally:
        await coordinator.shutdown()
