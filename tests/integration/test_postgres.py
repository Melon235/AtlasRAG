"""Real PostgreSQL verification for Stage 2 canonical persistence."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from psycopg import AsyncConnection, IntegrityError
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

from atlasrag.domain.enums import SourceType
from atlasrag.domain.errors import (
    DependencyTimeoutError,
    DependencyUnavailableError,
    InvariantViolationError,
)
from atlasrag.repositories.postgres.documents import DocumentRepository
from atlasrag.repositories.postgres.locks import (
    PostgresAdvisoryLockManager,
    advisory_lock_key,
)
from atlasrag.repositories.postgres.pool import PostgresPool
from atlasrag.repositories.postgres.records import (
    IngestionStatus,
    StoredChunk,
    StoredChunkType,
    StoredDocument,
    StoredElement,
)
from atlasrag.repositories.postgres.uow import PostgresUnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
EXPECTED_TABLES = {
    "chunks",
    "document_elements",
    "documents",
    "runtime_metadata",
}
EXPECTED_INDEXES = {
    "ix_chunks_chunk_type",
    "ix_chunks_document_revision",
    "ix_chunks_parent_id",
    "ix_document_elements_document_revision",
    "ix_documents_building_revision_id",
    "ix_documents_current_revision_id",
    "pk_chunks",
    "pk_document_elements",
    "pk_documents",
    "pk_runtime_metadata",
    "uq_chunks_identity_revision",
    "uq_documents_relative_source_path",
}


def stored_document(
    document_id: str,
    *,
    path: str | None = None,
    current: bool = True,
    building: bool = True,
) -> StoredDocument:
    return StoredDocument(
        document_id=document_id,
        file_name=f"{document_id}.md",
        relative_source_path=path or f"manuals/{document_id}.md",
        source_type=SourceType.MD,
        observed_content_hash=f"observed-{document_id}",
        current_revision_id=f"current-{document_id}" if current else None,
        current_content_hash=f"current-hash-{document_id}" if current else None,
        current_pipeline_fingerprint=f"current-pipeline-{document_id}"
        if current
        else None,
        building_revision_id=f"building-{document_id}" if building else None,
        building_content_hash=f"building-hash-{document_id}" if building else None,
        building_pipeline_fingerprint=f"building-pipeline-{document_id}"
        if building
        else None,
        ingestion_status=IngestionStatus.CHUNKING,
        parse_metadata={"parser": "stage-2", "pages": 3},
        created_at=NOW,
        updated_at=NOW,
    )


def stored_element(
    element_id: str,
    document_id: str,
    revision_id: str,
    *,
    order_index: int = 0,
    content: str | None = None,
) -> StoredElement:
    return StoredElement(
        element_id=element_id,
        document_id=document_id,
        revision_id=revision_id,
        element_type="paragraph",
        order_index=order_index,
        content=content or f"element content for {element_id}",
        section_path=("Architecture",),
        structured_content={"kind": "paragraph"},
        metadata={"source": "integration"},
        created_at=NOW,
    )


def stored_chunk(
    chunk_id: str,
    document_id: str,
    revision_id: str,
    chunk_type: StoredChunkType,
    *,
    parent_id: str | None = None,
    content: str | None = None,
) -> StoredChunk:
    return StoredChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        revision_id=revision_id,
        chunk_type=chunk_type,
        parent_id=parent_id,
        content=content or f"chunk content for {chunk_id}",
        section_path=("Architecture",),
        metadata={"source": "integration"},
        strategy_metadata={"strategy": "fixed"},
        created_at=NOW,
    )


async def put_documents(
    pool: PostgresPool,
    *documents: StoredDocument,
) -> None:
    async with PostgresUnitOfWork(pool) as uow:
        for document in documents:
            await uow.documents.put(document)
        await uow.commit()


async def revision_ids(
    pool: PostgresPool,
    document_id: str,
    revision_id: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    async with PostgresUnitOfWork(pool) as uow:
        elements = await uow.elements.list_revision_ids(document_id, revision_id)
        chunks = await uow.chunks.list_revision_ids(document_id, revision_id)
    return elements, chunks


async def test_fresh_database_reaches_head_with_exact_schema(
    postgres_dsn: str,
) -> None:
    connection = await AsyncConnection.connect(postgres_dsn, row_factory=dict_row)
    try:
        version_cursor = await connection.execute(
            "SELECT version_num FROM alembic_version"
        )
        version = await version_cursor.fetchone()
        assert version == {"version_num": "20260928_0001"}

        table_cursor = await connection.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = current_schema()
              AND tablename <> 'alembic_version'
            """
        )
        assert {row["tablename"] for row in await table_cursor.fetchall()} == (
            EXPECTED_TABLES
        )

        index_cursor = await connection.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = ANY(%s)
            """,
            (sorted(EXPECTED_TABLES),),
        )
        assert {row["indexname"] for row in await index_cursor.fetchall()} == (
            EXPECTED_INDEXES
        )
    finally:
        await connection.close()


async def test_document_round_trip_uniqueness_and_status_constraint(
    postgres_pool: PostgresPool,
    postgres_dsn: str,
) -> None:
    document = stored_document("doc-roundtrip", path="manuals/shared.md")
    await put_documents(postgres_pool, document)

    async with PostgresUnitOfWork(postgres_pool) as uow:
        assert await uow.documents.get_by_id(document.document_id) == document
        assert (
            await uow.documents.get_by_relative_source_path(
                document.relative_source_path
            )
            == document
        )
        assert await uow.documents.list() == (document,)

    duplicate_path = stored_document(
        "doc-duplicate",
        path=document.relative_source_path,
    )
    with pytest.raises(InvariantViolationError):
        async with PostgresUnitOfWork(postgres_pool) as uow:
            await uow.documents.put(duplicate_path)
            await uow.commit()

    async with PostgresUnitOfWork(postgres_pool) as uow:
        assert await uow.documents.get_by_id(duplicate_path.document_id) is None

    connection = await AsyncConnection.connect(postgres_dsn)
    try:
        with pytest.raises(IntegrityError):
            await connection.execute(
                """
                UPDATE documents
                SET ingestion_status = 'NOT_A_STATUS'
                WHERE document_id = %s
                """,
                (document.document_id,),
            )
        await connection.rollback()
    finally:
        await connection.close()


async def test_elements_and_chunks_are_revision_scoped_and_deferred(
    postgres_pool: PostgresPool,
) -> None:
    document_id = "doc-scoped"
    revision_one = "revision-one"
    revision_two = "revision-two"
    await put_documents(postgres_pool, stored_document(document_id))

    revision_one_elements = (stored_element("element-one", document_id, revision_one),)
    revision_two_elements = (stored_element("element-two", document_id, revision_two),)
    child = stored_chunk(
        "a-child",
        document_id,
        revision_one,
        StoredChunkType.TEXT_CHILD,
        parent_id="z-parent",
    )
    parent = stored_chunk(
        "z-parent",
        document_id,
        revision_one,
        StoredChunkType.TEXT_PARENT,
    )
    table = stored_chunk(
        "table-two",
        document_id,
        revision_two,
        StoredChunkType.TABLE,
    )

    async with PostgresUnitOfWork(postgres_pool) as uow:
        await uow.elements.replace_revision_set(
            document_id,
            revision_one,
            revision_one_elements,
        )
        await uow.elements.replace_revision_set(
            document_id,
            revision_two,
            revision_two_elements,
        )
        await uow.chunks.replace_revision_set(
            document_id,
            revision_one,
            (child, parent),
        )
        await uow.chunks.replace_revision_set(
            document_id,
            revision_two,
            (table,),
        )
        await uow.commit()

    async with PostgresUnitOfWork(postgres_pool) as uow:
        assert (
            await uow.elements.list_for_revision(document_id, revision_one)
            == revision_one_elements
        )
        assert (
            await uow.elements.list_for_revision(document_id, revision_two)
            == revision_two_elements
        )
        assert await uow.chunks.list_revision_ids(document_id, revision_one) == (
            "a-child",
            "z-parent",
        )
        assert await uow.chunks.list_revision_ids(document_id, revision_two) == (
            "table-two",
        )
        assert (
            await uow.chunks.get_canonical_context(
                document_id,
                revision_one,
                parent.chunk_id,
            )
            == parent
        )


@pytest.mark.parametrize(
    "invalid_parent",
    ["missing", "cross_document", "cross_revision", "wrong_type"],
)
async def test_invalid_chunk_parent_is_rejected_at_commit(
    postgres_pool: PostgresPool,
    invalid_parent: str,
) -> None:
    target_document = "doc-target"
    other_document = "doc-other"
    target_revision = "revision-target"
    await put_documents(
        postgres_pool,
        stored_document(target_document),
        stored_document(other_document),
    )

    parent_id = f"parent-{invalid_parent}"
    if invalid_parent == "cross_document":
        async with PostgresUnitOfWork(postgres_pool) as uow:
            await uow.chunks.replace_revision_set(
                other_document,
                target_revision,
                (
                    stored_chunk(
                        parent_id,
                        other_document,
                        target_revision,
                        StoredChunkType.TEXT_PARENT,
                    ),
                ),
            )
            await uow.commit()
    elif invalid_parent == "cross_revision":
        async with PostgresUnitOfWork(postgres_pool) as uow:
            await uow.chunks.replace_revision_set(
                target_document,
                "revision-other",
                (
                    stored_chunk(
                        parent_id,
                        target_document,
                        "revision-other",
                        StoredChunkType.TEXT_PARENT,
                    ),
                ),
            )
            await uow.commit()

    child = stored_chunk(
        f"child-{invalid_parent}",
        target_document,
        target_revision,
        StoredChunkType.TEXT_CHILD,
        parent_id=parent_id,
    )
    candidate_set: tuple[StoredChunk, ...] = (child,)
    if invalid_parent == "wrong_type":
        candidate_set = (
            child,
            stored_chunk(
                parent_id,
                target_document,
                target_revision,
                StoredChunkType.TABLE,
            ),
        )

    with pytest.raises(InvariantViolationError):
        async with PostgresUnitOfWork(postgres_pool) as uow:
            await uow.chunks.replace_revision_set(
                target_document,
                target_revision,
                candidate_set,
            )
            await uow.commit()

    async with PostgresUnitOfWork(postgres_pool) as uow:
        assert (
            await uow.chunks.count_revision(
                target_document,
                target_revision,
            )
            == 0
        )


async def test_committed_exact_sets_remove_stale_rows(
    postgres_pool: PostgresPool,
) -> None:
    document_id = "doc-exact"
    revision_id = "revision-exact"
    await put_documents(postgres_pool, stored_document(document_id))

    async with PostgresUnitOfWork(postgres_pool) as uow:
        await uow.elements.replace_revision_set(
            document_id,
            revision_id,
            (
                stored_element("element-old", document_id, revision_id),
                stored_element(
                    "element-retained",
                    document_id,
                    revision_id,
                    order_index=1,
                ),
            ),
        )
        await uow.chunks.replace_revision_set(
            document_id,
            revision_id,
            (
                stored_chunk(
                    "parent-old",
                    document_id,
                    revision_id,
                    StoredChunkType.TEXT_PARENT,
                ),
                stored_chunk(
                    "child-old",
                    document_id,
                    revision_id,
                    StoredChunkType.TEXT_CHILD,
                    parent_id="parent-old",
                ),
            ),
        )
        await uow.commit()

    async with PostgresUnitOfWork(postgres_pool) as uow:
        await uow.elements.replace_revision_set(
            document_id,
            revision_id,
            (
                stored_element(
                    "element-retained",
                    document_id,
                    revision_id,
                    content="updated",
                ),
                stored_element(
                    "element-new",
                    document_id,
                    revision_id,
                    order_index=1,
                ),
            ),
        )
        await uow.chunks.replace_revision_set(
            document_id,
            revision_id,
            (
                stored_chunk(
                    "table-new",
                    document_id,
                    revision_id,
                    StoredChunkType.TABLE,
                ),
            ),
        )
        await uow.commit()

    assert await revision_ids(postgres_pool, document_id, revision_id) == (
        ("element-new", "element-retained"),
        ("table-new",),
    )


@pytest.mark.parametrize(
    "rollback_trigger",
    ["no_commit", "exception", "repository_failure"],
)
async def test_uncommitted_or_failed_exact_set_restores_previous_rows(
    postgres_pool: PostgresPool,
    rollback_trigger: str,
) -> None:
    document_id = "doc-rollback"
    revision_id = "revision-rollback"
    document = stored_document(document_id, path="manuals/rollback.md")
    await put_documents(postgres_pool, document)

    async with PostgresUnitOfWork(postgres_pool) as uow:
        await uow.elements.replace_revision_set(
            document_id,
            revision_id,
            (stored_element("element-old", document_id, revision_id),),
        )
        await uow.chunks.replace_revision_set(
            document_id,
            revision_id,
            (
                stored_chunk(
                    "parent-old",
                    document_id,
                    revision_id,
                    StoredChunkType.TEXT_PARENT,
                ),
                stored_chunk(
                    "child-old",
                    document_id,
                    revision_id,
                    StoredChunkType.TEXT_CHILD,
                    parent_id="parent-old",
                ),
            ),
        )
        await uow.commit()

    async def replace_inside_uow() -> None:
        async with PostgresUnitOfWork(postgres_pool) as uow:
            await uow.elements.replace_revision_set(
                document_id,
                revision_id,
                (stored_element("element-new", document_id, revision_id),),
            )
            await uow.chunks.replace_revision_set(
                document_id,
                revision_id,
                (
                    stored_chunk(
                        "table-new",
                        document_id,
                        revision_id,
                        StoredChunkType.TABLE,
                    ),
                ),
            )
            if rollback_trigger == "exception":
                raise RuntimeError("injected application failure")
            if rollback_trigger == "repository_failure":
                await uow.documents.put(
                    stored_document(
                        "doc-conflict",
                        path=document.relative_source_path,
                    )
                )

    if rollback_trigger == "exception":
        with pytest.raises(RuntimeError, match="injected application failure"):
            await replace_inside_uow()
    elif rollback_trigger == "repository_failure":
        with pytest.raises(InvariantViolationError):
            await replace_inside_uow()
    else:
        await replace_inside_uow()

    assert await revision_ids(postgres_pool, document_id, revision_id) == (
        ("element-old",),
        ("child-old", "parent-old"),
    )


async def test_runtime_marker_survives_pool_reconnect(
    postgres_dsn: str,
) -> None:
    first_pool = PostgresPool(postgres_dsn, min_size=1, max_size=2, timeout=2.0)
    await first_pool.open()
    try:
        async with PostgresUnitOfWork(first_pool) as uow:
            assert not (
                await uow.runtime_metadata.get()
            ).query_cache_invalidation_required
            marker = await uow.runtime_metadata.set_query_cache_invalidation_required(
                True
            )
            assert marker.query_cache_invalidation_required
            await uow.commit()
    finally:
        await first_pool.close()

    second_pool = PostgresPool(postgres_dsn, min_size=1, max_size=2, timeout=2.0)
    await second_pool.open()
    try:
        async with PostgresUnitOfWork(second_pool) as uow:
            assert (await uow.runtime_metadata.get()).query_cache_invalidation_required
    finally:
        await second_pool.close()


async def test_postgres_unavailable_and_pool_timeout_are_typed(
    postgres_dsn: str,
) -> None:
    connection = await AsyncConnection.connect(postgres_dsn, row_factory=dict_row)
    repository = DocumentRepository(connection)
    await connection.close()
    with pytest.raises(DependencyUnavailableError):
        await repository.list()

    unreachable_url = make_url(postgres_dsn).set(
        host="127.0.0.1",
        port=1,
    )
    unreachable_url = unreachable_url.update_query_dict({"connect_timeout": "1"})
    pool = PostgresPool(
        unreachable_url.render_as_string(hide_password=False),
        min_size=1,
        max_size=1,
        timeout=0.05,
    )
    try:
        with pytest.raises(DependencyTimeoutError):
            await pool.open()
    finally:
        await pool.close()


async def test_advisory_locks_contend_by_document_and_release_explicitly(
    postgres_pool: PostgresPool,
) -> None:
    manager = PostgresAdvisoryLockManager(postgres_pool)
    first = await manager.try_acquire("document-lock-one")
    assert first is not None
    assert await manager.try_acquire("document-lock-one") is None

    second, third = await asyncio.gather(
        manager.try_acquire("document-lock-two"),
        manager.try_acquire("document-lock-three"),
    )
    assert second is not None
    assert third is not None
    await second.release()
    await third.release()

    await first.release()
    await first.release()
    reacquired = await manager.try_acquire("document-lock-one")
    assert reacquired is not None
    await reacquired.release()


async def test_closing_session_releases_advisory_lock(
    postgres_pool: PostgresPool,
    postgres_dsn: str,
) -> None:
    document_id = "document-session-close"
    connection = await AsyncConnection.connect(postgres_dsn)
    await connection.execute(
        "SELECT pg_advisory_lock(%s)",
        (advisory_lock_key(document_id),),
    )

    manager = PostgresAdvisoryLockManager(postgres_pool)
    assert await manager.try_acquire(document_id) is None
    await connection.close()

    acquired = await manager.try_acquire(document_id)
    assert acquired is not None
    await acquired.release()
