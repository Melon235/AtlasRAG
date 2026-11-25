"""Concrete identity probes for isolated benchmark resources."""

from __future__ import annotations

from typing import Protocol


class AsyncConnectionLike(Protocol):
    async def execute(self, query: str, params: object = ...) -> object: ...
    async def fetchone(self) -> object: ...
    async def rollback(self) -> None: ...


class RedisLike(Protocol):
    async def ping(self) -> bool: ...
    @property
    def connection_pool(self) -> object: ...


class MilvusLike(Protocol):
    def has_collection(self, collection_name: str) -> bool: ...


async def postgres_identity(connection: AsyncConnectionLike) -> str:
    try:
        cursor = await connection.execute("SELECT current_database()")
        row = await cursor.fetchone()  # type: ignore[attr-defined]
    finally:
        await connection.rollback()
    if not isinstance(row, (tuple, list)) or not row or not isinstance(row[0], str):
        raise ValueError("PostgreSQL identity query returned no database name")
    return row[0]


async def redis_identity(client: RedisLike) -> int:
    if await client.ping() is not True:
        raise ValueError("Redis ping failed")
    pool = client.connection_pool
    kwargs = getattr(pool, "connection_kwargs", None)
    if not isinstance(kwargs, dict):
        raise ValueError("Redis pool identity is unavailable")
    database = kwargs.get("db", 0)
    if type(database) is not int:
        raise ValueError("Redis database identity is not an integer")
    return database


def milvus_collection_identity(client: MilvusLike, collection: str) -> bool:
    if not collection.strip():
        raise ValueError("Milvus collection name must be nonempty")
    return client.has_collection(collection)
