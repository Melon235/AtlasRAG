"""Async Redis adapter for disposable typed AtlasRAG caches."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from redis.exceptions import DataError as RedisDataError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from atlasrag._canonical import validate_unicode_scalar_text
from atlasrag.domain.errors import (
    DependencyTimeoutError,
    DependencyUnavailableError,
    InvariantViolationError,
    ProviderResponseError,
)
from atlasrag.providers.cache.keys import parent_cache_key, query_cache_key
from atlasrag.providers.cache.models import CachedParentContext, CachedQueryResult

_CacheModelT = TypeVar("_CacheModelT", bound=BaseModel)
_QUERY_PATTERN = "atlasrag:query:*"
_QUERY_PREFIX = "atlasrag:query:"


@contextmanager
def _redis_error_boundary() -> Iterator[None]:
    try:
        yield
    except (RedisTimeoutError, TimeoutError):
        raise DependencyTimeoutError from None
    except RedisDataError:
        raise InvariantViolationError from None
    except (RedisError, OSError):
        raise DependencyUnavailableError from None


def _positive_integer(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise InvariantViolationError
    return value


def _bounded_count(value: object, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ProviderResponseError
    return value


def _deserialize(model: type[_CacheModelT], payload: object) -> _CacheModelT:
    if not isinstance(payload, (bytes, str)):
        raise ProviderResponseError
    try:
        return model.model_validate_json(payload)
    except (ValidationError, UnicodeDecodeError):
        raise ProviderResponseError from None


def _validate_for_write(model: type[_CacheModelT], value: object) -> _CacheModelT:
    if not isinstance(value, model):
        raise InvariantViolationError
    try:
        return model.model_validate(value)
    except ValidationError:
        raise InvariantViolationError from None


def _scan_page(payload: object) -> tuple[int, Sequence[object]]:
    if not isinstance(payload, (list, tuple)) or len(payload) != 2:
        raise ProviderResponseError
    cursor, keys = payload
    if type(cursor) is not int or cursor < 0:
        raise ProviderResponseError
    if not isinstance(keys, (list, tuple)):
        raise ProviderResponseError
    return cursor, keys


def _query_scan_key(value: object) -> str:
    if isinstance(value, bytes):
        try:
            key = value.decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderResponseError from None
    elif isinstance(value, str):
        key = value
    else:
        raise ProviderResponseError
    try:
        validate_unicode_scalar_text(key)
    except ValueError:
        raise ProviderResponseError from None
    if not key.startswith(_QUERY_PREFIX):
        raise ProviderResponseError
    return key


class RedisCacheProvider:
    """Expose technical Parent and Session Query Cache capabilities only."""

    def __init__(self, client: Redis, *, scan_batch_size: int = 100) -> None:
        self._client = client
        self._scan_batch_size = _positive_integer(scan_batch_size)

    async def get_parent(
        self, revision_id: str, parent_id: str
    ) -> CachedParentContext | None:
        payload = await self._get(parent_cache_key(revision_id, parent_id))
        if payload is None:
            return None
        value = _deserialize(CachedParentContext, payload)
        if value.revision_id != revision_id or value.context_id != parent_id:
            raise ProviderResponseError
        return value

    async def put_parent(
        self,
        value: CachedParentContext,
        *,
        ttl_seconds: int,
    ) -> None:
        value = _validate_for_write(CachedParentContext, value)
        await self._put(
            parent_cache_key(value.revision_id, value.context_id),
            value,
            ttl_seconds,
        )

    async def delete_parent(self, revision_id: str, parent_id: str) -> int:
        return await self._delete(parent_cache_key(revision_id, parent_id))

    async def get_query(
        self,
        session_id: str,
        runtime_fingerprint: str,
        request_hash: str,
    ) -> CachedQueryResult | None:
        payload = await self._get(
            query_cache_key(session_id, runtime_fingerprint, request_hash)
        )
        if payload is None:
            return None
        return _deserialize(CachedQueryResult, payload)

    async def put_query(
        self,
        session_id: str,
        runtime_fingerprint: str,
        request_hash: str,
        value: CachedQueryResult,
        *,
        ttl_seconds: int,
    ) -> None:
        value = _validate_for_write(CachedQueryResult, value)
        await self._put(
            query_cache_key(session_id, runtime_fingerprint, request_hash),
            value,
            ttl_seconds,
        )

    async def delete_query(
        self,
        session_id: str,
        runtime_fingerprint: str,
        request_hash: str,
    ) -> int:
        return await self._delete(
            query_cache_key(session_id, runtime_fingerprint, request_hash)
        )

    async def invalidate_queries(self) -> int:
        """Delete only AtlasRAG Query Cache keys through bounded SCAN pages."""

        cursor = 0
        seen_cursors: set[int] = set()
        total_deleted = 0
        while True:
            with _redis_error_boundary():
                raw_page = await self._client.scan(
                    cursor=cursor,
                    match=_QUERY_PATTERN,
                    count=self._scan_batch_size,
                )
            next_cursor, raw_keys = _scan_page(raw_page)
            if next_cursor != 0:
                if next_cursor in seen_cursors:
                    raise ProviderResponseError
                seen_cursors.add(next_cursor)
            keys = tuple(_query_scan_key(key) for key in raw_keys)
            for offset in range(0, len(keys), self._scan_batch_size):
                batch = keys[offset : offset + self._scan_batch_size]
                with _redis_error_boundary():
                    deleted = await self._client.unlink(*batch)
                total_deleted += _bounded_count(deleted, len(batch))
            if next_cursor == 0:
                return total_deleted
            cursor = next_cursor

    async def health(self) -> None:
        with _redis_error_boundary():
            healthy = await self._client.ping()
        if healthy is not True:
            raise ProviderResponseError

    async def _get(self, key: str) -> object:
        with _redis_error_boundary():
            return await self._client.get(key)

    async def _put(self, key: str, value: BaseModel, ttl_seconds: int) -> None:
        ttl = _positive_integer(ttl_seconds)
        payload = value.model_dump_json()
        with _redis_error_boundary():
            stored = await self._client.set(key, payload, ex=ttl)
        if stored is not True:
            raise ProviderResponseError

    async def _delete(self, key: str) -> int:
        with _redis_error_boundary():
            deleted = await self._client.delete(key)
        return _bounded_count(deleted, 1)
