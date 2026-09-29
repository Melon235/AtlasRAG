"""Service-free contracts for typed Redis cache providers."""

from __future__ import annotations

import ast
import asyncio
import inspect
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
)
from redis.exceptions import DataError as RedisDataError
from redis.exceptions import (
    TimeoutError as RedisTimeoutError,
)

from atlasrag.domain.errors import (
    DependencyTimeoutError,
    DependencyUnavailableError,
    InvariantViolationError,
    ProviderResponseError,
)
from atlasrag.providers.cache.keys import parent_cache_key, query_cache_key
from atlasrag.providers.cache.models import CachedParentContext, CachedQueryResult
from atlasrag.providers.cache.redis import RedisCacheProvider

NOW = datetime(2026, 9, 29, 15, 0, tzinfo=UTC)


class FakeRedis:
    """Small async Redis fake with scripted SCAN responses and failures."""

    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.errors: dict[str, BaseException] = {}
        self.set_result: object = True
        self.delete_result: object | None = None
        self.unlink_result: object | None = None
        self.ping_result: object = True
        self.scan_results: list[object] = []
        self.get_calls: list[str] = []
        self.set_calls: list[tuple[str, str, int]] = []
        self.delete_calls: list[tuple[str, ...]] = []
        self.scan_calls: list[tuple[int, str, int]] = []
        self.unlink_calls: list[tuple[str, ...]] = []
        self.ping_calls = 0

    def _raise_configured(self, operation: str) -> None:
        if error := self.errors.get(operation):
            raise error

    async def get(self, name: str) -> object:
        self.get_calls.append(name)
        self._raise_configured("get")
        return self.values.get(name)

    async def set(self, name: str, value: str, *, ex: int) -> object:
        self.set_calls.append((name, value, ex))
        self._raise_configured("set")
        if self.set_result is True:
            self.values[name] = value
        return self.set_result

    async def delete(self, *names: str) -> object:
        self.delete_calls.append(names)
        self._raise_configured("delete")
        if self.delete_result is not None:
            return self.delete_result
        deleted = 0
        for name in names:
            if name in self.values:
                deleted += 1
                del self.values[name]
        return deleted

    async def scan(
        self,
        cursor: int = 0,
        *,
        match: str,
        count: int,
    ) -> object:
        self.scan_calls.append((cursor, match, count))
        self._raise_configured("scan")
        if self.scan_results:
            return self.scan_results.pop(0)
        return (0, [])

    async def unlink(self, *names: str) -> object:
        self.unlink_calls.append(names)
        self._raise_configured("unlink")
        if self.unlink_result is not None:
            return self.unlink_result
        deleted = 0
        for name in names:
            if name in self.values:
                deleted += 1
                del self.values[name]
        return deleted

    async def ping(self) -> object:
        self.ping_calls += 1
        self._raise_configured("ping")
        return self.ping_result


def cached_parent() -> CachedParentContext:
    return CachedParentContext.model_validate(
        {
            "evidence_type": "TEXT_PARENT",
            "context_id": "parent-1",
            "document_id": "doc-1",
            "revision_id": "rev-1",
            "content": "Full canonical parent content. Do not truncate.",
            "provenance": {
                "file_name": "manual.md",
                "relative_source_path": "guides/manual.md",
                "section_path": ["Architecture", "Caching"],
                "source_anchor": {"heading": "Caching"},
            },
        }
    )


def cached_query() -> CachedQueryResult:
    return CachedQueryResult.model_validate(
        {
            "schema_version": 1,
            "created_at": NOW,
            "final_response": {
                "answer_text": "AtlasRAG keeps canonical knowledge in PostgreSQL.",
                "citations": [
                    {
                        "citation_id": "L1",
                        "source_kind": "LOCAL",
                        "file_name": "manual.md",
                        "relative_source_path": "guides/manual.md",
                        "evidence_type": "TEXT_PARENT",
                        "source_anchor": {"heading": "Caching"},
                    }
                ],
            },
        }
    )


def test_cache_keys_are_exact_and_query_key_has_no_knowledge_revision() -> None:
    assert parent_cache_key("rev-1", "parent-1") == ("atlasrag:parent:rev-1:parent-1")
    assert query_cache_key("session-1", "runtime-1", "request-1") == (
        "atlasrag:query:session-1:runtime-1:request-1"
    )
    assert tuple(inspect.signature(query_cache_key).parameters) == (
        "session_id",
        "runtime_fingerprint",
        "request_hash",
    )


@pytest.mark.parametrize("invalid", ["", " ", "bad:key", "bad\nkey", "bad\x7fkey"])
def test_cache_keys_reject_blank_or_control_delimited_components(
    invalid: str,
) -> None:
    with pytest.raises(InvariantViolationError):
        parent_cache_key(invalid, "parent-1")
    with pytest.raises(InvariantViolationError):
        parent_cache_key("rev-1", invalid)
    with pytest.raises(InvariantViolationError):
        query_cache_key(invalid, "runtime-1", "request-1")
    with pytest.raises(InvariantViolationError):
        query_cache_key("session-1", invalid, "request-1")
    with pytest.raises(InvariantViolationError):
        query_cache_key("session-1", "runtime-1", invalid)


