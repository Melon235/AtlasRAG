"""Async boundary around PyMilvus's synchronous derived-index operations."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, TypeVar

from pydantic import TypeAdapter, ValidationError

from atlasrag.domain.errors import (
    ConfigurationError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    InvariantViolationError,
    ProviderResponseError,
)
from atlasrag.providers.index.models import (
    CanonicalId,
    MilvusChunkRecord,
    validate_milvus_record_set,
)
from atlasrag.providers.index.schema import (
    COLLECTION_NAME,
    build_collection_schema,
    build_index_params,
    collection_schema_mismatches,
    index_schema_mismatches,
)

_ResultT = TypeVar("_ResultT")
_CANONICAL_ID = TypeAdapter(CanonicalId)
_COLLECTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,254}\Z")
_MAX_QUERY_BATCH_SIZE = 16_384


class _MilvusClient(Protocol):
    def has_collection(self, **kwargs: object) -> object: ...

    def create_collection(self, **kwargs: object) -> object: ...

    def describe_collection(self, **kwargs: object) -> object: ...

    def list_indexes(self, **kwargs: object) -> object: ...

    def describe_index(self, **kwargs: object) -> object: ...

    def load_collection(self, **kwargs: object) -> object: ...

    def delete(self, **kwargs: object) -> object: ...

    def flush(self, **kwargs: object) -> object: ...

    def insert(self, **kwargs: object) -> object: ...

    def query(self, **kwargs: object) -> object: ...

    def query_iterator(self, **kwargs: object) -> object: ...

    def close(self) -> object: ...


async def _await_settled(task: asyncio.Task[_ResultT]) -> _ResultT:
    """Do not propagate cancellation while an owned operation is still running."""

    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        try:
            task.result()
        except (Exception, asyncio.CancelledError):
            pass
        raise


def _is_timeout_failure(error: Exception) -> bool:
    if isinstance(error, TimeoutError):
        return True
    code = getattr(error, "code", None)
    if callable(code):
        try:
            status = code()
        except Exception:
            status = None
        status_name = getattr(status, "name", status)
        if str(status_name).upper().endswith("DEADLINE_EXCEEDED"):
            return True
    text = f"{type(error).__name__} {error}".upper()
    return any(
        marker in text for marker in ("DEADLINE_EXCEEDED", "TIMED OUT", "TIMEOUT")
    )


async def _run_sync(
    operation: Callable[..., _ResultT],
    /,
    *args: object,
    **kwargs: object,
) -> _ResultT:
    """Run one SDK operation off-loop and translate only its exception boundary."""

    try:
        worker = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        return await _await_settled(worker)
    except Exception as error:
        if _is_timeout_failure(error):
            raise DependencyTimeoutError from None
        # PyMilvus may expose MilvusException, grpc failures, built-in socket
        # errors, or wrapped runtime errors depending on the failing operation.
        raise DependencyUnavailableError from None


def _validate_collection_name(value: object) -> str:
    if not isinstance(value, str) or _COLLECTION_NAME.fullmatch(value) is None:
        raise InvariantViolationError
    return value


def _validate_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise InvariantViolationError
    return float(value)


def _validate_batch_size(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_QUERY_BATCH_SIZE:
        raise InvariantViolationError
    return value


def _local_identifier(value: object) -> str:
    try:
        return _CANONICAL_ID.validate_python(value)
    except ValidationError:
        raise InvariantViolationError from None


def _response_identifier(value: object) -> str:
    try:
        return _CANONICAL_ID.validate_python(value)
    except ValidationError:
        raise ProviderResponseError from None


def _literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _revision_filter(document_id: str, revision_id: str) -> str:
    return (
        f"document_id == {_literal(document_id)} and "
        f"revision_id == {_literal(revision_id)}"
    )


def _document_filter(document_id: str) -> str:
    return f"document_id == {_literal(document_id)}"


def _expect_none(response: object) -> None:
    if response is not None:
        raise ProviderResponseError


def _validate_has_collection(response: object) -> bool:
    if type(response) is not bool:
        raise ProviderResponseError
    return response


def _validate_index_names(response: object) -> tuple[str, ...]:
    if not isinstance(response, (list, tuple)):
        raise ProviderResponseError
    names: list[str] = []
    seen: set[str] = set()
    for value in response:
        if not isinstance(value, str) or not value or value in seen:
            raise ProviderResponseError
        seen.add(value)
        names.append(value)
    return tuple(sorted(names))


def _validate_insert_response(
    response: object,
    expected_ids: tuple[str, ...],
) -> None:
    if not isinstance(response, Mapping):
        raise ProviderResponseError
    count = response.get("insert_count")
    raw_ids = response.get("ids")
    if type(count) is not int or count != len(expected_ids):
        raise ProviderResponseError
    if isinstance(raw_ids, (str, bytes, bytearray)) or not isinstance(
        raw_ids, Sequence
    ):
        raise ProviderResponseError
    actual_ids = tuple(_response_identifier(value) for value in raw_ids)
    if (
        len(actual_ids) != len(expected_ids)
        or len(set(actual_ids)) != len(actual_ids)
        or set(actual_ids) != set(expected_ids)
    ):
        raise ProviderResponseError


def _validate_delete_response(response: object) -> int:
    if not isinstance(response, Mapping):
        raise ProviderResponseError
    count = response.get("delete_count")
    if type(count) is not int or count < 0:
        raise ProviderResponseError
    return count


def _validate_count_response(response: object) -> int:
    if not isinstance(response, (list, tuple)) or len(response) != 1:
        raise ProviderResponseError
    row = response[0]
    if not isinstance(row, Mapping) or set(row) != {"count(*)"}:
        raise ProviderResponseError
    count = row["count(*)"]
    if type(count) is not int or count < 0:
        raise ProviderResponseError
    return count


def _validate_identity_page(
    response: object,
    *,
    batch_size: int,
) -> tuple[str, ...]:
    if not isinstance(response, (list, tuple)) or len(response) > batch_size:
        raise ProviderResponseError
    identities: list[str] = []
    for row in response:
        if not isinstance(row, Mapping) or set(row) != {"chunk_id"}:
            raise ProviderResponseError
        identities.append(_response_identifier(row["chunk_id"]))
    return tuple(identities)


def _record_payload(record: MilvusChunkRecord) -> dict[str, object]:
    serialized = record.model_dump(mode="json")
    metadata = serialized["structured_metadata"]
    if not isinstance(metadata, dict):
        raise InvariantViolationError
    return {
        "chunk_id": record.chunk_id,
        "document_id": record.document_id,
        "revision_id": record.revision_id,
        "file_name": record.file_name,
        "parent_id": record.parent_id,
        "chunk_type": record.chunk_type.value,
        "source_type": record.source_type.value,
        "content": record.content,
        "sparse_text": record.sparse_text,
        "dense_vector": list(record.dense_vector),
        "structured_metadata": metadata,
    }


class MilvusIndexProvider:
    """Technical async capability over the rebuildable Milvus index."""

    def __init__(
        self,
        client: _MilvusClient,
        *,
        embedding_dimension: int,
        analyzer: str = "standard",
        collection_name: str = COLLECTION_NAME,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._client = client
        self._collection_name = _validate_collection_name(collection_name)
        self._timeout_seconds = _validate_timeout(timeout_seconds)
        self._embedding_dimension = embedding_dimension
        self._analyzer = analyzer
        # Build eagerly so all local configuration errors precede SDK calls.
        self._schema = build_collection_schema(
            embedding_dimension,
            analyzer=analyzer,
        )
        self._index_params = build_index_params()

    async def _call(
        self,
        operation: Callable[..., _ResultT],
        /,
        **kwargs: object,
    ) -> _ResultT:
        return await _run_sync(
            operation,
            **kwargs,
            timeout=self._timeout_seconds,
        )

    async def initialize(self) -> None:
        """Create a missing collection or fail closed on any incompatibility."""

        exists = _validate_has_collection(
            await self._call(
                self._client.has_collection,
                collection_name=self._collection_name,
            )
        )
        if not exists:
            response = await self._call(
                self._client.create_collection,
                collection_name=self._collection_name,
                schema=self._schema,
                index_params=self._index_params,
            )
            _expect_none(response)
            return

        collection_description = await self._call(
            self._client.describe_collection,
            collection_name=self._collection_name,
        )
        index_names = _validate_index_names(
            await self._call(
                self._client.list_indexes,
                collection_name=self._collection_name,
            )
        )
        index_descriptions = []
        for index_name in index_names:
            index_descriptions.append(
                await self._call(
                    self._client.describe_index,
                    collection_name=self._collection_name,
                    index_name=index_name,
                )
            )

        collection_mismatches = collection_schema_mismatches(
            collection_description,
            embedding_dimension=self._embedding_dimension,
            analyzer=self._analyzer,
            collection_name=self._collection_name,
        )
        index_mismatches = index_schema_mismatches(index_descriptions)
        if collection_mismatches or index_mismatches:
            raise ConfigurationError

        response = await self._call(
            self._client.load_collection,
            collection_name=self._collection_name,
        )
        _expect_none(response)

    async def replace_revision_set(
        self,
        records: Sequence[MilvusChunkRecord],
        *,
        document_id: str,
        revision_id: str,
    ) -> None:
        """Replace one candidate revision with a complete validated exact set."""

        validated = validate_milvus_record_set(
            records,
            document_id=document_id,
            revision_id=revision_id,
            embedding_dimension=self._embedding_dimension,
        )
        payloads = [_record_payload(record) for record in validated]
        expected_ids = tuple(record.chunk_id for record in validated)
        expression = _revision_filter(document_id, revision_id)

        replacement = asyncio.create_task(
            self._replace_validated_set(
                payloads,
                expected_ids=expected_ids,
                expression=expression,
            )
        )
        await _await_settled(replacement)

    async def _replace_validated_set(
        self,
        payloads: list[dict[str, object]],
        *,
        expected_ids: tuple[str, ...],
        expression: str,
    ) -> None:
        """Finish a validated replacement before propagating caller cancellation."""

        deleted = await self._call(
            self._client.delete,
            collection_name=self._collection_name,
            filter=expression,
        )
        _validate_delete_response(deleted)
        await self._visibility_barrier()
        if not payloads:
            return

        inserted = await self._call(
            self._client.insert,
            collection_name=self._collection_name,
            data=payloads,
        )
        _validate_insert_response(inserted, expected_ids)
        await self._visibility_barrier()

    async def count_revision(self, document_id: str, revision_id: str) -> int:
        document_id = _local_identifier(document_id)
        revision_id = _local_identifier(revision_id)
        response = await self._call(
            self._client.query,
            collection_name=self._collection_name,
            filter=_revision_filter(document_id, revision_id),
            output_fields=["count(*)"],
            consistency_level="Strong",
        )
        return _validate_count_response(response)

    async def list_revision_chunk_ids(
        self,
        document_id: str,
        revision_id: str,
        *,
        batch_size: int = 1_000,
    ) -> tuple[str, ...]:
        document_id = _local_identifier(document_id)
        revision_id = _local_identifier(revision_id)
        batch_size = _validate_batch_size(batch_size)
        iterator = await self._call(
            self._client.query_iterator,
            collection_name=self._collection_name,
            filter=_revision_filter(document_id, revision_id),
            output_fields=["chunk_id"],
            batch_size=batch_size,
            limit=-1,
            consistency_level="Strong",
        )
        next_page = getattr(iterator, "next", None)
        close = getattr(iterator, "close", None)
        if not callable(close):
            raise ProviderResponseError

        identities: list[str] = []
        seen: set[str] = set()
        primary_error: BaseException | None = None
        try:
            if not callable(next_page):
                raise ProviderResponseError
            while True:
                page = _validate_identity_page(
                    await _run_sync(next_page),
                    batch_size=batch_size,
                )
                if not page:
                    break
                for identity in page:
                    if identity in seen:
                        raise ProviderResponseError
                    seen.add(identity)
                    identities.append(identity)
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                await _run_sync(close)
            except BaseException:
                if primary_error is None:
                    raise
        return tuple(sorted(identities))

    async def delete_revision(self, document_id: str, revision_id: str) -> int:
        document_id = _local_identifier(document_id)
        revision_id = _local_identifier(revision_id)
        response = await self._call(
            self._client.delete,
            collection_name=self._collection_name,
            filter=_revision_filter(document_id, revision_id),
        )
        deleted = _validate_delete_response(response)
        await self._visibility_barrier()
        return deleted

    async def delete_document(self, document_id: str) -> int:
        document_id = _local_identifier(document_id)
        response = await self._call(
            self._client.delete,
            collection_name=self._collection_name,
            filter=_document_filter(document_id),
        )
        deleted = _validate_delete_response(response)
        await self._visibility_barrier()
        return deleted

    async def health(self) -> None:
        exists = _validate_has_collection(
            await self._call(
                self._client.has_collection,
                collection_name=self._collection_name,
            )
        )
        if not exists:
            raise ConfigurationError

    async def close(self) -> None:
        _expect_none(await _run_sync(self._client.close))

    async def _visibility_barrier(self) -> None:
        response = await self._call(
            self._client.flush,
            collection_name=self._collection_name,
        )
        _expect_none(response)
