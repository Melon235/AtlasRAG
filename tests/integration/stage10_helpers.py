"""Isolated Stage 10-B real-infrastructure harness, controlled fakes and TCP fault proxy."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import math
import os
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from pymilvus import MilvusClient  # type: ignore[import-untyped]
from redis.asyncio import Redis

from atlasrag.config.models import RuntimeConfig, RuntimeOperationalConfig
from atlasrag.domain.indexing.models import EmbeddingModelIdentity
from atlasrag.providers.embedding.qwen import (
    QWEN_DIMENSION,
    QWEN_MODEL_ID,
    QWEN_REVISION,
)
from atlasrag.providers.index.schema import COLLECTION_NAME
from atlasrag.providers.reranker.base import RerankerModelIdentity
from atlasrag.providers.reranker.bge import (
    BGE_RERANKER_MODEL_ID,
    BGE_RERANKER_REVISION,
)
from atlasrag.runtime import composition as composition_module
from atlasrag.runtime import ownership as ownership_module
from atlasrag.services.ingestion.chunking import DEFAULT_CHUNKER_BEHAVIOR_VERSION
from integration.conftest import (
    MILVUS_URI_ENV,
    REDIS_URL_ENV,
    _require_loopback_host,
    _required_environment,
)

STAGE10B_REDIS_LEASE_KEY = "atlasrag:test:stage10b:exclusive-lease"


def _normalized_vector(
    text: str, *, dimension: int = QWEN_DIMENSION
) -> tuple[float, ...]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values = [1.0 + (digest[index % len(digest)] / 255.0) for index in range(8)]
    norm = math.sqrt(sum(item * item for item in values))
    unit = [item / norm for item in values]
    return tuple(unit) + (0.0,) * (dimension - len(unit))


class FakeQwenEmbedding:
    """Deterministic 1024-d embedding substitute matching pinned Qwen identity."""

    def __init__(
        self,
        *,
        device: str = "cuda:0",
        fail_init: Callable[[], BaseException | None] | None = None,
    ) -> None:
        self._device = "cuda:0" if device == "cuda" else device
        self._fail_init = fail_init
        self.embed_calls = 0
        self.query_calls = 0
        self.init_calls = 0

    @property
    def identity(self) -> EmbeddingModelIdentity:
        return EmbeddingModelIdentity(
            model_id=QWEN_MODEL_ID,
            model_revision=QWEN_REVISION,
            tokenizer_id=QWEN_MODEL_ID,
            tokenizer_revision=QWEN_REVISION,
            dimension=QWEN_DIMENSION,
            device=self._device,
        )

    async def initialize(self, *, allow_download: bool = False) -> None:
        assert allow_download is False
        self.init_calls += 1
        if self._fail_init is not None:
            error = self._fail_init()
            if error is not None:
                raise error

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        self.embed_calls += 1
        return tuple(_normalized_vector(text) for text in texts)

    async def embed_query(self, query: str) -> tuple[float, ...]:
        self.query_calls += 1
        return _normalized_vector(query)


class FakeBgeReranker:
    """Deterministic cross-encoder substitute matching pinned BGE identity."""

    def __init__(
        self,
        *,
        device: str = "cuda:0",
        fail_init: BaseException | None = None,
        score_value: float = 0.95,
    ) -> None:
        self._device = "cuda:0" if device == "cuda" else device
        self._fail_init = fail_init
        self._score_value = score_value
        self.init_calls = 0
        self.score_calls = 0

    @property
    def identity(self) -> RerankerModelIdentity:
        return RerankerModelIdentity(
            model_id=BGE_RERANKER_MODEL_ID,
            model_revision=BGE_RERANKER_REVISION,
            tokenizer_id=BGE_RERANKER_MODEL_ID,
            tokenizer_revision=BGE_RERANKER_REVISION,
            device=self._device,
            dtype="float16",
            max_input_tokens=8192,
        )

    async def initialize(self, *, allow_download: bool = False) -> None:
        assert allow_download is False
        self.init_calls += 1
        if self._fail_init is not None:
            raise self._fail_init

    async def score(self, query: str, passages: Sequence[str]) -> tuple[float, ...]:
        self.score_calls += 1
        return tuple(self._score_value for _ in passages)


@dataclass
class HttpMockState:
    deepseek_rewrite_calls: int = 0
    deepseek_generation_calls: int = 0
    searxng_probe_calls: int = 0
    searxng_search_calls: int = 0
    searxng_status: int = 200
    answer_text: str = "Grounded answer [L1]."
    rewrite_query: str | None = None
    prompts: list[str] = field(default_factory=list)


def make_http_client_factory(state: HttpMockState) -> Callable[..., httpx.AsyncClient]:
    """Build deterministic DeepSeek/SearXNG HTTP AsyncClients without external IO."""

    orig_async_client = httpx.AsyncClient

    def _handler(request: httpx.Request) -> httpx.Response:
        url_text = str(request.url)
        if "deepseek" in url_text:
            body = json.loads(request.content.decode("utf-8"))
            user_content = body["messages"][1]["content"]
            state.prompts.append(user_content)
            if "UNTRUSTED_EVIDENCE_DATA_JSON" in user_content:
                state.deepseek_generation_calls += 1
                payload = json.dumps(
                    {
                        "answer_text": state.answer_text,
                        "used_citation_ids": ["L1"],
                    }
                )
            else:
                state.deepseek_rewrite_calls += 1
                original_query = json.loads(user_content).get("query", "guide")
                payload = json.dumps(
                    {"retrieval_query": state.rewrite_query or original_query}
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": payload}}]},
            )
        if "/search" in url_text:
            state.searxng_search_calls += 1
            return httpx.Response(state.searxng_status, json={"results": []})
        state.searxng_probe_calls += 1
        return httpx.Response(state.searxng_status, text="ok")

    def _factory(**kwargs: object) -> httpx.AsyncClient:
        timeout = kwargs.get("timeout")
        return orig_async_client(
            transport=httpx.MockTransport(_handler),
            timeout=timeout
            if isinstance(timeout, httpx.Timeout)
            else httpx.Timeout(10.0),
        )

    return _factory


class TcpFaultProxy:
    """Thread-hosted TCP fault proxy compatible with both async and blocking clients."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self._target_host = target_host
        self._target_port = target_port
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._host = "127.0.0.1"
        self._port = 0
        self.mode = "passthrough"
        self.drop_next_match: bytes | None = None
        self.captured_payloads: list[bytes] = []
        self._captured_signal = threading.Event()
        self._active_tasks: set[asyncio.Task[object]] = set()
        self._connections: set[asyncio.StreamWriter] = set()

    @property
    def port(self) -> int:
        if self._port <= 0:
            raise RuntimeError("proxy is not started")
        return self._port

    async def wait_captured(self, timeout: float = 5.0) -> None:
        ok = await asyncio.to_thread(self._captured_signal.wait, timeout)
        if not ok:
            raise TimeoutError("Timed out waiting for proxy packet capture")

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()

    async def _run_on_proxy(self, coro: Any) -> Any:
        assert self._loop is not None
        fut: concurrent.futures.Future[Any] = asyncio.run_coroutine_threadsafe(
            coro, self._loop
        )
        return await asyncio.wrap_future(fut)

    async def _start_server(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self._host,
            port=self._port,
        )
        sockets = self._server.sockets
        assert sockets
        self._port = int(sockets[0].getsockname()[1])

    async def start(self) -> None:
        if self._server is not None:
            return
        if self._thread is None:
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run_loop, name="stage10b-tcp-proxy", daemon=True
            )
            self._thread.start()
            await asyncio.to_thread(self._ready.wait, 5.0)
        await self._run_on_proxy(self._start_server())

    async def _drop_connections_local(self) -> None:
        for writer in list(self._connections):
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        for task in list(self._active_tasks):
            if not task.done():
                task.cancel()
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
        self._active_tasks.clear()
        self._connections.clear()

    async def drop_connections(self) -> None:
        if self._loop is not None and self._loop.is_running():
            await self._run_on_proxy(self._drop_connections_local())

    async def _stop_listening_local(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        await self._drop_connections_local()

    async def stop_listening(self) -> None:
        if self._loop is not None and self._loop.is_running():
            await self._run_on_proxy(self._stop_listening_local())

    async def close(self) -> None:
        await self.stop_listening()
        loop = self._loop
        thread = self._thread
        self._loop = None
        self._thread = None
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            await asyncio.to_thread(thread.join, 5.0)

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._active_tasks.add(current)
        self._connections.add(client_writer)
        server_writer: asyncio.StreamWriter | None = None
        try:
            if self.mode == "refuse":
                return
            server_reader, server_writer = await asyncio.open_connection(
                self._target_host, self._target_port
            )
            self._connections.add(server_writer)

            async def _c2s() -> None:
                assert server_writer is not None
                while True:
                    data = await client_reader.read(65536)
                    if not data:
                        break
                    if (
                        self.mode == "hold_match_and_stall"
                        and self.drop_next_match is not None
                        and self.drop_next_match in data
                    ):
                        self.captured_payloads.append(data)
                        self._captured_signal.set()
                        await asyncio.Future()
                    server_writer.write(data)
                    await server_writer.drain()

            async def _s2c() -> None:
                while True:
                    data = await server_reader.read(65536)
                    if not data:
                        break
                    if self.mode == "stall_all_replies":
                        await asyncio.Future()
                    client_writer.write(data)
                    await client_writer.drain()

            t1 = asyncio.create_task(_c2s())
            t2 = asyncio.create_task(_s2c())
            done, pending = await asyncio.wait(
                {t1, t2}, return_when=asyncio.FIRST_COMPLETED
            )
            for item in pending:
                item.cancel()
            await asyncio.gather(*pending, *done, return_exceptions=True)
        except Exception:
            pass
        finally:
            if server_writer is not None:
                self._connections.discard(server_writer)
                server_writer.close()
                with contextlib.suppress(Exception):
                    await server_writer.wait_closed()
            self._connections.discard(client_writer)
            client_writer.close()
            with contextlib.suppress(Exception):
                await client_writer.wait_closed()
            if current is not None:
                self._active_tasks.discard(current)


async def send_raw_resp_to_redis(host: str, port: int, payload: bytes) -> bytes:
    """Transmit one raw RESP packet to real Redis and read its raw reply."""

    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(payload)
        await writer.drain()
        writer.write_eof()
        return await asyncio.wait_for(reader.read(65536), timeout=5.0)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def parse_stage10_endpoints() -> tuple[
    tuple[str, int, str, str, str], tuple[str, int], tuple[str, int]
]:
    pg_url = urlsplit(_required_environment("ATLASRAG_POSTGRES_DSN"))
    pg_host = _require_loopback_host(pg_url.hostname, "PostgreSQL")
    pg_port = int(pg_url.port or 5432)
    pg_db = pg_url.path.lstrip("/")
    pg_user = pg_url.username or "atlasrag"
    pg_password = pg_url.password or ""

    redis_url = urlsplit(_required_environment(REDIS_URL_ENV))
    redis_host = _require_loopback_host(redis_url.hostname, "Redis")
    redis_port = int(redis_url.port or 6379)

    milvus_url = urlsplit(_required_environment(MILVUS_URI_ENV))
    milvus_host = _require_loopback_host(milvus_url.hostname, "Milvus")
    milvus_port = int(milvus_url.port or 19530)

    return (
        (pg_host, pg_port, pg_db, pg_user, pg_password),
        (redis_host, redis_port),
        (milvus_host, milvus_port),
    )


def build_stage10_runtime_config(
    *,
    postgres_dsn: str,
    knowledge_directory: Path,
    redis_host: str,
    redis_port: int,
    milvus_host: str,
    milvus_port: int,
    model_device: str = "cuda",
    runtime_overrides: dict[str, object] | None = None,
    cache_overrides: dict[str, object] | None = None,
    maintenance_overrides: dict[str, object] | None = None,
) -> RuntimeConfig:
    pg_url = urlsplit(postgres_dsn)
    pg_host = _require_loopback_host(pg_url.hostname, "PostgreSQL")
    pg_port = int(pg_url.port or 5432)
    pg_db = pg_url.path.lstrip("/")
    pg_user = pg_url.username or "atlasrag"
    pg_password = pg_url.password or ""

    runtime_values: dict[str, object] = {
        "startup_attempts": 2,
        "startup_retry_delay_seconds": 0.05,
        "health_probe_timeout_seconds": 3.0,
        "health_refresh_interval_seconds": 10.0,
        "gpu_concurrency": 1,
        "cache_recovery_interval_seconds": 10.0,
        "redis_connect_timeout_seconds": 2.0,
        "redis_socket_timeout_seconds": 2.0,
        "redis_health_check_interval_seconds": 0.0,
        "postgres_pool_min_size": 1,
        "postgres_pool_max_size": 10,
    }
    if runtime_overrides:
        runtime_values.update(runtime_overrides)
    operational = RuntimeOperationalConfig(**runtime_values)  # type: ignore[arg-type]

    return RuntimeConfig.model_validate(
        {
            "infrastructure": {
                "knowledge_directory": str(knowledge_directory),
                "postgres_host": pg_host,
                "postgres_port": pg_port,
                "postgres_database": pg_db,
                "postgres_user": pg_user,
                "postgres_password": pg_password,
                "redis_host": redis_host,
                "redis_port": redis_port,
                "milvus_host": milvus_host,
                "milvus_port": milvus_port,
                "searxng_url": "http://127.0.0.1:18080",
            },
            "models": {
                "embedding_model_id": QWEN_MODEL_ID,
                "embedding_model_revision": QWEN_REVISION,
                "embedding_tokenizer_id": QWEN_MODEL_ID,
                "embedding_tokenizer_revision": QWEN_REVISION,
                "reranker_model_id": BGE_RERANKER_MODEL_ID,
                "reranker_model_revision": BGE_RERANKER_REVISION,
                "query_model_id": "deepseek-chat",
                "query_model_revision": "query-r1",
                "generation_model_id": "deepseek-chat",
                "generation_model_revision": "generation-r1",
                "deepseek_api_key": "stage10b-test-only-key",
                "model_device": model_device,
            },
            "part_i": {
                "loader_behavior_version": "loader-v1",
                "parser_behavior_version": "parser-v1",
                "chunker_behavior_version": DEFAULT_CHUNKER_BEHAVIOR_VERSION,
                "representation_version": "representation-v1",
                "embedding_dimension": QWEN_DIMENSION,
                "bm25_analyzer": "standard",
                "milvus_schema_version": "schema-v1",
                "milvus_index_version": "index-v1",
            },
            "retrieval": {
                "query_construction_version": "query-construction-v1",
                "retrieval_semantics_version": "retrieval-v1",
                "sufficiency_strategy": "evidence-coverage",
                "sufficiency_version": "sufficiency-v1",
                "reranker_strategy": "cross-encoder",
            },
            "answer": {
                "generation_prompt_version": "generation-v1",
                "web_evidence_strategy": "sanitized-content",
                "compression_strategy": "lossless-v1",
                "citation_policy_version": "citation-v1",
                "output_policy_version": "output-v1",
            },
            "cache": {
                "query_cache_enabled": True,
                "parent_cache_enabled": True,
                "query_ttl_seconds": 600,
                "parent_ttl_seconds": 3600,
                **(cache_overrides or {}),
            },
            "maintenance": {
                "max_document_concurrency": 2,
                "drain_timeout_seconds": 5.0,
                "shutdown_timeout_seconds": 5.0,
                **(maintenance_overrides or {}),
            },
            "observability": {
                "trace_sample_rate": 0.0,
                "log_level": "INFO",
                "log_directory": str(knowledge_directory.parent / "logs"),
                "log_queries": False,
            },
            "runtime": operational.model_dump(mode="python"),
        }
    )


def patch_runtime_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    *,
    lock_file: Path,
    embedding_factory: Callable[..., Any],
    reranker_factory: Callable[..., Any],
    http_state: HttpMockState,
) -> None:
    """Patch only model/HTTP/lock-path boundaries while keeping all real DB/Milvus/Redis/orchestration."""

    lock_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(lock_file.parent, 0o700)
    monkeypatch.setattr(ownership_module, "canonical_lock_path", lambda: lock_file)
    monkeypatch.setattr(composition_module, "QwenEmbeddingProvider", embedding_factory)
    monkeypatch.setattr(composition_module, "BgeRerankerProvider", reranker_factory)
    monkeypatch.setattr(httpx, "AsyncClient", make_http_client_factory(http_state))


