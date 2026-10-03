"""Real Milvus verification over one explicitly isolated test collection."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from pymilvus import MilvusClient  # type: ignore[import-untyped]

from atlasrag.domain.errors import (
    ConfigurationError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    InvariantViolationError,
)
from atlasrag.providers.index.milvus import MilvusIndexProvider
from atlasrag.providers.index.models import MilvusChunkRecord
from atlasrag.providers.index.schema import (
    build_collection_schema,
    build_index_params,
)

pytestmark = pytest.mark.integration

EMBEDDING_DIMENSION = 3
SDK_TIMEOUT_SECONDS = 10.0
_TEST_COLLECTION = re.compile(r"\Aatlasrag_test_[0-9a-f]{32}\Z")
_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True)
class IsolatedMilvus:
    client: Any
    provider: MilvusIndexProvider
    collection_name: str


async def _sdk_call(
    operation: Callable[..., _ResultT],
    /,
    *args: object,
    **kwargs: object,
) -> _ResultT:
    return await asyncio.to_thread(operation, *args, **kwargs)


async def _settle_cleanup(operation: Callable[[], object]) -> None:
    """Finish owned cleanup before propagating fixture cancellation."""

    task = asyncio.create_task(asyncio.to_thread(operation))
    interrupted: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if interrupted is None:
                interrupted = error
    task.result()
    if interrupted is not None:
        raise interrupted


def _cleanup_test_collection(resource: IsolatedMilvus) -> None:
    name = resource.collection_name
    if _TEST_COLLECTION.fullmatch(name) is None:
        raise RuntimeError("refusing to clean a non-test Milvus collection")
    try:
        if resource.client.has_collection(
            collection_name=name,
            timeout=SDK_TIMEOUT_SECONDS,
        ):
            resource.client.drop_collection(
                collection_name=name,
                timeout=SDK_TIMEOUT_SECONDS,
            )
    finally:
        resource.client.close()


@pytest_asyncio.fixture
async def isolated_milvus(milvus_uri: str) -> AsyncIterator[IsolatedMilvus]:
    client = await _sdk_call(
        MilvusClient,
        uri=milvus_uri,
        timeout=SDK_TIMEOUT_SECONDS,
    )
    collection_name = f"atlasrag_test_{uuid4().hex}"
    resource = IsolatedMilvus(
        client=client,
        provider=MilvusIndexProvider(
            cast(Any, client),
            collection_name=collection_name,
            embedding_dimension=EMBEDDING_DIMENSION,
            timeout_seconds=SDK_TIMEOUT_SECONDS,
        ),
        collection_name=collection_name,
    )
    # This finally block is established before a test may create the collection.
    try:
        yield resource
    finally:
        await _settle_cleanup(lambda: _cleanup_test_collection(resource))


def _record(**overrides: object) -> MilvusChunkRecord:
    values: dict[str, object] = {
        "chunk_id": "child-alpha",
        "document_id": "doc-a",
        "revision_id": "rev-1",
        "file_name": "manual.md",
        "parent_id": "parent-a",
        "chunk_type": "TEXT_CHILD",
        "source_type": "MD",
        "content": "Alpha architecture content.",
        "sparse_text": "alpha architecture canonical",
        "dense_vector": [1.0, 0.0, 0.0],
        "structured_metadata": {"section": "architecture"},
    }
    values.update(overrides)
    return MilvusChunkRecord.model_validate(values)


def _search_ids(response: object) -> tuple[str, ...]:
    assert isinstance(response, list) and len(response) == 1
    hits = response[0]
    assert isinstance(hits, list)
    identities: list[str] = []
    for hit in hits:
        assert isinstance(hit, dict)
        identity = hit.get("chunk_id")
        assert isinstance(identity, str)
        identities.append(identity)
    return tuple(identities)


@pytest.mark.asyncio
async def test_collection_creation_and_compatible_reopen(
    isolated_milvus: IsolatedMilvus,
) -> None:
    resource = isolated_milvus
    assert (
        await _sdk_call(
            resource.client.has_collection,
            collection_name=resource.collection_name,
        )
        is False
    )

    await resource.provider.initialize()
    assert (
        await _sdk_call(
            resource.client.has_collection,
            collection_name=resource.collection_name,
        )
        is True
    )

    await resource.provider.initialize()
    await resource.provider.health()


@pytest.mark.asyncio
async def test_incompatible_collection_fails_closed_and_remains_present(
    isolated_milvus: IsolatedMilvus,
) -> None:
    resource = isolated_milvus
    await _sdk_call(
        resource.client.create_collection,
        collection_name=resource.collection_name,
        schema=build_collection_schema(EMBEDDING_DIMENSION + 1),
        index_params=build_index_params(),
        timeout=SDK_TIMEOUT_SECONDS,
    )

    with pytest.raises(ConfigurationError):
        await resource.provider.initialize()

    assert (
        await _sdk_call(
            resource.client.has_collection,
            collection_name=resource.collection_name,
        )
        is True
    )


@pytest.mark.asyncio
async def test_exact_set_validation_enumeration_and_delete_visibility(
    isolated_milvus: IsolatedMilvus,
) -> None:
    resource = isolated_milvus
    await resource.provider.initialize()
    child = _record()
    table = _record(
        chunk_id="table-beta",
        parent_id=None,
        chunk_type="TABLE",
        source_type="XLSX",
        content="Beta persistence table.",
        sparse_text="beta persistence table",
        dense_vector=[0.0, 1.0, 0.0],
    )

    await resource.provider.replace_revision_set(
        [child, table],
        document_id="doc-a",
        revision_id="rev-1",
    )
    assert await resource.provider.count_revision("doc-a", "rev-1") == 2
    assert await resource.provider.list_revision_chunk_ids("doc-a", "rev-1") == (
        "child-alpha",
        "table-beta",
    )

    with pytest.raises(ValidationError):
        _record(chunk_type="TEXT_PARENT")
    with pytest.raises(ValidationError):
        _record(dense_vector=[0.0, float("inf"), 0.0])
    with pytest.raises(InvariantViolationError):
        await resource.provider.replace_revision_set(
            [_record(dense_vector=[1.0, 0.0])],
            document_id="doc-a",
            revision_id="rev-1",
        )
    assert await resource.provider.count_revision("doc-a", "rev-1") == 2

    replacement = _record(
        chunk_id="child-replacement",
        content="Replacement content.",
        sparse_text="replacement content",
    )
    await resource.provider.replace_revision_set(
        [replacement],
        document_id="doc-a",
        revision_id="rev-1",
    )
    assert await resource.provider.count_revision("doc-a", "rev-1") == 1
    assert await resource.provider.list_revision_chunk_ids("doc-a", "rev-1") == (
        "child-replacement",
    )

    await resource.provider.replace_revision_set(
        [
            _record(
                chunk_id="child-rev-2",
                revision_id="rev-2",
            )
        ],
        document_id="doc-a",
        revision_id="rev-2",
    )
    await resource.provider.replace_revision_set(
        [
            _record(
                chunk_id="child-doc-b",
                document_id="doc-b",
                parent_id="parent-b",
            )
        ],
        document_id="doc-b",
        revision_id="rev-1",
    )

    assert await resource.provider.delete_revision("doc-a", "rev-2") == 1
    assert await resource.provider.count_revision("doc-a", "rev-2") == 0
    assert await resource.provider.delete_document("doc-a") == 1
    assert await resource.provider.count_revision("doc-a", "rev-1") == 0
    assert await resource.provider.count_revision("doc-b", "rev-1") == 1
    assert await resource.provider.delete_document("doc-b") == 1
    assert await resource.provider.count_revision("doc-b", "rev-1") == 0


@pytest.mark.asyncio
async def test_dense_hnsw_and_builtin_bm25_search_expected_ids(
    isolated_milvus: IsolatedMilvus,
) -> None:
    resource = isolated_milvus
    await resource.provider.initialize()
    await resource.provider.replace_revision_set(
        [
            _record(),
            _record(
                chunk_id="table-beta",
                parent_id=None,
                chunk_type="TABLE",
                source_type="XLSX",
                content="Beta persistence table.",
                sparse_text="beta persistence table",
                dense_vector=[0.0, 1.0, 0.0],
            ),
            _record(
                chunk_id="child-gamma",
                parent_id="parent-gamma",
                content="Gamma operations content.",
                sparse_text="gamma operations runbook",
                dense_vector=[-1.0, 0.0, 0.0],
            ),
        ],
        document_id="doc-a",
        revision_id="rev-1",
    )

    dense = await _sdk_call(
        resource.client.search,
        collection_name=resource.collection_name,
        data=[[1.0, 0.0, 0.0]],
        anns_field="dense_vector",
        search_params={"metric_type": "COSINE", "params": {"ef": 32}},
        limit=1,
        output_fields=["chunk_id"],
        consistency_level="Strong",
        timeout=SDK_TIMEOUT_SECONDS,
    )
    sparse = await _sdk_call(
        resource.client.search,
        collection_name=resource.collection_name,
        data=["persistence"],
        anns_field="sparse_vector",
        search_params={"metric_type": "BM25", "params": {}},
        limit=1,
        output_fields=["chunk_id"],
        consistency_level="Strong",
        timeout=SDK_TIMEOUT_SECONDS,
    )

    assert _search_ids(dense) == ("child-alpha",)
    assert _search_ids(sparse) == ("table-beta",)


class _UnreachableMilvusClient:
    """Use the real SDK lazily so connection failure crosses the provider boundary."""

    def has_collection(self, **kwargs: object) -> object:
        timeout = kwargs.get("timeout", 0.5)
        client = MilvusClient(uri="http://127.0.0.1:1", timeout=timeout)
        try:
            return client.has_collection(**kwargs)
        finally:
            client.close()


@pytest.mark.asyncio
async def test_unreachable_milvus_is_a_typed_failure_not_an_empty_result() -> None:
    provider = MilvusIndexProvider(
        cast(Any, _UnreachableMilvusClient()),
        collection_name=f"atlasrag_test_{uuid4().hex}",
        embedding_dimension=EMBEDDING_DIMENSION,
        timeout_seconds=0.5,
    )

    with pytest.raises((DependencyUnavailableError, DependencyTimeoutError)):
        await provider.initialize()
