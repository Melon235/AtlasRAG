"""Candidate append and lossless paginated projection provider contracts."""

import asyncio
import math
import threading
from collections.abc import Callable, Sequence
from contextlib import aclosing
from typing import Any, Never, cast

import pytest
from providers.test_milvus_provider import (
    FakeMilvusClient,
    FakeQueryIterator,
    call_names,
    calls_named,
    milvus_record,
    provider,
    wait_for_thread_event,
)
from pydantic import ValidationError
from pymilvus.exceptions import (  # type: ignore[import-untyped]
    DataNotMatchException,
    DataTypeNotMatchException,
    MilvusException,
    ParamError,
    SchemaMismatchRetryableException,
)

from atlasrag.domain.errors import (
    DependencyTimeoutError,
    DependencyUnavailableError,
    InvariantViolationError,
    ProviderResponseError,
)
from atlasrag.providers.index.models import MilvusChunkRecord
from atlasrag.providers.index.projection import IndexWriteRejectedError


def projection(**overrides: object) -> dict[str, Any]:
    result = milvus_record().model_dump(mode="json")
    result.pop("dense_vector")
    return {**result, **overrides}


@pytest.mark.asyncio
async def test_append_batches_and_explicit_visibility_never_delete() -> None:
    client = FakeMilvusClient()
    index = provider(client)
    for chunk_id in ("batch-a", "batch-b"):
        await index.insert_revision_batch(
            [milvus_record(chunk_id=chunk_id)],
            document_id="doc-1",
            revision_id="rev-1",
        )
    await index.wait_revision_visible("doc-1", "rev-1")
    assert call_names(client) == ["insert", "insert", "flush"]
    inserted_ids = []
    for call in calls_named(client, "insert"):
        data = call.kwargs["data"]
        assert isinstance(data, list)
        assert len(data) == 1 and isinstance(data[0], dict)
        inserted_ids.append(data[0]["chunk_id"])
    assert inserted_ids == ["batch-a", "batch-b"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        {"chunk_id": "chunk-1"},
        {"document_id": "other"},
        {"revision_id": "other"},
        {"chunk_type": "TEXT_PARENT"},
        {"dense_vector": (math.inf, 0.0, 0.0)},
        {"dense_vector": (1.0, 2.0)},
    ],
)
async def test_entire_batch_is_validated_before_sdk(invalid: dict[str, object]) -> None:
    client = FakeMilvusClient()
    forged = MilvusChunkRecord.model_construct(
        **{
            **milvus_record(chunk_id="second").model_dump(mode="python"),
            **invalid,
        }
    )
    with pytest.raises(InvariantViolationError):
        await provider(client).insert_revision_batch(
            [milvus_record(), forged],
            document_id="doc-1",
            revision_id="rev-1",
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_projection_preserves_duplicates_unexpected_lineage_and_metadata() -> (
    None
):
    client = FakeMilvusClient()
    rows = [
        projection(),
        projection(),
        projection(
            chunk_id=" unexpected ",
            document_id="wrong-doc",
            revision_id="wrong-rev",
            chunk_type="TEXT_PARENT",
            source_type="UNKNOWN",
            parent_id="self",
            structured_metadata={"bad_semantics": [None, 1, " x "]},
        ),
    ]
    client.iterator = FakeQueryIterator(client.calls, [rows[:2], rows[2:], []])
    result = [
        row.model_dump(mode="json")
        async for row in provider(client).iter_revision_projection(
            "doc-1", "rev-1", batch_size=2
        )
    ]
    assert result == rows
    query = calls_named(client, "query_iterator")[0].kwargs
    output_fields = query["output_fields"]
    assert isinstance(output_fields, list)
    assert set(output_fields) == set(rows[0])
    assert query["consistency_level"] == "Strong"
    assert query["batch_size"] == 2
    assert query["limit"] == -1
    assert call_names(client).count("iterator.next") == 3
    assert call_names(client)[-1] == "iterator.close"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page",
    [
        None,
        {},
        [None],
        [{}],
        [projection(chunk_id=1)],
        [projection(structured_metadata=[])],
        [projection(dense_vector=[1, 2, 3])],
    ],
)
async def test_malformed_projection_fails_closed_and_closes(page: object) -> None:
    client = FakeMilvusClient()
    client.iterator = FakeQueryIterator(client.calls, [page])
    with pytest.raises(ProviderResponseError):
        _ = [
            row
            async for row in provider(client).iter_revision_projection(
                "doc-1", "rev-1", batch_size=2
            )
        ]
    assert call_names(client)[-1] == "iterator.close"