async def clear_stage10_redis_db0(host: str, port: int) -> None:
    """Clean all keys on isolated Stage 10-B Redis DB 0 via SCAN + UNLINK (never FLUSHDB/FLUSHALL)."""

    client = Redis.from_url(
        f"redis://{host}:{port}/0",
        decode_responses=False,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )
    try:
        assert await client.ping() is True
        cursor = 0
        seen: set[int] = set()
        while True:
            cursor, raw_keys = await client.scan(cursor=cursor, count=200)
            if raw_keys:
                await client.unlink(*raw_keys)
            if cursor == 0:
                break
            if cursor in seen:
                raise RuntimeError("Redis SCAN repeated cursor during cleanup")
            seen.add(cursor)
        assert await client.dbsize() == 0
    finally:
        await client.aclose()


def drop_stage10_milvus_collection(uri: str) -> None:
    client = MilvusClient(uri=uri, timeout=10.0)
    try:
        if client.has_collection(collection_name=COLLECTION_NAME, timeout=10.0):
            client.drop_collection(collection_name=COLLECTION_NAME, timeout=10.0)
    finally:
        client.close()


@contextlib.asynccontextmanager
async def stage10_isolation_guard(
    *,
    redis_host: str,
    redis_port: int,
    milvus_uri: str,
) -> AsyncIterator[None]:
    """Acquire/verify Stage 10-B DB 0 + atlas_chunks cleanup before and after each test."""

    await clear_stage10_redis_db0(redis_host, redis_port)
    await asyncio.to_thread(drop_stage10_milvus_collection, milvus_uri)
    primary: BaseException | None = None
    try:
        yield
    except BaseException as error:
        primary = error
    try:
        await clear_stage10_redis_db0(redis_host, redis_port)
        await asyncio.to_thread(drop_stage10_milvus_collection, milvus_uri)
    except BaseException as cleanup_error:
        if primary is not None:
            primary.add_note(f"Stage 10-B cleanup failure: {cleanup_error!r}")
            raise primary
        raise
    if primary is not None:
        raise primary
