"""Service-free contracts for the async Milvus derived-index provider."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import threading
from collections import UserList
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from pymilvus.exceptions import (  # type: ignore[import-untyped]
    MilvusException,
    MilvusUnavailableException,
)

from atlasrag.domain.errors import (
    ConfigurationError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    InvariantViolationError,
    ProviderResponseError,
)
from atlasrag.providers.index.milvus import MilvusIndexProvider
from atlasrag.providers.index.models import MilvusChunkRecord
from atlasrag.providers.index.schema import (
    COLLECTION_NAME,
    DENSE_INDEX_NAME,
    SPARSE_INDEX_NAME,
    build_collection_schema,
    build_index_params,
    collection_schema_mismatches,
    index_schema_mismatches,
)


@dataclass(frozen=True)
class SDKCall:
    name: str
    kwargs: dict[str, object]
    thread_id: int


class FakeQueryIterator:
    def __init__(
        self,
        calls: list[SDKCall],
        pages: list[object] | None = None,
    ) -> None:
        self._calls = calls
        self.pages = list(pages or [[]])
        self.next_error: BaseException | None = None
        self.close_error: BaseException | None = None

    def next(self) -> object:
        self._calls.append(SDKCall("iterator.next", {}, threading.get_ident()))
        if self.next_error is not None:
            raise self.next_error
        if self.pages:
            return self.pages.pop(0)
        return []

    def close(self) -> None:
        self._calls.append(SDKCall("iterator.close", {}, threading.get_ident()))
        if self.close_error is not None:
            raise self.close_error


def collection_description(
    collection_name: str = COLLECTION_NAME,
    *,
    embedding_dimension: int = 3,
) -> dict[str, object]:
    return {
        "collection_name": collection_name,
        **build_collection_schema(embedding_dimension).to_dict(),
        "properties": {},
        "aliases": [],
        "collection_id": 123,
        "num_shards": 1,
        "num_partitions": 1,
        "consistency_level": 2,
        "consistency_level_name": "Bounded",
        "created_timestamp": 11,
        "update_timestamp": 12,
    }


def index_descriptions() -> dict[str, object]:
    return {
        parameter.index_name: parameter.to_dict() for parameter in build_index_params()
    }


class FakeMilvusClient:
    """Synchronous fake whose call records prove thread offloading."""

    def __init__(
        self, *, existing: bool = True, collection_name: str = COLLECTION_NAME
    ):
        self.calls: list[SDKCall] = []
        self.errors: dict[str, BaseException] = {}
        self.has_collection_result: object = existing
        self.collection_description: object = collection_description(collection_name)
        self.index_names_result: object = [DENSE_INDEX_NAME, SPARSE_INDEX_NAME]
        self.index_description_results: dict[str, object] = index_descriptions()
        self.query_results: list[object] = [[{"count(*)": 0}]]
        self.iterator = FakeQueryIterator(self.calls)
        self.insert_result: object | None = None
        self.delete_results: list[object] = []
        self.flush_result: object = None
        self.create_result: object = None
        self.load_result: object = None
        self.close_result: object = None

    def _record(self, name: str, kwargs: dict[str, object]) -> None:
        self.calls.append(SDKCall(name, dict(kwargs), threading.get_ident()))
        if error := self.errors.get(name):
            raise error

    def has_collection(self, **kwargs: object) -> object:
        self._record("has_collection", kwargs)
        return self.has_collection_result

    def create_collection(self, **kwargs: object) -> object:
        self._record("create_collection", kwargs)
        return self.create_result

    def describe_collection(self, **kwargs: object) -> object:
        self._record("describe_collection", kwargs)
        return self.collection_description

    def list_indexes(self, **kwargs: object) -> object:
        self._record("list_indexes", kwargs)
        return self.index_names_result

    def describe_index(self, **kwargs: object) -> object:
        self._record("describe_index", kwargs)
        index_name = kwargs.get("index_name")
        assert isinstance(index_name, str)
        return self.index_description_results.get(index_name)

    def load_collection(self, **kwargs: object) -> object:
        self._record("load_collection", kwargs)
        return self.load_result

    def delete(self, **kwargs: object) -> object:
        self._record("delete", kwargs)
        if self.delete_results:
            return self.delete_results.pop(0)
        return {"delete_count": 0}

    def flush(self, **kwargs: object) -> object:
        self._record("flush", kwargs)
        return self.flush_result

    def insert(self, **kwargs: object) -> object:
        self._record("insert", kwargs)
        if self.insert_result is not None:
            return self.insert_result
        data = kwargs.get("data")
        assert isinstance(data, list)
        return {
            "insert_count": len(data),
            "ids": [row["chunk_id"] for row in data],
        }

    def query(self, **kwargs: object) -> object:
        self._record("query", kwargs)
        if self.query_results:
            return self.query_results.pop(0)
        return [{"count(*)": 0}]

    def query_iterator(self, **kwargs: object) -> object:
        self._record("query_iterator", kwargs)
        return self.iterator

    def close(self) -> object:
        self._record("close", {})
        return self.close_result


class BlockingQueryIterator(FakeQueryIterator):
    def __init__(self, calls: list[SDKCall]) -> None:
        super().__init__(calls)
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.active = False
        self.closed_while_active = False

    def next(self) -> object:
        self._calls.append(SDKCall("iterator.next", {}, threading.get_ident()))
        self.active = True
        self.started.set()
        self.release.wait(timeout=5.0)
        self.active = False
        return []

    def close(self) -> None:
        self._calls.append(SDKCall("iterator.close", {}, threading.get_ident()))
        self.closed_while_active = self.active
        self.closed.set()


class BlockingDeleteClient(FakeMilvusClient):
    def __init__(self) -> None:
        super().__init__()
        self.delete_started = threading.Event()
        self.release_delete = threading.Event()

    def delete(self, **kwargs: object) -> object:
        self._record("delete", kwargs)
        self.delete_started.set()
        self.release_delete.wait(timeout=5.0)
        return {"delete_count": 0}


async def wait_for_thread_event(event: threading.Event) -> None:
    async with asyncio.timeout(1.0):
        while not event.is_set():
            await asyncio.sleep(0)


def milvus_record(**overrides: object) -> MilvusChunkRecord:
    values: dict[str, object] = {
        "chunk_id": "chunk-1",
        "document_id": "doc-1",
        "revision_id": "rev-1",
        "file_name": "manual.md",
        "parent_id": "parent-1",
        "chunk_type": "TEXT_CHILD",
        "source_type": "MD",
        "content": "Canonical child content.",
        "sparse_text": "architecture canonical child content",
        "dense_vector": [0.25, -0.5, 0.75],
        "structured_metadata": {
            "section_path": ["Architecture", "Persistence"],
            "page": 7,
        },
    }
    values.update(overrides)
    return MilvusChunkRecord.model_validate(values)


def provider(
    client: FakeMilvusClient,
    *,
    collection_name: str = COLLECTION_NAME,
    embedding_dimension: int = 3,
    timeout_seconds: float = 5.0,
) -> MilvusIndexProvider:
    return MilvusIndexProvider(
        cast(Any, client),
        collection_name=collection_name,
        embedding_dimension=embedding_dimension,
        timeout_seconds=timeout_seconds,
    )


def call_names(client: FakeMilvusClient) -> list[str]:
    return [call.name for call in client.calls]


def calls_named(client: FakeMilvusClient, name: str) -> list[SDKCall]:
    return [call for call in client.calls if call.name == name]


def test_public_provider_methods_are_async() -> None:
    for method_name in (
        "initialize",
        "replace_revision_set",
        "count_revision",
        "list_revision_chunk_ids",
        "delete_revision",
        "delete_document",
        "health",
        "close",
    ):
        assert inspect.iscoroutinefunction(getattr(MilvusIndexProvider, method_name))


@pytest.mark.asyncio
async def test_missing_collection_is_created_with_exact_schema_and_indexes() -> None:
    client = FakeMilvusClient(existing=False)

    await provider(client).initialize()

    assert call_names(client) == ["has_collection", "create_collection"]
    create_call = calls_named(client, "create_collection")[0]
    assert create_call.kwargs["collection_name"] == COLLECTION_NAME
    schema = cast(Any, create_call.kwargs["schema"])
    indexes = cast(Any, create_call.kwargs["index_params"])
    assert (
        collection_schema_mismatches(
            {"collection_name": COLLECTION_NAME, **schema.to_dict()},
            embedding_dimension=3,
        )
        == ()
    )
    assert index_schema_mismatches([parameter.to_dict() for parameter in indexes]) == ()


@pytest.mark.asyncio
async def test_existing_collection_is_fully_verified_then_loaded() -> None:
    client = FakeMilvusClient()

    await provider(client).initialize()

    assert call_names(client) == [
        "has_collection",
        "describe_collection",
        "list_indexes",
        "describe_index",
        "describe_index",
        "load_collection",
    ]
    assert {
        call.kwargs["index_name"] for call in calls_named(client, "describe_index")
    } == {DENSE_INDEX_NAME, SPARSE_INDEX_NAME}


@pytest.mark.asyncio
async def test_custom_isolated_collection_name_is_compared_as_expected() -> None:
    collection_name = "atlas_chunks_test_123"
    client = FakeMilvusClient(collection_name=collection_name)

    await provider(client, collection_name=collection_name).initialize()

    assert calls_named(client, "load_collection")[0].kwargs["collection_name"] == (
        collection_name
    )


@pytest.mark.asyncio
async def test_incompatible_existing_collection_fails_closed_without_mutation() -> None:
    client = FakeMilvusClient()
    description = collection_description()
    fields = description["fields"]
    assert isinstance(fields, list)
    dense = next(field for field in fields if field["name"] == "dense_vector")
    dense["params"]["dim"] = 4
    client.collection_description = description
    dense_index = client.index_description_results[DENSE_INDEX_NAME]
    assert isinstance(dense_index, dict)
    client.index_description_results[DENSE_INDEX_NAME] = {
        **dense_index,
        "index_type": "IVF_FLAT",
    }

    with pytest.raises(ConfigurationError):
        await provider(client).initialize()

    assert call_names(client) == [
        "has_collection",
        "describe_collection",
        "list_indexes",
        "describe_index",
        "describe_index",
    ]
    assert "create_collection" not in call_names(client)
    assert "load_collection" not in call_names(client)


@pytest.mark.asyncio
async def test_missing_extra_or_malformed_indexes_fail_closed() -> None:
    for index_names in (
        [DENSE_INDEX_NAME],
        [DENSE_INDEX_NAME, SPARSE_INDEX_NAME, "unexpected"],
    ):
        client = FakeMilvusClient()
        client.index_names_result = index_names
        client.index_description_results["unexpected"] = {
            "field_name": "content",
            "index_name": "unexpected",
            "index_type": "INVERTED",
            "metric_type": "BM25",
            "params": {},
        }
        with pytest.raises(ConfigurationError):
            await provider(client).initialize()

    malformed = FakeMilvusClient()
    malformed.index_names_result = "dense_hnsw"
    with pytest.raises(ProviderResponseError):
        await provider(malformed).initialize()


@pytest.mark.asyncio
async def test_replace_revision_set_projects_only_insertable_fields_in_order() -> None:
    client = FakeMilvusClient()
    first = milvus_record()
    second = milvus_record(
        chunk_id="table-1",
        parent_id=None,
        chunk_type="TABLE",
        source_type="XLSX",
    )

    await provider(client).replace_revision_set(
        [first, second], document_id="doc-1", revision_id="rev-1"
    )

    assert call_names(client) == ["delete", "flush", "insert", "flush"]
    insert = calls_named(client, "insert")[0]
    data = insert.kwargs["data"]
    assert isinstance(data, list)
    assert [row["chunk_id"] for row in data] == ["chunk-1", "table-1"]
    assert set(data[0]) == {
        "chunk_id",
        "document_id",
        "revision_id",
        "file_name",
        "parent_id",
        "chunk_type",
        "source_type",
        "content",
        "sparse_text",
        "dense_vector",
        "structured_metadata",
    }
    assert "sparse_vector" not in data[0]
    assert data[0]["dense_vector"] == [0.25, -0.5, 0.75]
    assert data[0]["structured_metadata"] == {
        "section_path": ["Architecture", "Persistence"],
        "page": 7,
    }


@pytest.mark.asyncio
async def test_empty_replacement_deletes_and_exposes_the_empty_exact_set() -> None:
    client = FakeMilvusClient()

    await provider(client).replace_revision_set(
        [], document_id="doc-1", revision_id="rev-1"
    )

    assert call_names(client) == ["delete", "flush"]


@pytest.mark.asyncio
async def test_replacement_finishes_the_exact_set_before_propagating_cancellation() -> (
    None
):
    client = BlockingDeleteClient()
    index = provider(client)
    replacement = asyncio.create_task(
        index.replace_revision_set(
            [milvus_record()],
            document_id="doc-1",
            revision_id="rev-1",
        )
    )
    await wait_for_thread_event(client.delete_started)

    replacement.cancel()
    await asyncio.sleep(0)
    replacement.cancel()
    await asyncio.sleep(0.01)
    finished_before_release = replacement.done()
    client.release_delete.set()

    with pytest.raises(asyncio.CancelledError):
        await replacement
    assert not finished_before_release
    assert call_names(client) == ["delete", "flush", "insert", "flush"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "records",
    [
        [milvus_record(), milvus_record()],
        [milvus_record(), milvus_record(chunk_id="chunk-2", document_id="doc-2")],
        [milvus_record(), milvus_record(chunk_id="chunk-2", revision_id="rev-2")],
        [
            milvus_record(),
            milvus_record(chunk_id="chunk-2", dense_vector=[0.1, 0.2, 0.3, 0.4]),
        ],
    ],
)
async def test_invalid_replacement_is_rejected_before_first_sdk_mutation(
    records: list[MilvusChunkRecord],
) -> None:
    client = FakeMilvusClient()

    with pytest.raises(InvariantViolationError):
        await provider(client).replace_revision_set(
            records, document_id="doc-1", revision_id="rev-1"
        )

    assert client.calls == []


@pytest.mark.asyncio
async def test_forged_record_is_rejected_before_first_sdk_mutation() -> None:
    client = FakeMilvusClient()
    record = milvus_record()
    forged = MilvusChunkRecord.model_construct(
        **{
            **record.model_dump(mode="python"),
            "dense_vector": (math.inf, 0.0, 0.0),
        }
    )

    with pytest.raises(InvariantViolationError):
        await provider(client).replace_revision_set(
            [forged], document_id="doc-1", revision_id="rev-1"
        )

    assert client.calls == []


@pytest.mark.asyncio
async def test_scope_filters_escape_literals_without_expression_injection() -> None:
    document_id = 'doc" or chunk_type == "TABLE'
    revision_id = "rev\\tail"
    client = FakeMilvusClient()

    await provider(client).delete_revision(document_id, revision_id)

    delete_call = calls_named(client, "delete")[0]
    assert delete_call.kwargs["filter"] == (
        f"document_id == {json.dumps(document_id)} and "
        f"revision_id == {json.dumps(revision_id)}"
    )


@pytest.mark.asyncio
async def test_count_revision_validates_the_aggregate_response() -> None:
    client = FakeMilvusClient()
    client.query_results = [[{"count(*)": 0}], [{"count(*)": 7}]]
    index = provider(client)

    assert await index.count_revision("doc-1", "rev-1") == 0
    assert await index.count_revision("doc-1", "rev-1") == 7
    query_call = calls_named(client, "query")[0]
    assert query_call.kwargs["output_fields"] == ["count(*)"]
    assert query_call.kwargs["consistency_level"] == "Strong"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [None, {}, [], [{"count(*)": -1}], [{"count(*)": True}], [{"other": 1}]],
)
async def test_count_revision_rejects_malformed_responses(response: object) -> None:
    client = FakeMilvusClient()
    client.query_results = [response]

    with pytest.raises(ProviderResponseError):
        await provider(client).count_revision("doc-1", "rev-1")


@pytest.mark.asyncio
async def test_chunk_id_enumeration_pages_validates_and_closes_iterator() -> None:
    client = FakeMilvusClient()
    client.iterator = FakeQueryIterator(
        client.calls,
        pages=[
            [{"chunk_id": "chunk-2"}, {"chunk_id": "chunk-1"}],
            [{"chunk_id": "chunk-3"}],
            [],
        ],
    )

    result = await provider(client).list_revision_chunk_ids(
        "doc-1", "rev-1", batch_size=2
    )

    assert result == ("chunk-1", "chunk-2", "chunk-3")
    assert call_names(client) == [
        "query_iterator",
        "iterator.next",
        "iterator.next",
        "iterator.next",
        "iterator.close",
    ]
    query_call = calls_named(client, "query_iterator")[0]
    assert query_call.kwargs["batch_size"] == 2
    assert query_call.kwargs["limit"] == -1
    assert query_call.kwargs["output_fields"] == ["chunk_id"]
    assert query_call.kwargs["consistency_level"] == "Strong"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pages",
    [
        [None],
        [[{"not_chunk_id": "chunk-1"}]],
        [[{"chunk_id": True}]],
        [[{"chunk_id": "chunk-1"}, {"chunk_id": "chunk-1"}]],
    ],
)
async def test_chunk_id_enumeration_rejects_malformed_or_duplicate_pages(
    pages: list[object],
) -> None:
    client = FakeMilvusClient()
    client.iterator = FakeQueryIterator(client.calls, pages=pages)

    with pytest.raises(ProviderResponseError):
        await provider(client).list_revision_chunk_ids("doc-1", "rev-1")

    assert call_names(client)[-1] == "iterator.close"


@pytest.mark.asyncio
async def test_iterator_cleanup_preserves_primary_response_error() -> None:
    client = FakeMilvusClient()
    client.iterator = FakeQueryIterator(client.calls, pages=[None])
    client.iterator.close_error = OSError("cleanup unavailable")

    with pytest.raises(ProviderResponseError):
        await provider(client).list_revision_chunk_ids("doc-1", "rev-1")


@pytest.mark.asyncio
async def test_iterator_cleanup_failure_is_not_silently_ignored() -> None:
    client = FakeMilvusClient()
    client.iterator = FakeQueryIterator(client.calls, pages=[[]])
    client.iterator.close_error = OSError("cleanup unavailable")

    with pytest.raises(DependencyUnavailableError):
        await provider(client).list_revision_chunk_ids("doc-1", "rev-1")


@pytest.mark.asyncio
async def test_iterator_cancellation_waits_for_next_before_close() -> None:
    client = FakeMilvusClient()
    iterator = BlockingQueryIterator(client.calls)
    client.iterator = iterator
    enumeration = asyncio.create_task(
        provider(client).list_revision_chunk_ids("doc-1", "rev-1")
    )
    await wait_for_thread_event(iterator.started)

    enumeration.cancel()
    await asyncio.sleep(0)
    enumeration.cancel()
    await asyncio.sleep(0.01)
    finished_before_release = enumeration.done()
    closed_before_release = iterator.closed.is_set()
    iterator.release.set()

    with pytest.raises(asyncio.CancelledError):
        await enumeration
    assert not finished_before_release
    assert not closed_before_release
    assert iterator.closed.is_set()
    assert not iterator.closed_while_active


@pytest.mark.asyncio
async def test_revision_and_document_deletes_flush_and_report_counts() -> None:
    client = FakeMilvusClient()
    client.delete_results = [{"delete_count": 2}, {"delete_count": 5}]
    index = provider(client)

    assert await index.delete_revision("doc-1", "rev-1") == 2
    assert await index.delete_document("doc-1") == 5

    assert call_names(client) == ["delete", "flush", "delete", "flush"]
    revision_filter = calls_named(client, "delete")[0].kwargs["filter"]
    document_filter = calls_named(client, "delete")[1].kwargs["filter"]
    assert revision_filter == 'document_id == "doc-1" and revision_id == "rev-1"'
    assert document_filter == 'document_id == "doc-1"'


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [[], (), ["chunk-1"]])
async def test_delete_rejects_legacy_or_malformed_identity_responses(
    response: object,
) -> None:
    client = FakeMilvusClient()
    client.delete_results = [response]

    with pytest.raises(ProviderResponseError):
        await provider(client).replace_revision_set(
            [], document_id="doc-1", revision_id="rev-1"
        )

    assert call_names(client) == ["delete"]


@pytest.mark.asyncio
async def test_partial_insert_response_is_not_treated_as_success() -> None:
    client = FakeMilvusClient()
    client.insert_result = {"insert_count": 1, "ids": ["chunk-1"]}

    with pytest.raises(ProviderResponseError):
        await provider(client).replace_revision_set(
            [milvus_record(), milvus_record(chunk_id="chunk-2")],
            document_id="doc-1",
            revision_id="rev-1",
        )

    assert call_names(client) == ["delete", "flush", "insert"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"insert_count": 2},
        {"insert_count": 2, "ids": ["chunk-1"]},
        {"insert_count": 2, "ids": ["chunk-1", "chunk-1"]},
        {"insert_count": 2, "ids": ["chunk-1", "unexpected"]},
        {"insert_count": 2, "ids": ["chunk-1", True]},
    ],
)
async def test_insert_response_must_confirm_the_exact_identity_set(
    response: object,
) -> None:
    client = FakeMilvusClient()
    client.insert_result = response

    with pytest.raises(ProviderResponseError):
        await provider(client).replace_revision_set(
            [milvus_record(), milvus_record(chunk_id="chunk-2")],
            document_id="doc-1",
            revision_id="rev-1",
        )

    assert call_names(client) == ["delete", "flush", "insert"]


@pytest.mark.asyncio
async def test_insert_response_identity_order_is_not_semantic() -> None:
    client = FakeMilvusClient()
    client.insert_result = {
        "insert_count": 2,
        "ids": ["chunk-2", "chunk-1"],
    }

    await provider(client).replace_revision_set(
        [milvus_record(), milvus_record(chunk_id="chunk-2")],
        document_id="doc-1",
        revision_id="rev-1",
    )

    assert call_names(client) == ["delete", "flush", "insert", "flush"]


@pytest.mark.asyncio
async def test_insert_response_accepts_pinned_sdk_identity_sequence() -> None:
    client = FakeMilvusClient()
    client.insert_result = {
        "insert_count": 2,
        "ids": UserList(["chunk-2", "chunk-1"]),
    }

    await provider(client).replace_revision_set(
        [milvus_record(), milvus_record(chunk_id="chunk-2")],
        document_id="doc-1",
        revision_id="rev-1",
    )

    assert call_names(client) == ["delete", "flush", "insert", "flush"]


@pytest.mark.asyncio
async def test_health_distinguishes_missing_collection_and_invalid_response() -> None:
    missing = FakeMilvusClient(existing=False)
    with pytest.raises(ConfigurationError):
        await provider(missing).health()

    malformed = FakeMilvusClient()
    malformed.has_collection_result = 1
    with pytest.raises(ProviderResponseError):
        await provider(malformed).health()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (TimeoutError("timeout"), DependencyTimeoutError),
        (
            MilvusException(message="gRPC DEADLINE_EXCEEDED"),
            DependencyTimeoutError,
        ),
        (MilvusUnavailableException(message="unavailable"), DependencyUnavailableError),
        (OSError("connection refused"), DependencyUnavailableError),
        (RuntimeError("unexpected SDK failure"), DependencyUnavailableError),
    ],
)
async def test_sdk_failures_are_translated(
    error: BaseException,
    expected_error: type[Exception],
) -> None:
    client = FakeMilvusClient()
    client.errors["has_collection"] = error

    with pytest.raises(expected_error):
        await provider(client).health()


@pytest.mark.asyncio
async def test_technical_query_failure_never_becomes_empty_or_zero() -> None:
    client = FakeMilvusClient()
    client.errors["query"] = OSError("connection refused")

    with pytest.raises(DependencyUnavailableError):
        await provider(client).count_revision("doc-1", "rev-1")


@pytest.mark.asyncio
async def test_every_sync_sdk_and_iterator_call_runs_off_event_loop_thread() -> None:
    main_thread = threading.get_ident()
    client = FakeMilvusClient(existing=False)
    client.query_results = [[{"count(*)": 0}]]
    client.iterator = FakeQueryIterator(client.calls, pages=[[]])
    index = provider(client)

    await index.initialize()
    await index.replace_revision_set(
        [milvus_record()], document_id="doc-1", revision_id="rev-1"
    )
    await index.count_revision("doc-1", "rev-1")
    await index.list_revision_chunk_ids("doc-1", "rev-1")
    await index.delete_revision("doc-1", "rev-1")
    await index.delete_document("doc-1")
    client.has_collection_result = True
    await index.health()
    await index.close()

    assert client.calls
    assert all(call.thread_id != main_thread for call in client.calls)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"embedding_dimension": 1},
        {"embedding_dimension": 32_769},
        {"collection_name": "bad-name"},
        {"collection_name": ""},
        {"timeout_seconds": 0.0},
        {"timeout_seconds": math.inf},
        {"timeout_seconds": True},
    ],
)
def test_constructor_rejects_invalid_local_configuration(
    kwargs: dict[str, object],
) -> None:
    client = FakeMilvusClient()
    arguments: dict[str, object] = {
        "embedding_dimension": 3,
        "collection_name": COLLECTION_NAME,
        "timeout_seconds": 5.0,
        **kwargs,
    }

    with pytest.raises(InvariantViolationError):
        MilvusIndexProvider(cast(Any, client), **arguments)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", [0, -1, 16_385, True])
async def test_invalid_iterator_batch_size_is_rejected_before_sdk_call(
    batch_size: int,
) -> None:
    client = FakeMilvusClient()

    with pytest.raises(InvariantViolationError):
        await provider(client).list_revision_chunk_ids(
            "doc-1", "rev-1", batch_size=batch_size
        )

    assert client.calls == []


def test_provider_source_contains_no_destructive_collection_operations() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "atlasrag"
        / "providers"
        / "index"
        / "milvus.py"
    ).read_text(encoding="utf-8")

    assert ".drop_collection(" not in source
    assert ".truncate_collection(" not in source