def test_cache_models_are_frozen_complete_and_strict() -> None:
    parent = cached_parent()
    query = cached_query()

    assert parent.content == "Full canonical parent content. Do not truncate."
    assert parent.provenance.relative_source_path == "guides/manual.md"
    assert query.schema_version == 1
    assert query.created_at == NOW
    assert query.final_response.citations[0].citation_id == "L1"
    with pytest.raises(ValidationError):
        setattr(parent, "content", "mutated")
    with pytest.raises(ValidationError):
        CachedQueryResult.model_validate(
            {
                **query.model_dump(mode="python"),
                "schema_version": 2,
            }
        )
    with pytest.raises(ValidationError):
        CachedQueryResult.model_validate(
            {
                **query.model_dump(mode="python"),
                "created_at": datetime(2026, 9, 29, 15, 0),
            }
        )


@pytest.mark.asyncio
async def test_parent_cache_round_trips_str_and_bytes_with_ttl_and_delete() -> None:
    client = FakeRedis()
    provider = RedisCacheProvider(cast(Any, client))
    parent = cached_parent()

    await provider.put_parent(parent, ttl_seconds=300)

    key, payload, ttl = client.set_calls[0]
    assert key == "atlasrag:parent:rev-1:parent-1"
    assert ttl == 300
    assert CachedParentContext.model_validate_json(payload) == parent
    assert await provider.get_parent("rev-1", "parent-1") == parent

    client.values[key] = payload.encode("utf-8")
    assert await provider.get_parent("rev-1", "parent-1") == parent
    assert await provider.delete_parent("rev-1", "parent-1") == 1
    assert await provider.get_parent("rev-1", "parent-1") is None


@pytest.mark.asyncio
async def test_query_cache_round_trips_complete_final_response() -> None:
    client = FakeRedis()
    provider = RedisCacheProvider(cast(Any, client))
    query = cached_query()

    await provider.put_query(
        "session-1",
        "runtime-1",
        "request-1",
        query,
        ttl_seconds=45,
    )

    key, payload, ttl = client.set_calls[0]
    assert key == "atlasrag:query:session-1:runtime-1:request-1"
    assert ttl == 45
    assert CachedQueryResult.model_validate_json(payload) == query
    assert await provider.get_query("session-1", "runtime-1", "request-1") == query
    assert await provider.delete_query("session-1", "runtime-1", "request-1") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [b"{not-json", "{}", 123],
)
async def test_corrupt_cache_payload_is_not_a_miss(payload: object) -> None:
    client = FakeRedis()
    key = parent_cache_key("rev-1", "parent-1")
    client.values[key] = payload
    provider = RedisCacheProvider(cast(Any, client))

    with pytest.raises(ProviderResponseError):
        await provider.get_parent("rev-1", "parent-1")

    assert client.delete_calls == []


@pytest.mark.asyncio
async def test_parent_cache_payload_identity_must_match_lookup_key() -> None:
    client = FakeRedis()
    misplaced = cached_parent().model_dump_json()
    client.values[parent_cache_key("rev-other", "parent-other")] = misplaced
    provider = RedisCacheProvider(cast(Any, client))

    with pytest.raises(ProviderResponseError):
        await provider.get_parent("rev-other", "parent-other")


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl", [0, -1, True, 1.5])
async def test_cache_put_rejects_invalid_ttl_before_redis(ttl: object) -> None:
    client = FakeRedis()
    provider = RedisCacheProvider(cast(Any, client))

    with pytest.raises(InvariantViolationError):
        await provider.put_parent(cached_parent(), ttl_seconds=cast(Any, ttl))

    assert client.set_calls == []


@pytest.mark.asyncio
async def test_cache_write_and_delete_validate_driver_results() -> None:
    client = FakeRedis()
    provider = RedisCacheProvider(cast(Any, client))
    client.set_result = False
    with pytest.raises(ProviderResponseError):
        await provider.put_parent(cached_parent(), ttl_seconds=60)

    for invalid_count in (-1, 2, True):
        client.delete_result = invalid_count
        with pytest.raises(ProviderResponseError):
            await provider.delete_parent("rev-1", "parent-1")