@pytest.mark.asyncio
async def test_early_consumer_close_releases_iterator() -> None:
    client = FakeMilvusClient()
    client.iterator = FakeQueryIterator(client.calls, [[projection()], [projection()]])
    async with aclosing(
        provider(client).iter_revision_projection("doc-1", "rev-1", batch_size=1)
    ) as rows:
        await anext(rows)
    assert call_names(client) == ["query_iterator", "iterator.next", "iterator.close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["query_iterator", "next", "close"])
async def test_projection_technical_errors_surface(operation: str) -> None:
    client = FakeMilvusClient()
    if operation == "query_iterator":
        client.errors[operation] = OSError("unavailable")
    else:
        setattr(client.iterator, operation + "_error", OSError("unavailable"))
    with pytest.raises(DependencyUnavailableError):
        _ = [
            row
            async for row in provider(client).iter_revision_projection(
                "doc-1", "rev-1", batch_size=1
            )
        ]
    if operation != "query_iterator":
        assert call_names(client)[-1] == "iterator.close"


@pytest.mark.asyncio
async def test_projection_preserves_primary_error_if_cleanup_also_fails() -> None:
    client = FakeMilvusClient()
    client.iterator = FakeQueryIterator(client.calls, [None])
    client.iterator.close_error = OSError("close failed")
    with pytest.raises(ProviderResponseError):
        _ = [
            row
            async for row in provider(client).iter_revision_projection(
                "doc-1", "rev-1", batch_size=1
            )
        ]


@pytest.mark.asyncio
async def test_cancellation_during_failed_cleanup_takes_precedence_over_bad_page() -> (
    None
):
    client = FakeMilvusClient()
    client.iterator = FakeQueryIterator(client.calls, [None])
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocked_close() -> None:
        started.set()
        assert release.wait(5)
        finished.set()
        raise OSError("cleanup failed")

    setattr(client.iterator, "close", blocked_close)

    async def consume() -> None:
        _ = [
            row
            async for row in provider(client).iter_revision_projection(
                "doc-1", "rev-1", batch_size=1
            )
        ]

    task = asyncio.create_task(consume())
    try:
        await wait_for_thread_event(started)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["query_iterator", "next", "close", "insert", "flush"]
)
@pytest.mark.parametrize("worker_fails", [False, True])
async def test_repeated_cancellation_settles_owned_work(
    operation: str, worker_fails: bool
) -> None:
    client = FakeMilvusClient()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    owner = client.iterator if operation in {"next", "close"} else client
    original = getattr(owner, operation)

    def blocked(**kwargs: object) -> object:
        started.set()
        assert release.wait(5.0)
        try:
            if worker_fails:
                if operation == "insert":
                    raise DataNotMatchException(message="late rejection")
                raise OSError("late technical failure")
            return original(**kwargs)
        finally:
            finished.set()

    setattr(owner, operation, blocked)
    index = provider(client)

    async def consume() -> None:
        _ = [
            row
            async for row in index.iter_revision_projection(
                "doc-1", "rev-1", batch_size=1
            )
        ]

    if operation == "insert":
        task = asyncio.create_task(
            index.insert_revision_batch(
                [milvus_record()], document_id="doc-1", revision_id="rev-1"
            )
        )
    elif operation == "flush":
        task = asyncio.create_task(index.wait_revision_visible("doc-1", "rev-1"))
    else:
        task = asyncio.create_task(consume())
    try:
        await wait_for_thread_event(started)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        assert not finished.is_set()
        if operation in {"query_iterator", "next"}:
            assert "iterator.close" not in call_names(client)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    if worker_fails and operation in {"query_iterator", "close"}:
        assert "iterator.close" not in call_names(client)
    elif operation not in {"insert", "flush"}:
        assert call_names(client)[-1] == "iterator.close"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error, expected",
    [
        (DataNotMatchException(message="private payload"), IndexWriteRejectedError),
        (DataTypeNotMatchException(message="private payload"), IndexWriteRejectedError),
        (ParamError(message="data mismatch"), DependencyUnavailableError),
        (
            SchemaMismatchRetryableException(message="schema changed"),
            DependencyUnavailableError,
        ),
        (MilvusException(message="DataNotMatchException"), DependencyUnavailableError),
        (TimeoutError("timeout"), DependencyTimeoutError),
        (OSError("outage"), DependencyUnavailableError),
    ],
)
async def test_only_typed_deterministic_write_rejections_are_business_failures(
    error: Exception, expected: type[Exception]
) -> None:
    client = FakeMilvusClient()
    client.errors["insert"] = error
    with pytest.raises(expected) as caught:
        await provider(client).insert_revision_batch(
            [milvus_record()], document_id="doc-1", revision_id="rev-1"
        )
    assert "private payload" not in str(caught.value)


@pytest.mark.asyncio
async def test_empty_batch_is_noop_and_bad_scope_is_pre_sdk() -> None:
    client = FakeMilvusClient()
    index = provider(client)
    await index.insert_revision_batch([], document_id="doc-1", revision_id="rev-1")
    with pytest.raises(InvariantViolationError):
        await index.wait_revision_visible("", "rev-1")
    assert not client.calls


