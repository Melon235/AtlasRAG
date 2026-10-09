"""Service-free contracts for PostgreSQL repositories and transactions."""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from psycopg import (
    DataError,
    Error,
    IntegrityError,
    IsolationLevel,
    OperationalError,
    ProgrammingError,
    errors,
)
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout
from pydantic import ValidationError

from atlasrag.domain.errors import (
    CanonicalDataError,
    ConfigurationError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    InvariantViolationError,
)
from atlasrag.repositories.postgres.records import (
    StoredChunk,
    StoredDocument,
    StoredElement,
)

NOW = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)


class FakeCursor:
    def __init__(self, rows: list[object] | None = None, *, rowcount: int = 0) -> None:
        self.rows = list(rows or [])
        self.rowcount = rowcount

    async def fetchone(self) -> object | None:
        if not self.rows:
            return None
        return self.rows.pop(0)

    async def fetchall(self) -> list[object]:
        rows = self.rows
        self.rows = []
        return rows


class FakeConnection:
    """Small async connection fake used by pool and Unit-of-Work tests."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.isolation_levels: list[IsolationLevel] = []
        self.execute_calls: list[tuple[str, object]] = []
        self.execute_error: BaseException | None = None
        self.execute_failures: dict[int, BaseException] = {}
        self.commit_error: BaseException | None = None
        self.rollback_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.cursor_results: list[FakeCursor] = []
        self.info = FakeConnectionInfo()

    async def set_isolation_level(self, level: IsolationLevel) -> None:
        self.isolation_levels.append(level)

    async def commit(self) -> None:
        self.commits += 1
        if self.commit_error is not None:
            self.info.transaction_status = TransactionStatus.UNKNOWN
            raise self.commit_error
        # PostgreSQL accepts COMMIT while aborted as ROLLBACK. The UoW must
        # inspect status first rather than treating this normal return as success.
        self.info.transaction_status = TransactionStatus.IDLE

    async def rollback(self) -> None:
        self.rollbacks += 1
        if self.rollback_error is not None:
            self.info.transaction_status = TransactionStatus.UNKNOWN
            raise self.rollback_error
        self.info.transaction_status = TransactionStatus.IDLE

    async def close(self) -> None:
        self.closes += 1
        self.info.transaction_status = TransactionStatus.UNKNOWN
        if self.close_error is not None:
            raise self.close_error

    async def execute(self, query: str, params: object = None) -> FakeCursor:
        self.execute_calls.append((query, params))
        if self.execute_error is not None:
            self._set_failed_status(self.execute_error)
            raise self.execute_error
        failure = self.execute_failures.get(len(self.execute_calls))
        if failure is not None:
            self._set_failed_status(failure)
            raise failure
        self.info.transaction_status = TransactionStatus.INTRANS
        if self.cursor_results:
            return self.cursor_results.pop(0)
        return FakeCursor()

    def queue_rows(self, *rows: object, rowcount: int = 0) -> None:
        self.cursor_results.append(FakeCursor(list(rows), rowcount=rowcount))

    def _set_failed_status(self, error: BaseException) -> None:
        self.info.transaction_status = (
            TransactionStatus.INERROR
            if isinstance(error, (IntegrityError, DataError, ProgrammingError))
            else TransactionStatus.UNKNOWN
            if isinstance(error, (Error, OSError))
            else self.info.transaction_status
        )


class FakeConnectionInfo:
    def __init__(self) -> None:
        self.transaction_status = TransactionStatus.IDLE


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.getconn_calls = 0
        self.putconn_calls: list[FakeConnection] = []
        self.getconn_error: BaseException | None = None
        self.putconn_error: BaseException | None = None
        self.open_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.open_calls: list[tuple[bool, float]] = []
        self.close_calls = 0

    async def getconn(self) -> FakeConnection:
        self.getconn_calls += 1
        if self.getconn_error is not None:
            raise self.getconn_error
        return self.connection

    async def putconn(self, connection: FakeConnection) -> None:
        self.putconn_calls.append(connection)
        if self.putconn_error is not None:
            raise self.putconn_error

    async def open(self, *, wait: bool, timeout: float = 30.0) -> None:
        self.open_calls.append((wait, timeout))
        if self.open_error is not None:
            raise self.open_error

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def stored_document(
    *,
    document_id: str = "doc-1",
    relative_source_path: str = "guides/manual.md",
) -> StoredDocument:
    return StoredDocument.model_validate(
        {
            "document_id": document_id,
            "file_name": "manual.md",
            "relative_source_path": relative_source_path,
            "source_type": "MD",
            "observed_content_hash": "observed-hash",
            "current_revision_id": "rev-current",
            "current_content_hash": "current-hash",
            "current_pipeline_fingerprint": "pipeline-v1",
            "ingestion_status": "READY",
            "parse_metadata": {"parser": {"name": "local", "pages": [1, 2]}},
            "created_at": NOW,
            "updated_at": NOW,
        }
    )


def document_row(document: StoredDocument) -> dict[str, object]:
    row = document.model_dump(mode="json")
    row["created_at"] = document.created_at
    row["updated_at"] = document.updated_at
    return row


def stored_element(
    *,
    element_id: str = "element-1",
    document_id: str = "doc-1",
    revision_id: str = "rev-1",
    order_index: int = 0,
) -> StoredElement:
    return StoredElement.model_validate(
        {
            "element_id": element_id,
            "document_id": document_id,
            "revision_id": revision_id,
            "element_type": "PARAGRAPH",
            "order_index": order_index,
            "content": f"content for {element_id}",
            "section_path": ("Guide",),
            "source_anchor": {"page_number": order_index + 1},
            "structured_content": {"ordinal": order_index},
            "metadata": {"parser": "local"},
            "created_at": NOW,
        }
    )


def element_row(element: StoredElement) -> dict[str, object]:
    row = element.model_dump(mode="json")
    row["created_at"] = element.created_at
    return row


def stored_chunk(
    *,
    chunk_id: str = "parent-1",
    document_id: str = "doc-1",
    revision_id: str = "rev-1",
    chunk_type: str = "TEXT_PARENT",
    parent_id: str | None = None,
) -> StoredChunk:
    return StoredChunk.model_validate(
        {
            "chunk_id": chunk_id,
            "document_id": document_id,
            "revision_id": revision_id,
            "chunk_type": chunk_type,
            "parent_id": parent_id,
            "content": f"content for {chunk_id}",
            "section_path": ("Guide",),
            "source_anchor": {"page_number": 1},
            "sheet_name": "Sheet1" if chunk_type == "TABLE" else None,
            "metadata": {"canonical": True},
            "strategy_metadata": {"strategy": "semantic-v1"},
            "created_at": NOW,
        }
    )


def chunk_row(chunk: StoredChunk) -> dict[str, object]:
    row = chunk.model_dump(mode="json")
    row["created_at"] = chunk.created_at
    return row


def normalized_sql(query: str) -> str:
    return " ".join(query.split())


@pytest.mark.asyncio
async def test_pool_has_explicit_lifecycle_and_canonical_connection_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlasrag.repositories.postgres import pool as pool_module

    captured: dict[str, object] = {}

    class FakeDriverPool:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.open_calls: list[tuple[bool, float]] = []
            self.close_calls = 0

        async def open(self, *, wait: bool, timeout: float) -> None:
            self.open_calls.append((wait, timeout))

        async def close(self) -> None:
            self.close_calls += 1

    monkeypatch.setattr(pool_module, "AsyncConnectionPool", FakeDriverPool)
    postgres_pool = pool_module.PostgresPool(
        "postgresql://user:secret@db/atlasrag", timeout=12.5
    )

    assert captured["open"] is False
    assert captured["kwargs"] == {"autocommit": False, "row_factory": dict_row}
    assert callable(captured["configure"])
    configure = cast(Callable[[Any], Awaitable[None]], captured["configure"])
    configured_connection = FakeConnection()
    await configure(configured_connection)
    assert configured_connection.isolation_levels == [IsolationLevel.READ_COMMITTED]

    await postgres_pool.open()
    await postgres_pool.close()

    driver_pool = cast(Any, postgres_pool._pool)
    assert driver_pool.open_calls == [(True, 12.5)]
    assert driver_pool.close_calls == 1


@pytest.mark.asyncio
async def test_uncommitted_uow_rolls_back_and_returns_one_shared_connection() -> None:
    from atlasrag.repositories.postgres.uow import PostgresUnitOfWork

    connection = FakeConnection()
    pool = FakePool(connection)

    async with PostgresUnitOfWork(cast(Any, pool)) as uow:
        assert cast(Any, uow.documents._connection) is connection
        assert cast(Any, uow.elements._connection) is connection
        assert cast(Any, uow.chunks._connection) is connection
        assert cast(Any, uow.runtime_metadata._connection) is connection

    assert pool.getconn_calls == 1
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert pool.putconn_calls == [connection]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("driver_error", "expected_error"),
    [
        (PoolTimeout("pool timed out"), DependencyTimeoutError),
        (errors.CancellationTimeout("cancel timed out"), DependencyTimeoutError),
        (errors.ConnectionTimeout("connect timed out"), DependencyTimeoutError),
        (
            errors.IdleInTransactionSessionTimeout("idle transaction timed out"),
            DependencyTimeoutError,
        ),
        (errors.IdleSessionTimeout("idle session timed out"), DependencyTimeoutError),
        (errors.TransactionTimeout("transaction timed out"), DependencyTimeoutError),
        (OperationalError("connection includes secret"), DependencyUnavailableError),
    ],
)
async def test_pool_open_translates_driver_failures_without_leaking_details(
    monkeypatch: pytest.MonkeyPatch,
    driver_error: BaseException,
    expected_error: type[BaseException],
) -> None:
    from atlasrag.repositories.postgres import pool as pool_module

    driver_pool = FakePool(FakeConnection())
    driver_pool.open_error = driver_error
    monkeypatch.setattr(
        pool_module, "AsyncConnectionPool", lambda **_kwargs: driver_pool
    )

    with pytest.raises(expected_error) as raised:
        await pool_module.PostgresPool("postgresql://secret@db/atlasrag").open()

    assert "secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_pool_preserves_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    from atlasrag.repositories.postgres import pool as pool_module

    cancellation = asyncio.CancelledError()
    driver_pool = FakePool(FakeConnection())
    driver_pool.open_error = cancellation
    monkeypatch.setattr(
        pool_module, "AsyncConnectionPool", lambda **_kwargs: driver_pool
    )

    with pytest.raises(asyncio.CancelledError) as raised:
        await pool_module.PostgresPool("postgresql://db/atlasrag").open()

    assert raised.value is cancellation


@pytest.mark.asyncio
async def test_pool_health_rolls_back_probe_transaction_and_returns_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlasrag.repositories.postgres import pool as pool_module

    connection = FakeConnection()
    driver_pool = FakePool(connection)
    monkeypatch.setattr(
        pool_module, "AsyncConnectionPool", lambda **_kwargs: driver_pool
    )

    await pool_module.PostgresPool("postgresql://db/atlasrag").health()

    assert connection.execute_calls == [("SELECT 1", None)]
    assert connection.rollbacks == 1
    assert driver_pool.putconn_calls == [connection]


@pytest.mark.asyncio
async def test_pool_health_translates_query_failure_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlasrag.repositories.postgres import pool as pool_module

    connection = FakeConnection()
    connection.execute_error = OperationalError("server secret detail")
    driver_pool = FakePool(connection)
    monkeypatch.setattr(
        pool_module, "AsyncConnectionPool", lambda **_kwargs: driver_pool
    )

    with pytest.raises(DependencyUnavailableError) as raised:
        await pool_module.PostgresPool("postgresql://db/atlasrag").health()

    assert "secret" not in str(raised.value)
    assert connection.rollbacks == 1
    assert driver_pool.putconn_calls == [connection]


@pytest.mark.asyncio
async def test_pool_health_closes_connection_when_transaction_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlasrag.repositories.postgres import pool as pool_module

    connection = FakeConnection()
    connection.rollback_error = OperationalError("rollback failed")
    driver_pool = FakePool(connection)
    monkeypatch.setattr(
        pool_module, "AsyncConnectionPool", lambda **_kwargs: driver_pool
    )

    with pytest.raises(DependencyUnavailableError):
        await pool_module.PostgresPool("postgresql://db/atlasrag").health()

    assert connection.closes == 1
    assert driver_pool.putconn_calls == [connection]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query_error", "expected_error"),
    [
        (asyncio.CancelledError(), asyncio.CancelledError),
        (ProgrammingError("query hidden"), ConfigurationError),
    ],
)
async def test_pool_health_preserves_primary_error_when_cleanup_also_fails(
    monkeypatch: pytest.MonkeyPatch,
    query_error: BaseException,
    expected_error: type[BaseException],
) -> None:
    from atlasrag.repositories.postgres import pool as pool_module

    connection = FakeConnection()
    connection.execute_error = query_error
    connection.rollback_error = OperationalError("rollback hidden")
    connection.close_error = OperationalError("close hidden")
    driver_pool = FakePool(connection)
    driver_pool.putconn_error = OperationalError("return hidden")
    monkeypatch.setattr(
        pool_module, "AsyncConnectionPool", lambda **_kwargs: driver_pool
    )

    with pytest.raises(expected_error) as raised:
        await pool_module.PostgresPool("postgresql://db/atlasrag").health()

    if isinstance(query_error, asyncio.CancelledError):
        assert raised.value is query_error
    assert connection.rollbacks == 1
    assert connection.closes >= 1
    assert driver_pool.putconn_calls == [connection]
    assert any("cleanup failure" in note.lower() for note in raised.value.__notes__)


@pytest.mark.asyncio
async def test_pool_getconn_and_putconn_translate_pool_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlasrag.repositories.postgres import pool as pool_module

    connection = FakeConnection()
    driver_pool = FakePool(connection)
    monkeypatch.setattr(
        pool_module, "AsyncConnectionPool", lambda **_kwargs: driver_pool
    )
    postgres_pool = pool_module.PostgresPool("postgresql://db/atlasrag")

    driver_pool.getconn_error = PoolTimeout("hidden")
    with pytest.raises(DependencyTimeoutError):
        await postgres_pool.getconn()

    driver_pool.getconn_error = None
    assert cast(Any, await postgres_pool.getconn()) is connection

    driver_pool.putconn_error = OperationalError("hidden")
    with pytest.raises(DependencyUnavailableError):
        await postgres_pool.putconn(cast(Any, connection))


@pytest.mark.asyncio
async def test_document_put_is_parameterized_jsonb_upsert_without_commit() -> None:
    from atlasrag.repositories.postgres.documents import DocumentRepository

    connection = FakeConnection()
    repository = DocumentRepository(cast(Any, connection))
    document = stored_document()

    await repository.put(document)

    assert len(connection.execute_calls) == 1
    query, raw_params = connection.execute_calls[0]
    sql = normalized_sql(query)
    assert "INSERT INTO documents" in sql
    assert "ON CONFLICT (document_id) DO UPDATE" in sql
    assert "created_at = EXCLUDED.created_at" not in sql
    assert "%s" in sql
    assert document.document_id not in query
    params = cast(tuple[object, ...], raw_params)
    assert params[0] == document.document_id
    assert isinstance(params[13], Jsonb)
    assert params[13].obj == {"parser": {"name": "local", "pages": [1, 2]}}
    assert connection.commits == 0


@pytest.mark.asyncio
async def test_document_get_by_id_distinguishes_valid_absence_from_failure() -> None:
    from atlasrag.repositories.postgres.documents import DocumentRepository

    connection = FakeConnection()
    repository = DocumentRepository(cast(Any, connection))
    document = stored_document()
    connection.queue_rows(document_row(document))
    connection.queue_rows()

    assert await repository.get_by_id("doc-1") == document
    assert await repository.get_by_id("missing") is None
    assert connection.execute_calls[0][1] == ("doc-1",)
    assert "WHERE document_id = %s" in normalized_sql(connection.execute_calls[0][0])


@pytest.mark.asyncio
async def test_document_get_by_path_and_list_are_deterministic_typed_reads() -> None:
    from atlasrag.repositories.postgres.documents import DocumentRepository

    connection = FakeConnection()
    repository = DocumentRepository(cast(Any, connection))
    first = stored_document()
    second = stored_document(
        document_id="doc-2", relative_source_path="reference/second.md"
    )
    connection.queue_rows(document_row(first))
    connection.queue_rows(document_row(first), document_row(second))

    assert await repository.get_by_relative_source_path("guides/manual.md") == first
    assert await repository.list() == (first, second)
    path_sql, path_params = connection.execute_calls[0]
    assert "WHERE relative_source_path = %s" in normalized_sql(path_sql)
    assert path_params == ("guides/manual.md",)
    list_sql, list_params = connection.execute_calls[1]
    assert "ORDER BY relative_source_path, document_id" in normalized_sql(list_sql)
    assert list_params is None


@pytest.mark.asyncio
async def test_document_invalid_row_is_canonical_data_error() -> None:
    from atlasrag.repositories.postgres.documents import DocumentRepository

    connection = FakeConnection()
    connection.queue_rows({"document_id": "doc-1"})
    repository = DocumentRepository(cast(Any, connection))

    with pytest.raises(CanonicalDataError):
        await repository.get_by_id("doc-1")


@pytest.mark.asyncio
async def test_document_driver_failure_is_not_a_legal_absence() -> None:
    from atlasrag.repositories.postgres.documents import DocumentRepository

    connection = FakeConnection()
    connection.execute_error = OperationalError("contains secret parameters")
    repository = DocumentRepository(cast(Any, connection))

    with pytest.raises(DependencyUnavailableError) as raised:
        await repository.get_by_id("doc-1")

    assert "secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_runtime_metadata_get_requires_valid_singleton_row() -> None:
    from atlasrag.repositories.postgres.runtime_metadata import (
        RuntimeMetadataRepository,
    )

    connection = FakeConnection()
    connection.queue_rows({"query_cache_invalidation_required": False})
    connection.queue_rows()
    repository = RuntimeMetadataRepository(cast(Any, connection))

    metadata = await repository.get()
    assert metadata.query_cache_invalidation_required is False
    with pytest.raises(CanonicalDataError):
        await repository.get()


@pytest.mark.asyncio
async def test_runtime_metadata_set_is_strict_parameterized_and_no_commit() -> None:
    from atlasrag.repositories.postgres.runtime_metadata import (
        RuntimeMetadataRepository,
    )

    connection = FakeConnection()
    connection.queue_rows({"query_cache_invalidation_required": True})
    repository = RuntimeMetadataRepository(cast(Any, connection))

    updated = await repository.set_query_cache_invalidation_required(True)

    assert updated.query_cache_invalidation_required is True
    query, params = connection.execute_calls[0]
    assert "UPDATE runtime_metadata" in normalized_sql(query)
    assert "WHERE metadata_key = %s" in normalized_sql(query)
    assert params == (True, "runtime")
    assert connection.commits == 0

    with pytest.raises(InvariantViolationError):
        await repository.set_query_cache_invalidation_required(cast(Any, 1))
    assert len(connection.execute_calls) == 1


@pytest.mark.asyncio
async def test_element_replace_validates_then_deletes_and_inserts_exact_set() -> None:
    from atlasrag.repositories.postgres.elements import ElementRepository

    connection = FakeConnection()
    repository = ElementRepository(cast(Any, connection))
    first = stored_element()
    second = stored_element(element_id="element-2", order_index=1)

    await repository.replace_revision_set("doc-1", "rev-1", (second, first))

    assert len(connection.execute_calls) == 3
    delete_sql, delete_params = connection.execute_calls[0]
    assert "DELETE FROM document_elements" in normalized_sql(delete_sql)
    assert delete_params == ("doc-1", "rev-1")
    inserted_ids: list[object] = []
    for query, raw_params in connection.execute_calls[1:]:
        assert "INSERT INTO document_elements" in normalized_sql(query)
        params = cast(tuple[object, ...], raw_params)
        inserted_ids.append(params[0])
        assert params[1:3] == ("doc-1", "rev-1")
        for index in (6, 7, 8, 9):
            assert isinstance(params[index], Jsonb)
    assert inserted_ids == ["element-1", "element-2"]
    assert connection.commits == 0


@pytest.mark.asyncio
async def test_element_empty_replace_only_deletes_revision() -> None:
    from atlasrag.repositories.postgres.elements import ElementRepository

    connection = FakeConnection()
    repository = ElementRepository(cast(Any, connection))

    await repository.replace_revision_set("doc-1", "rev-1", ())

    assert len(connection.execute_calls) == 1
    assert "DELETE FROM document_elements" in normalized_sql(
        connection.execute_calls[0][0]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["duplicate", "document", "revision"])
async def test_element_replace_rejects_invalid_set_before_delete(
    invalid_kind: str,
) -> None:
    from atlasrag.repositories.postgres.elements import ElementRepository

    connection = FakeConnection()
    repository = ElementRepository(cast(Any, connection))
    first = stored_element()
    if invalid_kind == "duplicate":
        records = (first, first)
    elif invalid_kind == "document":
        records = (first, stored_element(element_id="element-2", document_id="doc-2"))
    else:
        records = (first, stored_element(element_id="element-2", revision_id="rev-2"))

    with pytest.raises(InvariantViolationError):
        await repository.replace_revision_set("doc-1", "rev-1", records)

    assert connection.execute_calls == []


@pytest.mark.asyncio
async def test_element_revision_reads_are_typed_and_deterministic() -> None:
    from atlasrag.repositories.postgres.elements import ElementRepository

    connection = FakeConnection()
    repository = ElementRepository(cast(Any, connection))
    first = stored_element()
    second = stored_element(element_id="element-2", order_index=1)
    connection.queue_rows({"record_count": 2})
    connection.queue_rows({"element_id": "element-1"}, {"element_id": "element-2"})
    connection.queue_rows(element_row(first), element_row(second))
    connection.queue_rows(rowcount=2)

    assert await repository.count_revision("doc-1", "rev-1") == 2
    assert await repository.list_revision_ids("doc-1", "rev-1") == (
        "element-1",
        "element-2",
    )
    assert await repository.list_for_revision("doc-1", "rev-1") == (first, second)
    assert await repository.delete_revision("doc-1", "rev-1") == 2

    assert "COUNT(*) AS record_count" in normalized_sql(connection.execute_calls[0][0])
    assert "SELECT element_id" in normalized_sql(connection.execute_calls[1][0])
    assert "ORDER BY element_id" in normalized_sql(connection.execute_calls[1][0])
    assert "ORDER BY order_index, element_id" in normalized_sql(
        connection.execute_calls[2][0]
    )
    assert "DELETE FROM document_elements" in normalized_sql(
        connection.execute_calls[3][0]
    )
    assert all(
        call[1] == ("doc-1", "rev-1")
        for call in (
            connection.execute_calls[0],
            connection.execute_calls[2],
            connection.execute_calls[3],
        )
    )


@pytest.mark.asyncio
async def test_element_invalid_read_and_partial_write_failure_are_typed() -> None:
    from atlasrag.repositories.postgres.elements import ElementRepository

    invalid_read = FakeConnection()
    invalid_read.queue_rows({"element_id": "broken"})
    repository = ElementRepository(cast(Any, invalid_read))
    with pytest.raises(CanonicalDataError):
        await repository.list_for_revision("doc-1", "rev-1")

    failed_write = FakeConnection()
    failed_write.execute_failures[3] = OperationalError("hidden server failure")
    repository = ElementRepository(cast(Any, failed_write))
    with pytest.raises(DependencyUnavailableError):
        await repository.replace_revision_set(
            "doc-1",
            "rev-1",
            (
                stored_element(),
                stored_element(element_id="element-2", order_index=1),
            ),
        )
    assert len(failed_write.execute_calls) == 3
    assert failed_write.commits == 0


@pytest.mark.asyncio
async def test_chunk_replace_validates_then_deletes_and_inserts_exact_set() -> None:
    from atlasrag.repositories.postgres.chunks import ChunkRepository

    connection = FakeConnection()
    repository = ChunkRepository(cast(Any, connection))
    parent = stored_chunk()
    child = stored_chunk(
        chunk_id="child-1", chunk_type="TEXT_CHILD", parent_id="parent-1"
    )

    await repository.replace_revision_set("doc-1", "rev-1", (parent, child))

    assert len(connection.execute_calls) == 3
    delete_sql, delete_params = connection.execute_calls[0]
    assert "DELETE FROM chunks" in normalized_sql(delete_sql)
    assert delete_params == ("doc-1", "rev-1")
    inserted_ids: list[object] = []
    for query, raw_params in connection.execute_calls[1:]:
        assert "INSERT INTO chunks" in normalized_sql(query)
        params = cast(tuple[object, ...], raw_params)
        inserted_ids.append(params[0])
        assert params[1:3] == ("doc-1", "rev-1")
        for index in (6, 7, 9, 10):
            assert isinstance(params[index], Jsonb)
    assert inserted_ids == ["child-1", "parent-1"]
    assert connection.commits == 0


@pytest.mark.asyncio
async def test_chunk_empty_replace_only_deletes_revision() -> None:
    from atlasrag.repositories.postgres.chunks import ChunkRepository

    connection = FakeConnection()
    repository = ChunkRepository(cast(Any, connection))

    await repository.replace_revision_set("doc-1", "rev-1", ())

    assert len(connection.execute_calls) == 1
    assert "DELETE FROM chunks" in normalized_sql(connection.execute_calls[0][0])


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["duplicate", "document", "revision"])
async def test_chunk_replace_rejects_invalid_set_before_delete(
    invalid_kind: str,
) -> None:
    from atlasrag.repositories.postgres.chunks import ChunkRepository

    connection = FakeConnection()
    repository = ChunkRepository(cast(Any, connection))
    first = stored_chunk()
    if invalid_kind == "duplicate":
        records = (first, first)
    elif invalid_kind == "document":
        records = (
            first,
            stored_chunk(chunk_id="table-2", document_id="doc-2", chunk_type="TABLE"),
        )
    else:
        records = (
            first,
            stored_chunk(chunk_id="table-2", revision_id="rev-2", chunk_type="TABLE"),
        )

    with pytest.raises(InvariantViolationError):
        await repository.replace_revision_set("doc-1", "rev-1", records)

    assert connection.execute_calls == []


@pytest.mark.asyncio
async def test_chunk_revision_primitives_and_identity_lookup_are_typed() -> None:
    from atlasrag.repositories.postgres.chunks import ChunkRepository

    connection = FakeConnection()
    repository = ChunkRepository(cast(Any, connection))
    parent = stored_chunk()
    table = stored_chunk(chunk_id="table-1", chunk_type="TABLE")
    connection.queue_rows({"record_count": 2})
    connection.queue_rows({"chunk_id": "parent-1"}, {"chunk_id": "table-1"})
    connection.queue_rows(chunk_row(parent), chunk_row(table))
    connection.queue_rows(chunk_row(parent))
    connection.queue_rows()
    connection.queue_rows(rowcount=2)

    assert await repository.count_revision("doc-1", "rev-1") == 2
    assert await repository.list_revision_ids("doc-1", "rev-1") == (
        "parent-1",
        "table-1",
    )
    assert await repository.list_for_revision("doc-1", "rev-1") == (parent, table)
    assert await repository.get_by_id("parent-1") == parent
    assert await repository.get_by_id("missing") is None
    assert await repository.delete_revision("doc-1", "rev-1") == 2

    assert "ORDER BY chunk_id" in normalized_sql(connection.execute_calls[2][0])
    assert "WHERE chunk_id = %s" in normalized_sql(connection.execute_calls[3][0])
    assert connection.execute_calls[3][1] == ("parent-1",)


@pytest.mark.asyncio
async def test_chunk_context_lookup_accepts_only_parent_or_table() -> None:
    from atlasrag.repositories.postgres.chunks import ChunkRepository

    connection = FakeConnection()
    repository = ChunkRepository(cast(Any, connection))
    parent = stored_chunk()
    table = stored_chunk(chunk_id="table-1", chunk_type="TABLE")
    child = stored_chunk(
        chunk_id="child-1", chunk_type="TEXT_CHILD", parent_id="parent-1"
    )
    connection.queue_rows(chunk_row(parent))
    connection.queue_rows(chunk_row(table))
    connection.queue_rows()
    connection.queue_rows(chunk_row(child))

    assert (
        await repository.get_canonical_context("doc-1", "rev-1", "parent-1") == parent
    )
    assert await repository.get_canonical_context("doc-1", "rev-1", "table-1") == table
    assert await repository.get_canonical_context("doc-1", "rev-1", "missing") is None
    with pytest.raises(CanonicalDataError):
        await repository.get_canonical_context("doc-1", "rev-1", "child-1")
    assert connection.execute_calls[0][1] == ("doc-1", "rev-1", "parent-1")


@pytest.mark.asyncio
async def test_chunk_invalid_row_is_canonical_error_not_absence() -> None:
    from atlasrag.repositories.postgres.chunks import ChunkRepository

    connection = FakeConnection()
    connection.queue_rows({"chunk_id": "broken"})
    repository = ChunkRepository(cast(Any, connection))

    with pytest.raises(CanonicalDataError):
        await repository.get_by_id("broken")


@pytest.mark.asyncio
async def test_invalid_repository_identifiers_fail_before_sql() -> None:
    from atlasrag.repositories.postgres.chunks import ChunkRepository
    from atlasrag.repositories.postgres.documents import DocumentRepository
    from atlasrag.repositories.postgres.elements import ElementRepository

    connection = FakeConnection()
    with pytest.raises(InvariantViolationError):
        await DocumentRepository(cast(Any, connection)).get_by_id(" ")
    with pytest.raises(InvariantViolationError):
        await ElementRepository(cast(Any, connection)).count_revision("doc-1", "")
    with pytest.raises(InvariantViolationError):
        await ChunkRepository(cast(Any, connection)).get_by_id("bad\n")
    assert connection.execute_calls == []


@pytest.mark.asyncio
async def test_invalid_aggregate_and_revision_rows_are_canonical_errors() -> None:
    from atlasrag.repositories.postgres.elements import ElementRepository

    connection = FakeConnection()
    repository = ElementRepository(cast(Any, connection))
    connection.queue_rows({"record_count": -1})
    connection.queue_rows({"element_id": "bad\n"})
    connection.queue_rows(rowcount=-1)

    with pytest.raises(CanonicalDataError):
        await repository.count_revision("doc-1", "rev-1")
    with pytest.raises(CanonicalDataError):
        await repository.list_revision_ids("doc-1", "rev-1")
    with pytest.raises(CanonicalDataError):
        await repository.delete_revision("doc-1", "rev-1")


@pytest.mark.asyncio
async def test_integrity_failure_is_an_invariant_error() -> None:
    from atlasrag.repositories.postgres.documents import DocumentRepository

    connection = FakeConnection()
    connection.execute_error = IntegrityError("constraint detail with secret")
    repository = DocumentRepository(cast(Any, connection))

    with pytest.raises(InvariantViolationError) as raised:
        await repository.put(stored_document())

    assert "secret" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("driver_error", "expected_error"),
    [
        (DataError("invalid local value with secret"), InvariantViolationError),
        (ProgrammingError("invalid SQL with secret"), ConfigurationError),
    ],
)
async def test_non_connectivity_driver_errors_are_not_reported_as_outages(
    driver_error: BaseException,
    expected_error: type[BaseException],
) -> None:
    from atlasrag.repositories.postgres.documents import DocumentRepository

    connection = FakeConnection()
    connection.execute_error = driver_error
    repository = DocumentRepository(cast(Any, connection))

    with pytest.raises(expected_error) as raised:
        await repository.put(stored_document())

    assert "secret" not in str(raised.value)


def test_storage_records_reject_postgres_incompatible_nul() -> None:
    document = document_row(stored_document())
    element = element_row(stored_element())
    chunk = chunk_row(stored_chunk())
    invalid_records: tuple[tuple[type[object], dict[str, object]], ...] = (
        (StoredDocument, {**document, "file_name": "bad\x00name"}),
        (
            StoredDocument,
            {**document, "parse_metadata": {"bad\x00key": "value"}},
        ),
        (StoredElement, {**element, "content": "bad\x00content"}),
        (
            StoredElement,
            {**element, "source_anchor": {"heading": "bad\x00heading"}},
        ),
        (StoredChunk, {**chunk, "section_path": ["bad\x00section"]}),
        (StoredChunk, {**chunk, "metadata": {"value": "bad\x00json"}}),
    )

    for model, data in invalid_records:
        with pytest.raises(ValidationError):
            cast(Any, model).model_validate(data)


@pytest.mark.asyncio
async def test_uow_explicit_commit_is_single_use_and_skips_exit_rollback() -> None:
    from atlasrag.repositories.postgres.uow import PostgresUnitOfWork

    connection = FakeConnection()
    pool = FakePool(connection)
    uow = PostgresUnitOfWork(cast(Any, pool))

    async with uow:
        repository_reference = uow.documents
        await uow.commit()
        with pytest.raises(InvariantViolationError):
            await uow.commit()
        with pytest.raises(InvariantViolationError):
            await repository_reference.list()

    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert pool.putconn_calls == [connection]
    with pytest.raises(InvariantViolationError):
        _ = uow.documents
    with pytest.raises(InvariantViolationError):
        await uow.__aenter__()


@pytest.mark.asyncio
async def test_uow_cannot_commit_caught_repository_transaction_failure() -> None:
    from atlasrag.repositories.postgres.uow import PostgresUnitOfWork

    connection = FakeConnection()
    connection.execute_error = IntegrityError("constraint failure")
    pool = FakePool(connection)

    async with PostgresUnitOfWork(cast(Any, pool)) as uow:
        with pytest.raises(InvariantViolationError):
            await uow.documents.put(stored_document())
        connection.execute_error = None
        assert connection.info.transaction_status is TransactionStatus.INERROR
        with pytest.raises(InvariantViolationError):
            await uow.commit()

    assert connection.commits == 0
    assert connection.rollbacks == 1


@pytest.mark.asyncio
async def test_uow_cannot_commit_after_caught_canonical_read_failure() -> None:
    from atlasrag.repositories.postgres.uow import PostgresUnitOfWork

    connection = FakeConnection()
    connection.queue_rows({"document_id": "broken"})
    pool = FakePool(connection)

    async with PostgresUnitOfWork(cast(Any, pool)) as uow:
        with pytest.raises(CanonicalDataError):
            await uow.documents.get_by_id("broken")
        assert connection.info.transaction_status is TransactionStatus.INTRANS
        with pytest.raises(InvariantViolationError):
            await uow.commit()

    assert connection.commits == 0
    assert connection.rollbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        TransactionStatus.ACTIVE,
        TransactionStatus.INERROR,
        TransactionStatus.UNKNOWN,
    ],
)
async def test_uow_refuses_uncommittable_driver_status(
    status: TransactionStatus,
) -> None:
    from atlasrag.repositories.postgres.uow import PostgresUnitOfWork

    connection = FakeConnection()
    pool = FakePool(connection)
    async with PostgresUnitOfWork(cast(Any, pool)) as uow:
        connection.info.transaction_status = status
        with pytest.raises(InvariantViolationError):
            await uow.commit()
    assert connection.commits == 0
    assert connection.rollbacks == 1


@pytest.mark.asyncio
async def test_uow_exception_rolls_back_and_preserves_body_error() -> None:
    from atlasrag.repositories.postgres.uow import PostgresUnitOfWork

    connection = FakeConnection()
    pool = FakePool(connection)
    body_error = RuntimeError("business failure")

    with pytest.raises(RuntimeError) as raised:
        async with PostgresUnitOfWork(cast(Any, pool)):
            raise body_error

    assert raised.value is body_error
    assert connection.rollbacks == 1
    assert pool.putconn_calls == [connection]


@pytest.mark.asyncio
async def test_uow_preserves_cancellation_after_cleanup() -> None:
    from atlasrag.repositories.postgres.uow import PostgresUnitOfWork

    connection = FakeConnection()
    pool = FakePool(connection)
    cancellation = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError) as raised:
        async with PostgresUnitOfWork(cast(Any, pool)):
            raise cancellation

    assert raised.value is cancellation
    assert connection.rollbacks == 1
    assert pool.putconn_calls == [connection]


@pytest.mark.asyncio
async def test_uow_preserves_primary_error_while_all_cleanup_is_attempted() -> None:
    from atlasrag.repositories.postgres.uow import PostgresUnitOfWork

    connection = FakeConnection()
    connection.rollback_error = OperationalError("rollback hidden")
    connection.close_error = OperationalError("close hidden")
    pool = FakePool(connection)
    pool.putconn_error = OperationalError("return hidden")
    body_error = RuntimeError("primary body failure")

    with pytest.raises(RuntimeError) as raised:
        async with PostgresUnitOfWork(cast(Any, pool)):
            raise body_error

    assert raised.value is body_error
    assert connection.rollbacks == 1
    assert connection.closes >= 1
    assert pool.putconn_calls == [connection]
    assert any("cleanup failure" in note.lower() for note in raised.value.__notes__)


@pytest.mark.asyncio
async def test_uow_preserves_cancellation_when_cleanup_also_fails() -> None:
    from atlasrag.repositories.postgres.uow import PostgresUnitOfWork

    connection = FakeConnection()
    connection.rollback_error = OperationalError("rollback hidden")
    connection.close_error = OperationalError("close hidden")
    pool = FakePool(connection)
    pool.putconn_error = OperationalError("return hidden")
    cancellation = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError) as raised:
        async with PostgresUnitOfWork(cast(Any, pool)):
            raise cancellation

    assert raised.value is cancellation
    assert connection.rollbacks == 1
    assert connection.closes >= 1
    assert pool.putconn_calls == [connection]


@pytest.mark.asyncio
async def test_uow_commit_failure_is_translated_then_rolled_back() -> None:
    from atlasrag.repositories.postgres.uow import PostgresUnitOfWork

    connection = FakeConnection()
    connection.commit_error = OperationalError("commit leaked secret")
    pool = FakePool(connection)

    with pytest.raises(DependencyUnavailableError) as raised:
        async with PostgresUnitOfWork(cast(Any, pool)) as uow:
            await uow.commit()

    assert "secret" not in str(raised.value)
    assert connection.commits == 1
    assert connection.rollbacks == 1
    assert pool.putconn_calls == [connection]


@pytest.mark.asyncio
async def test_uow_acquisition_and_cleanup_failures_are_typed() -> None:
    from atlasrag.repositories.postgres.uow import PostgresUnitOfWork

    acquire_pool = FakePool(FakeConnection())
    acquire_pool.getconn_error = PoolTimeout("hidden")
    with pytest.raises(DependencyTimeoutError):
        async with PostgresUnitOfWork(cast(Any, acquire_pool)):
            pytest.fail("unreachable")

    rollback_connection = FakeConnection()
    rollback_connection.rollback_error = OperationalError("rollback hidden")
    rollback_pool = FakePool(rollback_connection)
    with pytest.raises(DependencyUnavailableError):
        async with PostgresUnitOfWork(cast(Any, rollback_pool)):
            pass
    assert rollback_connection.closes == 1
    assert rollback_pool.putconn_calls == [rollback_connection]

    return_connection = FakeConnection()
    return_pool = FakePool(return_connection)
    return_pool.putconn_error = OperationalError("return hidden")
    with pytest.raises(DependencyUnavailableError):
        async with PostgresUnitOfWork(cast(Any, return_pool)) as uow:
            await uow.commit()
    assert return_connection.closes == 1


def test_advisory_lock_key_is_stable_validated_signed_int64() -> None:
    from atlasrag.repositories.postgres.locks import advisory_lock_key

    positive = advisory_lock_key("doc-positive")
    negative = advisory_lock_key("doc-negative")

    assert positive == 8167232475569918280
    assert negative == -8328432071101341633
    assert -(2**63) <= positive < 2**63
    assert -(2**63) <= negative < 2**63
    assert positive != negative
    assert advisory_lock_key("doc-positive") == positive
    with pytest.raises(InvariantViolationError):
        advisory_lock_key(" ")


@pytest.mark.asyncio
async def test_advisory_lock_lease_holds_connection_until_idempotent_release() -> None:
    from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager

    connection = FakeConnection()
    connection.queue_rows({"acquired": True})
    connection.queue_rows({"released": True})
    pool = FakePool(connection)
    manager = PostgresAdvisoryLockManager(cast(Any, pool))

    lease = await manager.try_acquire("doc-positive")

    assert lease is not None
    acquire_sql, acquire_params = connection.execute_calls[0]
    assert "pg_try_advisory_lock(%s)" in normalized_sql(acquire_sql)
    assert acquire_params == (8167232475569918280,)
    assert connection.rollbacks == 1
    assert pool.putconn_calls == []

    await lease.release()
    release_sql, release_params = connection.execute_calls[1]
    assert "pg_advisory_unlock(%s)" in normalized_sql(release_sql)
    assert release_params == (8167232475569918280,)
    assert connection.rollbacks == 2
    assert pool.putconn_calls == [connection]

    await lease.release()
    assert len(connection.execute_calls) == 2
    assert pool.putconn_calls == [connection]


@pytest.mark.asyncio
async def test_advisory_lock_contention_is_the_only_none_result() -> None:
    from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager

    connection = FakeConnection()
    connection.queue_rows({"acquired": False})
    pool = FakePool(connection)

    assert (
        await PostgresAdvisoryLockManager(cast(Any, pool)).try_acquire("doc-1") is None
    )
    assert connection.rollbacks == 1
    assert connection.closes == 0
    assert pool.putconn_calls == [connection]


@pytest.mark.asyncio
async def test_advisory_lock_rejects_invalid_input_before_pool_acquisition() -> None:
    from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager

    pool = FakePool(FakeConnection())

    with pytest.raises(InvariantViolationError):
        await PostgresAdvisoryLockManager(cast(Any, pool)).try_acquire("bad\n")

    assert pool.getconn_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        (),
        ({"unexpected": True},),
        ({"acquired": 1},),
    ],
)
async def test_advisory_lock_malformed_result_is_not_contention(
    rows: tuple[object, ...],
) -> None:
    from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager

    connection = FakeConnection()
    connection.queue_rows(*rows)
    pool = FakePool(connection)

    with pytest.raises(CanonicalDataError):
        await PostgresAdvisoryLockManager(cast(Any, pool)).try_acquire("doc-1")

    assert connection.closes == 1
    assert pool.putconn_calls == [connection]


@pytest.mark.asyncio
async def test_advisory_lock_acquisition_failures_are_typed_and_cleaned_up() -> None:
    from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager

    timeout_pool = FakePool(FakeConnection())
    timeout_pool.getconn_error = PoolTimeout("hidden")
    with pytest.raises(DependencyTimeoutError):
        await PostgresAdvisoryLockManager(cast(Any, timeout_pool)).try_acquire("doc-1")

    connection = FakeConnection()
    connection.execute_error = OperationalError("query hidden")
    unavailable_pool = FakePool(connection)
    with pytest.raises(DependencyUnavailableError) as raised:
        await PostgresAdvisoryLockManager(cast(Any, unavailable_pool)).try_acquire(
            "doc-1"
        )

    assert "hidden" not in str(raised.value)
    assert connection.closes == 1
    assert unavailable_pool.putconn_calls == [connection]


@pytest.mark.asyncio
async def test_advisory_lock_acquisition_preserves_cancellation_after_cleanup() -> None:
    from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager

    cancellation = asyncio.CancelledError()
    connection = FakeConnection()
    connection.execute_error = cancellation
    pool = FakePool(connection)

    with pytest.raises(asyncio.CancelledError) as raised:
        await PostgresAdvisoryLockManager(cast(Any, pool)).try_acquire("doc-1")

    assert raised.value is cancellation
    assert connection.closes == 1
    assert pool.putconn_calls == [connection]


@pytest.mark.asyncio
async def test_advisory_lock_contention_cleanup_failure_is_not_none() -> None:
    from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager

    connection = FakeConnection()
    connection.queue_rows({"acquired": False})
    pool = FakePool(connection)
    pool.putconn_error = OperationalError("return hidden")

    with pytest.raises(DependencyUnavailableError):
        await PostgresAdvisoryLockManager(cast(Any, pool)).try_acquire("doc-1")

    assert connection.closes == 1
    assert pool.putconn_calls == [connection]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("release_error", "expected_error"),
    [
        (OperationalError("release hidden"), DependencyUnavailableError),
        (asyncio.CancelledError(), asyncio.CancelledError),
    ],
)
async def test_advisory_lock_release_failure_discards_connection_and_is_idempotent(
    release_error: BaseException,
    expected_error: type[BaseException],
) -> None:
    from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager

    connection = FakeConnection()
    connection.queue_rows({"acquired": True})
    connection.execute_failures[2] = release_error
    pool = FakePool(connection)
    lease = await PostgresAdvisoryLockManager(cast(Any, pool)).try_acquire("doc-1")
    assert lease is not None

    with pytest.raises(expected_error) as raised:
        await lease.release()

    if isinstance(release_error, asyncio.CancelledError):
        assert raised.value is release_error
    assert connection.closes == 1
    assert pool.putconn_calls == [connection]
    await lease.release()
    assert connection.closes == 1
    assert pool.putconn_calls == [connection]


@pytest.mark.asyncio
async def test_advisory_lock_false_unlock_is_an_invariant_failure() -> None:
    from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager

    connection = FakeConnection()
    connection.queue_rows({"acquired": True})
    connection.queue_rows({"released": False})
    pool = FakePool(connection)
    lease = await PostgresAdvisoryLockManager(cast(Any, pool)).try_acquire("doc-1")
    assert lease is not None

    with pytest.raises(InvariantViolationError):
        await lease.release()

    assert connection.closes == 1
    assert pool.putconn_calls == [connection]


@pytest.mark.asyncio
async def test_advisory_lock_release_return_failure_closes_unlocked_connection() -> (
    None
):
    from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager

    connection = FakeConnection()
    connection.queue_rows({"acquired": True})
    connection.queue_rows({"released": True})
    pool = FakePool(connection)
    lease = await PostgresAdvisoryLockManager(cast(Any, pool)).try_acquire("doc-1")
    assert lease is not None
    pool.putconn_error = OperationalError("return hidden")

    with pytest.raises(DependencyUnavailableError):
        await lease.release()

    assert connection.closes == 1
    assert pool.putconn_calls == [connection]


@pytest.mark.asyncio
async def test_advisory_lock_does_not_pool_possibly_locked_connection() -> None:
    from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager

    cancellation = asyncio.CancelledError()
    connection = FakeConnection()
    connection.queue_rows({"acquired": True})
    connection.execute_failures[2] = cancellation
    connection.close_error = OperationalError("close hidden")
    pool = FakePool(connection)
    lease = await PostgresAdvisoryLockManager(cast(Any, pool)).try_acquire("doc-1")
    assert lease is not None

    with pytest.raises(asyncio.CancelledError) as raised:
        await lease.release()

    assert raised.value is cancellation
    assert connection.closes == 1
    assert pool.putconn_calls == []
    assert any("cleanup failure" in note.lower() for note in raised.value.__notes__)


def test_advisory_lock_is_session_level_and_separate_from_uow() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "src/atlasrag/repositories/postgres/locks.py").read_text()

    assert "pg_try_advisory_lock" in source
    assert "pg_advisory_unlock" in source
    assert "pg_advisory_xact_lock" not in source
    assert "PostgresUnitOfWork" not in source
    assert ".getconn(" in source
    assert ".putconn(" in source


def test_repository_modules_never_commit_and_uow_avoids_pool_context() -> None:
    root = Path(__file__).resolve().parents[2]
    repository_paths = [
        root / "src/atlasrag/repositories/postgres" / name
        for name in (
            "documents.py",
            "elements.py",
            "chunks.py",
            "runtime_metadata.py",
        )
    ]
    for path in repository_paths:
        tree = ast.parse(path.read_text(), filename=path.as_posix())
        commits = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "commit"
        ]
        assert commits == [], f"repository must not commit: {path}"

    uow_source = (root / "src/atlasrag/repositories/postgres/uow.py").read_text()
    assert ".getconn(" in uow_source
    assert ".putconn(" in uow_source
    assert ".connection(" not in uow_source
