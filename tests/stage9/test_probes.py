from __future__ import annotations

import pytest

from benchmarks.environment.probes import (
    milvus_collection_identity,
    postgres_identity,
    redis_identity,
)


class Cursor:
    def __init__(self, row: object) -> None:
        self.row = row

    async def fetchone(self) -> object:
        return self.row


class Connection:
    def __init__(self, row: object) -> None:
        self.row = row
        self.rolled_back = False

    async def execute(self, query: str, params: object = ()) -> Cursor:
        assert query == "SELECT current_database()"
        return Cursor(self.row)

    async def fetchone(self) -> object:
        return self.row

    async def rollback(self) -> None:
        self.rolled_back = True


class Redis:
    def __init__(self, db: int) -> None:
        self.connection_pool = type("Pool", (), {"connection_kwargs": {"db": db}})()

    async def ping(self) -> bool:
        return True


class Milvus:
    def has_collection(self, collection_name: str) -> bool:
        return collection_name == "atlasrag_bench_stage9"


@pytest.mark.asyncio
async def test_postgres_and_redis_identity_probes() -> None:
    connection = Connection(("atlasrag_bench_stage9",))
    assert await postgres_identity(connection) == "atlasrag_bench_stage9"
    assert connection.rolled_back
    assert await redis_identity(Redis(15)) == 15


def test_milvus_collection_identity_probe() -> None:
    assert milvus_collection_identity(Milvus(), "atlasrag_bench_stage9")
    assert not milvus_collection_identity(Milvus(), "production_chunks")
