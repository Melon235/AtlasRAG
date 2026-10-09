"""Real Redis verification for Stage 2 disposable typed caches."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis

from atlasrag.domain.errors import (
    DependencyUnavailableError,
    ProviderResponseError,
)
from atlasrag.providers.cache.keys import parent_cache_key, query_cache_key
from atlasrag.providers.cache.models import CachedParentContext, CachedQueryResult
from atlasrag.providers.cache.redis import RedisCacheProvider

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOW = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)


def cached_parent(
    revision_id: str,
    context_id: str,
) -> CachedParentContext:
    return CachedParentContext.model_validate(
        {
            "evidence_type": "TEXT_PARENT",
            "context_id": context_id,
            "document_id": "doc-cache",
            "revision_id": revision_id,
            "content": f"canonical context for {revision_id}/{context_id}",
            "provenance": {
                "file_name": "manual.md",
                "relative_source_path": "manuals/cache.md",
                "section_path": ["Architecture", "Cache"],
                "source_anchor": {"heading": "Cache"},
            },
        }
    )


def cached_query(answer_text: str) -> CachedQueryResult:
    return CachedQueryResult.model_validate(
        {
            "schema_version": 1,
            "created_at": NOW,
            "final_response": {
                "answer_text": answer_text,
                "citations": [
                    {
                        "citation_id": "L1",
                        "source_kind": "LOCAL",
                        "file_name": "manual.md",
                        "relative_source_path": "manuals/cache.md",
                        "evidence_type": "TEXT_PARENT",
                        "source_anchor": {"heading": "Cache"},
                    }
                ],
            },
        }
    )


def decoded_keys(raw_keys: list[bytes | str]) -> set[str]:
    return {key.decode("utf-8") if isinstance(key, bytes) else key for key in raw_keys}


async def scanned_keys(client: Redis, pattern: str) -> set[str]:
    cursor = 0
    seen_cursors: set[int] = set()
    keys: set[str] = set()
    for _ in range(100):
        cursor, raw_keys = await client.scan(
            cursor=cursor,
            match=pattern,
            count=100,
        )
        keys.update(decoded_keys(raw_keys))
        if cursor == 0:
            return keys
        if cursor in seen_cursors:
            raise AssertionError("Redis integration SCAN cursor repeated")
        seen_cursors.add(cursor)
    raise AssertionError("Redis integration SCAN exceeded its page bound")


async def test_cache_keys_isolate_parent_revision_and_all_query_dimensions(
    redis_client: Redis,
    redis_cache: RedisCacheProvider,
) -> None:
    parent_one = cached_parent("revision-one", "parent-shared")
    parent_two = cached_parent("revision-two", "parent-shared")
    await redis_cache.put_parent(parent_one, ttl_seconds=60)
    await redis_cache.put_parent(parent_two, ttl_seconds=60)

    query_values = {
        ("session-one", "runtime-one", "request-one"): cached_query("one"),
        ("session-two", "runtime-one", "request-one"): cached_query("session"),
        ("session-one", "runtime-two", "request-one"): cached_query("runtime"),
        ("session-one", "runtime-one", "request-two"): cached_query("request"),
    }
    for dimensions, value in query_values.items():
        await redis_cache.put_query(
            *dimensions,
            value,
            ttl_seconds=60,
        )

    assert await redis_cache.get_parent("revision-one", "parent-shared") == parent_one
    assert await redis_cache.get_parent("revision-two", "parent-shared") == parent_two
    assert await redis_cache.get_parent("revision-three", "parent-shared") is None
    for dimensions, value in query_values.items():
        assert await redis_cache.get_query(*dimensions) == value

    expected_cache_keys = {
        parent_cache_key("revision-one", "parent-shared"),
        parent_cache_key("revision-two", "parent-shared"),
        *(query_cache_key(*dimensions) for dimensions in query_values),
    }
    raw_keys = await scanned_keys(redis_client, "atlasrag:*")
    assert raw_keys == {
        "atlasrag:test:exclusive-lease",
        *expected_cache_keys,
    }


async def test_cache_ttl_expires_in_real_redis(
    redis_cache: RedisCacheProvider,
) -> None:
    parent = cached_parent("revision-ttl", "parent-ttl")
    await redis_cache.put_parent(parent, ttl_seconds=1)
    assert await redis_cache.get_parent("revision-ttl", "parent-ttl") == parent

    deadline = asyncio.get_running_loop().time() + 3.0
    while asyncio.get_running_loop().time() < deadline:
        if await redis_cache.get_parent("revision-ttl", "parent-ttl") is None:
            break
        await asyncio.sleep(0.05)

    assert await redis_cache.get_parent("revision-ttl", "parent-ttl") is None


async def test_malformed_payload_is_rejected_and_preserved(
    redis_client: Redis,
    redis_cache: RedisCacheProvider,
) -> None:
    key = parent_cache_key("revision-corrupt", "parent-corrupt")
    malformed = b"{not-json"
    assert await redis_client.set(key, malformed, ex=60)

    with pytest.raises(ProviderResponseError):
        await redis_cache.get_parent("revision-corrupt", "parent-corrupt")

    assert await redis_client.get(key) == malformed


async def test_query_invalidation_preserves_parent_and_unrelated_keys(
    redis_client: Redis,
    redis_cache: RedisCacheProvider,
) -> None:
    parent = cached_parent("revision-preserved", "parent-preserved")
    await redis_cache.put_parent(parent, ttl_seconds=60)
    query_dimensions = (
        ("session-one", "runtime-one", "request-one"),
        ("session-two", "runtime-one", "request-one"),
        ("session-one", "runtime-two", "request-two"),
    )
    for index, dimensions in enumerate(query_dimensions, start=1):
        await redis_cache.put_query(
            *dimensions,
            cached_query(f"answer {index}"),
            ttl_seconds=60,
        )
    assert await redis_client.set("unrelated:key", "keep", ex=60)

    assert await redis_cache.invalidate_queries() == len(query_dimensions)

    assert (
        await redis_cache.get_parent("revision-preserved", "parent-preserved") == parent
    )
    assert await redis_client.get("unrelated:key") == b"keep"
    for dimensions in query_dimensions:
        assert await redis_cache.get_query(*dimensions) is None


async def test_unavailable_redis_is_a_typed_dependency_failure() -> None:
    client = Redis.from_url(
        "redis://127.0.0.1:1/15",
        socket_connect_timeout=0.05,
        socket_timeout=0.05,
    )
    try:
        provider = RedisCacheProvider(client)
        with pytest.raises(DependencyUnavailableError):
            await provider.health()
    finally:
        await client.aclose()