@pytest.mark.asyncio
async def test_batch_size_is_bounded_before_sdk() -> None:
    client = FakeMilvusClient()

    class OversizedBatch(Sequence[MilvusChunkRecord]):
        def __len__(self) -> int:
            return 16_385

        def __getitem__(self, index: int | slice) -> Never:
            raise AssertionError(
                "Oversized batch must be rejected before materialization"
            )

    with pytest.raises(InvariantViolationError):
        await provider(client).insert_revision_batch(
            OversizedBatch(), document_id="doc-1", revision_id="rev-1"
        )
    assert not client.calls


@pytest.mark.asyncio
async def test_projection_metadata_is_lossless_and_deeply_immutable() -> None:
    client = FakeMilvusClient()
    metadata: dict[str, list[Any]] = {
        "values": [True, 1, 1.0, "1", None, {"key": " x \x00 "}]
    }
    client.iterator = FakeQueryIterator(
        client.calls, [[projection(structured_metadata=metadata)]]
    )
    async with aclosing(
        provider(client).iter_revision_projection("doc-1", "rev-1", batch_size=1)
    ) as rows:
        row = await anext(rows)
    values = row.structured_metadata["values"]
    assert isinstance(values, tuple)
    assert tuple(type(value) for value in values[:5]) == (
        bool,
        int,
        float,
        str,
        type(None),
    )
    with pytest.raises(TypeError):
        values[5]["key"] = "changed"
    with pytest.raises(TypeError):
        # Deliberately bypass the static read-only type to test runtime freezing.
        cast(Any, row.structured_metadata)["extra"] = 1
    with pytest.raises(ValidationError):
        row.chunk_id = "changed"
    metadata["values"][5]["key"] = "mutated source"
    assert row.model_dump(mode="json")["structured_metadata"]["values"][5] == {
        "key": " x \x00 "
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", [0, -1, 16_385, True])
async def test_invalid_projection_page_bound_is_rejected_before_sdk(
    batch_size: int,
) -> None:
    client = FakeMilvusClient()
    with pytest.raises(InvariantViolationError):
        _ = [
            row
            async for row in provider(client).iter_revision_projection(
                "doc-1", "rev-1", batch_size=batch_size
            )
        ]
    assert not client.calls


@pytest.mark.asyncio
async def test_oversized_projection_page_is_rejected_and_closed() -> None:
    client = FakeMilvusClient()
    client.iterator = FakeQueryIterator(client.calls, [[projection(), projection()]])
    with pytest.raises(ProviderResponseError):
        _ = [
            row
            async for row in provider(client).iter_revision_projection(
                "doc-1", "rev-1", batch_size=1
            )
        ]
    assert call_names(client)[-1] == "iterator.close"


@pytest.mark.asyncio
async def test_partial_insert_confirmation_is_not_success() -> None:
    client = FakeMilvusClient()
    client.insert_result = {"insert_count": 0, "ids": []}
    with pytest.raises(ProviderResponseError):
        await provider(client).insert_revision_batch(
            [milvus_record()], document_id="doc-1", revision_id="rev-1"
        )
    assert call_names(client) == ["insert"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page", ["not-a-page", [{"chunk_id": 1}], [projection(), projection()]]
)
async def test_malformed_projection_response_has_precise_internal_classification(
    page: Any,
) -> None:
    from atlasrag.providers.index import projection as projection_module

    malformed = getattr(projection_module, "IndexProjectionMalformedError", None)
    assert malformed is not None, (
        "malformed reachable projection needs a precise subtype"
    )
    client = FakeMilvusClient()
    client.iterator = FakeQueryIterator(client.calls, [page])
    with pytest.raises(malformed):
        _ = [
            row
            async for row in provider(client).iter_revision_projection(
                "doc-1", "rev-1", batch_size=1
            )
        ]
    assert call_names(client)[-1] == "iterator.close"


@pytest.mark.asyncio
async def test_malformed_projection_and_cleanup_failure_remains_technical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlasrag.providers.index import milvus
    from atlasrag.providers.index.projection import IndexProjectionMalformedError

    # Isolate response/cleanup classification from the separately tested SDK
    # thread boundary; all operations here are deterministic in-memory fakes.
    async def invoke(
        operation: Callable[..., object], *args: object, **kwargs: object
    ) -> object:
        try:
            return operation(*args, **kwargs)
        except OSError:
            raise DependencyUnavailableError from None

    monkeypatch.setattr(milvus, "_run_sync", invoke)
    client = FakeMilvusClient()
    client.iterator = FakeQueryIterator(client.calls, [None])
    client.iterator.close_error = OSError("close unavailable")
    with pytest.raises(ProviderResponseError) as caught:
        _ = [
            row
            async for row in provider(client).iter_revision_projection(
                "doc-1", "rev-1", batch_size=1
            )
        ]
    assert type(caught.value) is ProviderResponseError
    assert isinstance(caught.value.__cause__, IndexProjectionMalformedError)
    assert call_names(client)[-1] == "iterator.close"