@pytest.mark.asyncio
async def test_cache_put_revalidates_forged_models_before_redis() -> None:
    client = FakeRedis()
    provider = RedisCacheProvider(cast(Any, client))
    valid = cached_parent()
    forged = CachedParentContext.model_construct(
        **{
            **valid.model_dump(mode="python"),
            "content": " ",
        }
    )

    with pytest.raises(InvariantViolationError):
        await provider.put_parent(forged, ttl_seconds=60)
    with pytest.raises(InvariantViolationError):
        await provider.put_parent(
            cast(Any, valid.model_dump(mode="python")), ttl_seconds=60
        )

    assert client.set_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("driver_error", "expected_error"),
    [
        (RedisTimeoutError("timeout secret"), DependencyTimeoutError),
        (RedisDataError("local secret"), InvariantViolationError),
        (RedisConnectionError("connection secret"), DependencyUnavailableError),
        (OSError("socket secret"), DependencyUnavailableError),
    ],
)
async def test_redis_failures_are_typed_not_cache_misses(
    driver_error: BaseException,
    expected_error: type[BaseException],
) -> None:
    client = FakeRedis()
    client.errors["get"] = driver_error
    provider = RedisCacheProvider(cast(Any, client))

    with pytest.raises(expected_error) as raised:
        await provider.get_parent("rev-1", "parent-1")

    assert "secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_redis_cache_preserves_cancellation() -> None:
    cancellation = asyncio.CancelledError()
    client = FakeRedis()
    client.errors["get"] = cancellation
    provider = RedisCacheProvider(cast(Any, client))

    with pytest.raises(asyncio.CancelledError) as raised:
        await provider.get_parent("rev-1", "parent-1")

    assert raised.value is cancellation


@pytest.mark.asyncio
async def test_redis_health_requires_a_valid_ping() -> None:
    client = FakeRedis()
    provider = RedisCacheProvider(cast(Any, client))

    await provider.health()
    assert client.ping_calls == 1

    client.ping_result = False
    with pytest.raises(ProviderResponseError):
        await provider.health()

    client.errors["ping"] = RedisTimeoutError("hidden")
    with pytest.raises(DependencyTimeoutError):
        await provider.health()


@pytest.mark.asyncio
async def test_query_invalidation_uses_bounded_scan_and_unlink_only() -> None:
    client = FakeRedis()
    query_keys = [
        "atlasrag:query:s1:r1:q1",
        "atlasrag:query:s2:r2:q2",
        "atlasrag:query:s3:r3:q3",
        "atlasrag:query:s4:r4:q4",
    ]
    parent_key = "atlasrag:parent:rev-1:parent-1"
    unrelated_key = "another-app:key"
    client.values = {key: "value" for key in [*query_keys, parent_key, unrelated_key]}
    client.scan_results = [
        (7, [query_keys[0].encode(), query_keys[1], query_keys[2]]),
        (0, [query_keys[3]]),
    ]
    provider = RedisCacheProvider(cast(Any, client), scan_batch_size=2)

    deleted = await provider.invalidate_queries()

    assert deleted == 4
    assert client.scan_calls == [
        (0, "atlasrag:query:*", 2),
        (7, "atlasrag:query:*", 2),
    ]
    assert client.unlink_calls == [
        (query_keys[0], query_keys[1]),
        (query_keys[2],),
        (query_keys[3],),
    ]
    assert parent_key in client.values
    assert unrelated_key in client.values
    assert all(key not in client.values for key in query_keys)


@pytest.mark.asyncio
async def test_query_invalidation_rejects_unsafe_scan_results() -> None:
    client = FakeRedis()
    client.scan_results = [(0, ["atlasrag:parent:rev-1:parent-1"])]
    provider = RedisCacheProvider(cast(Any, client), scan_batch_size=2)

    with pytest.raises(ProviderResponseError):
        await provider.invalidate_queries()

    assert client.unlink_calls == []


@pytest.mark.asyncio
async def test_query_invalidation_rejects_nonterminating_cursor_cycle() -> None:
    client = FakeRedis()
    client.scan_results = [(7, []), (7, [])]
    provider = RedisCacheProvider(cast(Any, client), scan_batch_size=2)

    with pytest.raises(ProviderResponseError):
        await provider.invalidate_queries()

    assert len(client.scan_calls) == 2


@pytest.mark.asyncio
async def test_query_invalidation_validates_unlink_count() -> None:
    client = FakeRedis()
    client.values["atlasrag:query:s:r:q"] = "value"
    client.scan_results = [(0, ["atlasrag:query:s:r:q"])]
    client.unlink_result = 2
    provider = RedisCacheProvider(cast(Any, client), scan_batch_size=2)

    with pytest.raises(ProviderResponseError):
        await provider.invalidate_queries()


@pytest.mark.asyncio
async def test_query_invalidation_translates_scan_and_unlink_failures() -> None:
    scan_client = FakeRedis()
    scan_client.errors["scan"] = RedisTimeoutError("scan hidden")
    with pytest.raises(DependencyTimeoutError):
        await RedisCacheProvider(cast(Any, scan_client)).invalidate_queries()

    unlink_client = FakeRedis()
    unlink_client.scan_results = [(0, ["atlasrag:query:s:r:q"])]
    unlink_client.errors["unlink"] = RedisConnectionError("unlink hidden")
    with pytest.raises(DependencyUnavailableError):
        await RedisCacheProvider(cast(Any, unlink_client)).invalidate_queries()


def test_redis_provider_has_no_blocking_or_global_delete_commands() -> None:
    root = Path(__file__).resolve().parents[2]
    path = root / "src/atlasrag/providers/cache/redis.py"
    tree = ast.parse(path.read_text(), filename=path.as_posix())
    attributes = {
        node.attr.casefold()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    }

    assert attributes.isdisjoint({"keys", "flushdb", "flushall"})
