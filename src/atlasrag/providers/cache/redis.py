"""Async Redis adapter for disposable typed AtlasRAG caches."""

from __future__ import annotations

from collections.abc import Awaitable, Iterator, Sequence
from contextlib import contextmanager
from typing import TypeVar, cast
from uuid import uuid4

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
from atlasrag.providers.cache.keys import (
    QUERY_WRITE_FENCE_KEY,
    parent_cache_key,
    query_cache_key,
    validate_write_fence_input,
)
from atlasrag.providers.cache.models import CachedParentContext, CachedQueryResult

_CacheModelT = TypeVar("_CacheModelT", bound=BaseModel)
_QUERY_PATTERN = "atlasrag:query:*"
_QUERY_PREFIX = "atlasrag:query:"
_GET_FENCED_QUERY_SCRIPT = (
    "return {redis.call('GET', KEYS[2]), redis.call('GET', KEYS[1])}"
)
_PUT_FENCED_QUERY_SCRIPT = (
    "if redis.call('GET', KEYS[2]) ~= ARGV[3] then "
    "return 0 "
    "end "
    "redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2])) "
    "return 1"
)
_PUT_UNFENCED_IF_UNPROTECTED_SCRIPT = (
    "if redis.call('EXISTS', KEYS[2]) == 1 then "
    "return 0 "
    "end "
    "redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2])) "
    "return 1"
)
_ENSURE_QUERY_WRITE_FENCE_SCRIPT = (
    "local current = redis.call('GET', KEYS[1]) "
    "if current then "
    "return current "
    "end "
    "redis.call('SET', KEYS[1], ARGV[1]) "
    "return ARGV[1]"
)


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


def _decode_write_fence_token(value: object) -> str:
    if isinstance(value, bytes):
        try:
            token = value.decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderResponseError from None
    elif isinstance(value, str):
        token = value
    else:
        raise ProviderResponseError
    try:
        validate_unicode_scalar_text(token)
    except ValueError:
        raise ProviderResponseError from None
    if (
        not token
        or len(token) > 128
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in token
        )
    ):
        raise ProviderResponseError
    return token


def _validate_eval_write_fence_result(value: object) -> bool:
    if type(value) is not int or value not in (0, 1):
        raise ProviderResponseError
    return value == 1


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

    async def get_query_write_fence(self) -> str | None:
        raw = await self._get(QUERY_WRITE_FENCE_KEY)
        if raw is None:
            return None
        return _decode_write_fence_token(raw)

    async def revoke_query_write_fence(self) -> int:
        return await self._delete(QUERY_WRITE_FENCE_KEY)

    async def establish_query_write_fence(self) -> str:
        token = uuid4().hex
        with _redis_error_boundary():
            stored = await self._client.set(QUERY_WRITE_FENCE_KEY, token)
        if stored is not True:
            raise ProviderResponseError
        return token

    async def ensure_query_write_fence(self) -> str:
        candidate = uuid4().hex
        with _redis_error_boundary():
            raw = await cast(
                Awaitable[object],
                self._client.eval(
                    _ENSURE_QUERY_WRITE_FENCE_SCRIPT,
                    1,
                    QUERY_WRITE_FENCE_KEY,
                    candidate,
                ),
            )
        return _decode_write_fence_token(raw)

    async def get_fenced_query(
        self,
        session_id: str,
        runtime_fingerprint: str,
        request_hash: str,
    ) -> tuple[str | None, CachedQueryResult | None]:
        key = query_cache_key(session_id, runtime_fingerprint, request_hash)
        with _redis_error_boundary():
            raw = await cast(
                Awaitable[object],
                self._client.eval(
                    _GET_FENCED_QUERY_SCRIPT,
                    2,
                    key,
                    QUERY_WRITE_FENCE_KEY,
                ),
            )
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise ProviderResponseError
        raw_fence, raw_payload = raw
        fence = None if raw_fence is None else _decode_write_fence_token(raw_fence)
        cached = (
            None
            if raw_payload is None
            else _deserialize(CachedQueryResult, raw_payload)
        )
        return fence, cached

    async def put_fenced_query(
        self,
        session_id: str,
        runtime_fingerprint: str,
        request_hash: str,
        value: CachedQueryResult,
        *,
        ttl_seconds: int,
        write_fence: str | None,
    ) -> bool:
        value = _validate_for_write(CachedQueryResult, value)
        ttl = _positive_integer(ttl_seconds)
        key = query_cache_key(session_id, runtime_fingerprint, request_hash)
        payload = value.model_dump_json()
        if write_fence is not None:
            token = validate_write_fence_input(write_fence)
            with _redis_error_boundary():
                result = await cast(
                    Awaitable[object],
                    self._client.eval(
                        _PUT_FENCED_QUERY_SCRIPT,
                        2,
                        key,
                        QUERY_WRITE_FENCE_KEY,
                        payload,
                        str(ttl),
                        token,
                    ),
                )
        else:
            with _redis_error_boundary():
                result = await cast(
                    Awaitable[object],
                    self._client.eval(
                        _PUT_UNFENCED_IF_UNPROTECTED_SCRIPT,
                        2,
                        key,
                        QUERY_WRITE_FENCE_KEY,
                        payload,
                        str(ttl),
                    ),
                )
        return _validate_eval_write_fence_result(result)

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
