#!/usr/bin/env python3
"""Wait for every Stage 2 local dependency using real capability probes."""

from __future__ import annotations

import asyncio
import math
import os
import re
import sys
import time
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import urlsplit

from pymilvus import MilvusClient  # type: ignore[import-untyped]
from redis.asyncio import Redis

from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.providers.index.milvus import MilvusIndexProvider
from atlasrag.providers.index.models import (
    MILVUS_DENSE_VECTOR_MAX_DIMENSION,
    MILVUS_DENSE_VECTOR_MIN_DIMENSION,
)
from atlasrag.repositories.postgres.pool import PostgresPool

_SERVICE_NAMES = ("postgres", "redis", "milvus", "searxng")
_COLLECTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,254}\Z")
_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    postgres_dsn: str
    redis_url: str
    milvus_uri: str
    milvus_collection: str
    embedding_dimension: int
    searxng_url: str
    wait_timeout_seconds: float
    probe_timeout_seconds: float
    poll_seconds: float


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _positive_float(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive finite number") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def _embedding_dimension() -> int:
    name = "ATLASRAG_EMBEDDING_DIMENSION"
    raw = _required_environment(name)
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if not (
        MILVUS_DENSE_VECTOR_MIN_DIMENSION <= value <= MILVUS_DENSE_VECTOR_MAX_DIMENSION
    ):
        raise ValueError(
            f"{name} must be between "
            f"{MILVUS_DENSE_VECTOR_MIN_DIMENSION} and "
            f"{MILVUS_DENSE_VECTOR_MAX_DIMENSION}"
        )
    return value


def _milvus_collection() -> str:
    name = "ATLASRAG_MILVUS_COLLECTION"
    value = os.environ.get(name, "atlas_chunks").strip()
    if _COLLECTION_NAME.fullmatch(value) is None:
        raise ValueError(f"{name} must be a valid Milvus collection identifier")
    return value


def _http_url(name: str) -> str:
    value = _required_environment(name)
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{name} must be an HTTP URL without user info")
    return value


def config_from_environment() -> ProbeConfig:
    return ProbeConfig(
        postgres_dsn=_required_environment("ATLASRAG_POSTGRES_DSN"),
        redis_url=_required_environment("ATLASRAG_REDIS_URL"),
        milvus_uri=_http_url("ATLASRAG_MILVUS_URI"),
        milvus_collection=_milvus_collection(),
        embedding_dimension=_embedding_dimension(),
        searxng_url=_http_url("ATLASRAG_SEARXNG_URL"),
        wait_timeout_seconds=_positive_float(
            "ATLASRAG_INFRA_WAIT_TIMEOUT_SECONDS", "300"
        ),
        probe_timeout_seconds=_positive_float(
            "ATLASRAG_INFRA_PROBE_TIMEOUT_SECONDS", "5"
        ),
        poll_seconds=_positive_float("ATLASRAG_INFRA_POLL_SECONDS", "1"),
    )


async def _await_settled(task: asyncio.Future[_ResultT]) -> _ResultT:
    """Do not abandon an owned cleanup or constructor after cancellation."""

    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        try:
            task.result()
        except (Exception, asyncio.CancelledError):
            pass
        raise


async def _capture_cleanup(operation: Awaitable[None]) -> BaseException | None:
    task = asyncio.ensure_future(operation)
    try:
        await _await_settled(task)
    except BaseException as error:
        return error
    return None


def _raise_probe_outcome(
    primary_error: BaseException | None,
    cleanup_error: BaseException | None,
    *,
    service: str,
) -> None:
    if isinstance(primary_error, asyncio.CancelledError):
        if cleanup_error is not None:
            primary_error.add_note(
                f"{service} readiness cleanup failure: {type(cleanup_error).__name__}"
            )
        raise primary_error
    if isinstance(cleanup_error, asyncio.CancelledError):
        if primary_error is not None:
            cleanup_error.add_note(
                f"{service} readiness failure before cleanup cancellation: "
                f"{type(primary_error).__name__}"
            )
        raise cleanup_error
    if primary_error is not None:
        if cleanup_error is not None:
            primary_error.add_note(
                f"{service} readiness cleanup failure: {type(cleanup_error).__name__}"
            )
        raise primary_error
    if cleanup_error is not None:
        raise cleanup_error


async def _probe_postgres(config: ProbeConfig) -> None:
    pool = PostgresPool(
        config.postgres_dsn,
        min_size=1,
        max_size=1,
        timeout=config.probe_timeout_seconds,
    )
    primary_error: BaseException | None = None
    try:
        await pool.open()
        await pool.health()
    except BaseException as error:
        primary_error = error

    cleanup_error = await _capture_cleanup(pool.close())
    _raise_probe_outcome(
        primary_error,
        cleanup_error,
        service="PostgreSQL",
    )


async def _probe_redis(config: ProbeConfig) -> None:
    client = Redis.from_url(
        config.redis_url,
        socket_connect_timeout=config.probe_timeout_seconds,
        socket_timeout=config.probe_timeout_seconds,
        decode_responses=False,
    )
    primary_error: BaseException | None = None
    try:
        await RedisCacheProvider(client).health()
    except BaseException as error:
        primary_error = error

    cleanup_error = await _capture_cleanup(client.aclose())
    _raise_probe_outcome(primary_error, cleanup_error, service="Redis")


async def _construct_milvus_client(config: ProbeConfig) -> Any:
    worker = asyncio.create_task(
        asyncio.to_thread(
            MilvusClient,
            uri=config.milvus_uri,
            timeout=config.probe_timeout_seconds,
        )
    )
    try:
        return await _await_settled(worker)
    except asyncio.CancelledError as cancellation:
        try:
            client = worker.result()
        except BaseException as construction_error:
            cancellation.add_note(
                "Milvus client construction after cancellation failed: "
                f"{type(construction_error).__name__}"
            )
        else:
            cleanup_error = await _capture_cleanup(asyncio.to_thread(client.close))
            if cleanup_error is not None:
                cancellation.add_note(
                    f"Milvus readiness cleanup failure: {type(cleanup_error).__name__}"
                )
        raise


async def _probe_milvus(config: ProbeConfig) -> None:
    client = await _construct_milvus_client(config)
    provider: MilvusIndexProvider | None = None
    primary_error: BaseException | None = None
    try:
        provider = MilvusIndexProvider(
            client,
            collection_name=config.milvus_collection,
            embedding_dimension=config.embedding_dimension,
            timeout_seconds=config.probe_timeout_seconds,
        )
        # Readiness includes idempotent adapter initialization so a missing
        # derived collection is created, while incompatibility fails closed.
        await provider.initialize()
        await provider.health()
    except BaseException as error:
        primary_error = error

    if provider is None:
        cleanup = asyncio.to_thread(client.close)
    else:
        cleanup = provider.close()
    cleanup_error = await _capture_cleanup(cleanup)
    _raise_probe_outcome(primary_error, cleanup_error, service="Milvus")


def _request_searxng(url: str, timeout: float) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "AtlasRAG-Stage2-Readiness"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = response.getcode()
        if not 200 <= status < 400:
            raise RuntimeError("SearXNG root returned an unhealthy status")
        response.read(1)


async def _probe_searxng(config: ProbeConfig) -> None:
    await asyncio.to_thread(
        _request_searxng,
        config.searxng_url,
        config.probe_timeout_seconds,
    )


async def _capture_probe(
    probe: Callable[[ProbeConfig], Awaitable[None]],
    config: ProbeConfig,
) -> Exception | None:
    try:
        await probe(config)
    except Exception as error:
        return error
    return None


async def _probe_all(config: ProbeConfig) -> dict[str, Exception | None]:
    probes = (_probe_postgres, _probe_redis, _probe_milvus, _probe_searxng)
    outcomes = await asyncio.gather(
        *(_capture_probe(probe, config) for probe in probes)
    )
    return dict(zip(_SERVICE_NAMES, outcomes, strict=True))


def _status_line(outcomes: dict[str, Exception | None]) -> str:
    return ", ".join(
        f"{name}={'ready' if error is None else type(error).__name__}"
        for name, error in outcomes.items()
    )


async def wait_for_infrastructure(config: ProbeConfig) -> bool:
    deadline = time.monotonic() + config.wait_timeout_seconds
    previous_status: str | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            outcomes = await asyncio.wait_for(_probe_all(config), timeout=remaining)
        except TimeoutError:
            return False
        if time.monotonic() >= deadline:
            return False

        status = _status_line(outcomes)
        if status != previous_status:
            print(f"infrastructure readiness: {status}", flush=True)
            previous_status = status
        if all(error is None for error in outcomes.values()):
            return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(config.poll_seconds, remaining))


def main() -> int:
    try:
        config = config_from_environment()
    except ValueError as error:
        print(f"invalid readiness configuration: {error}", file=sys.stderr)
        return 2

    try:
        ready = asyncio.run(wait_for_infrastructure(config))
    except KeyboardInterrupt:
        return 130
    if not ready:
        print("infrastructure readiness timed out", file=sys.stderr)
        return 1
    print("all Stage 2 infrastructure probes are ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
