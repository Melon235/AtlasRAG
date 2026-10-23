"""Real session-lock coordination, cancellation, and actual runner lease loss."""

import asyncio
import re
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from sqlalchemy.engine import make_url

from atlasrag.domain.errors import DependencyUnavailableError
from atlasrag.domain.ingestion.source import CandidateBuildContext, SourceFile
from atlasrag.graphs.indexing.contracts import BuildResult
from atlasrag.providers.index.milvus import MilvusIndexProvider
from atlasrag.providers.parsing.routing import ParserRouter
from atlasrag.repositories.postgres.locks import (
    AdvisoryLockLease,
    PostgresAdvisoryLockManager,
)
from atlasrag.repositories.postgres.pool import PostgresPool
from atlasrag.repositories.postgres.records import IngestionStatus
from atlasrag.services.indexing.build import DocumentBuildServices
from integration.stage4_helpers import (
    Factory,
    assert_last_good,
    clear_marker,
    marker,
    request,
    resources,
    runner,
    snapshot,
)
from integration.test_milvus import IsolatedMilvus
from integration.test_milvus import isolated_milvus as isolated_milvus

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "same_document", [True, False], ids=["same-document", "different-documents"]
)
async def test_actual_runner_builds_respect_real_document_lock_boundaries(
    postgres_pool: PostgresPool,
    postgres_uow_factory: Factory,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
    same_document: bool,
) -> None:
    provider = MilvusIndexProvider(
        isolated_milvus.client,
        collection_name=isolated_milvus.collection_name,
        embedding_dimension=3,
        timeout_seconds=30.0,
    )
    # Match DocumentBuildOwner.open(): initialize shared resources before any
    # concurrent raw-runner invocations can race collection creation.
    await provider.initialize()
    bundle = resources(postgres_uow_factory, provider)
    first = request(tmp_path, bundle, "First concurrent document.")
    second = (
        first
        if same_document
        else request(tmp_path, bundle, "Second concurrent document.")
    )
    entered, both_entered, contended, release = (asyncio.Event() for _ in range(4))
    calls: list[str] = []
    parser = bundle.parsers.parser_for(first.source)

    class BarrierParser:
        async def parse(
            self, source: SourceFile, context: CandidateBuildContext
        ) -> Any:
            document = await parser.parse(source, context)
            calls.append(context.document_id)
            ordinal = len(calls)
            entered.set()
            if ordinal == 2:
                both_entered.set()
            await release.wait()
            if same_document and ordinal == 1:
                # A failed first attempt lets the waiting actual build retry the
                # same candidate without changing source bytes under its lease.
                raise TimeoutError("controlled first attempt")
            return document

    class ContentionManager:
        def __init__(self, pool: Any) -> None:
            self.manager = PostgresAdvisoryLockManager(pool)

        async def try_acquire(self, document_id: str) -> Any:
            lease = await self.manager.try_acquire(document_id)
            if lease is None:
                contended.set()
            return lease

    parser_probe = BarrierParser()
    bundle = replace(
        bundle,
        parsers=ParserRouter(parser_probe, parser_probe, parser_probe, parser_probe),
    )
    build_runner = runner(bundle, postgres_pool, lock_manager_factory=ContentionManager)
    tasks: list[asyncio.Task[Any]] = []
    async with asyncio.timeout(60):
        try:
            tasks.append(asyncio.create_task(build_runner.build(first)))
            await entered.wait()
            tasks.append(asyncio.create_task(build_runner.build(second)))
            if same_document:
                await contended.wait()
                assert len(calls) == 1
            else:
                await both_entered.wait()
                assert len(set(calls)) == 2
                assert not contended.is_set()
            release.set()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            if same_document:
                assert isinstance(outcomes[0], TimeoutError)
                assert isinstance(outcomes[1], BuildResult)
                assert outcomes[1].outcome == "READY"
                assert calls[0] == calls[1]
            else:
                for result in outcomes:
                    assert isinstance(result, BuildResult)
                    assert result.outcome == "READY"
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_sessions_serialize_same_document_and_allow_other_document(
    postgres_pool: PostgresPool,
) -> None:
    document_id = f"owned-{uuid4().hex}"
    other_id = f"owned-{uuid4().hex}"
    manager = PostgresAdvisoryLockManager(postgres_pool)
    held, inspected, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def owner() -> None:
        lease = await manager.try_acquire(document_id)
        assert lease is not None
        try:
            held.set()
            await release.wait()
        finally:
            await lease.release()

    async def contender() -> None:
        await held.wait()
        assert await manager.try_acquire(document_id) is None
        other = await manager.try_acquire(other_id)
        assert other is not None
        try:
            # Independent document owns another actual PG session concurrently.
            assert await manager.try_acquire(other_id) is None
            inspected.set()
            await release.wait()
        finally:
            await other.release()

    async with asyncio.timeout(30):
        tasks = [asyncio.create_task(owner()), asyncio.create_task(contender())]
        try:
            await inspected.wait()
        finally:
            release.set()
            await asyncio.gather(*tasks)
    lease = await manager.try_acquire(document_id)
    assert lease is not None
    await lease.release()


@pytest.mark.asyncio
async def test_runner_cancellation_holds_real_lease_until_inflight_parser_settles(
    postgres_pool: PostgresPool,
    postgres_uow_factory: Factory,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
) -> None:
    bundle = resources(postgres_uow_factory, isolated_milvus.provider)
    build_request = request(tmp_path, bundle, "Cancellation source.")
    parser = bundle.parsers.parser_for(build_request.source)
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_snapshots: list[bool] = []

    class HeldParser:
        async def parse(
            self, source: SourceFile, context: CandidateBuildContext
        ) -> Any:
            document = await parser.parse(source, context)
            entered.set()
            await release.wait()
            # Only normal parser completion counts; cancellation must not set it.
            settled.set()
            return document

    class ObservedLease:
        def __init__(self, lease: AdvisoryLockLease) -> None:
            self.lease = lease

        async def release(self) -> None:
            release_snapshots.append(settled.is_set())
            # Observe without asserting here: always release the actual session.
            await self.lease.release()

    class ObservedManager:
        def __init__(self, pool: Any) -> None:
            self.manager = PostgresAdvisoryLockManager(pool)

        async def try_acquire(self, document_id: str) -> ObservedLease | None:
            lease = await self.manager.try_acquire(document_id)
            return None if lease is None else ObservedLease(lease)

    held = HeldParser()
    bundle = replace(bundle, parsers=ParserRouter(held, held, held, held))
    from atlasrag.domain.ingestion.source import derive_document_id

    document_id = derive_document_id(build_request.source.relative_source_path)
    manager = PostgresAdvisoryLockManager(postgres_pool)
    async with asyncio.timeout(30):
        task = asyncio.create_task(
            runner(bundle, postgres_pool, lock_manager_factory=ObservedManager).build(
                build_request
            )
        )
        try:
            await entered.wait()
            task.cancel()
            # A real lock attempt forms the coordination barrier; no sleeps.
            assert await manager.try_acquire(document_id) is None
            assert not task.done()
        finally:
            release.set()
            if not task.done():
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert release_snapshots == [True]
    lease = await manager.try_acquire(document_id)
    assert lease is not None
    await lease.release()
    async with postgres_uow_factory() as unit:
        document = await unit.documents.get_by_id(document_id)
        assert (
            document is not None
            and document.ingestion_status is not IngestionStatus.FAILED
        )
        assert document.current_revision_id is None and document.failed_stage is None
    assert await marker(postgres_uow_factory) is False


class ObservedPool:
    """Observe only public pool methods; this is the exact manager session."""

    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool
        self.connections: list[AsyncConnection[DictRow]] = []
        self.returned: list[AsyncConnection[DictRow]] = []

    async def getconn(self) -> AsyncConnection[DictRow]:
        connection = await self.pool.getconn()
        self.connections.append(connection)
        return connection

    async def putconn(self, connection: AsyncConnection[DictRow]) -> None:
        self.returned.append(connection)
        await self.pool.putconn(connection)


@pytest.mark.asyncio
async def test_actual_runner_lost_lease_session_after_verify_cannot_publish(
    postgres_pool: PostgresPool,
    postgres_uow_factory: Factory,
    postgres_dsn: str,
    isolated_milvus: IsolatedMilvus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, index = postgres_uow_factory, isolated_milvus.provider
    bundle = resources(factory, index)
    build_request = request(tmp_path, bundle, "Last-good lease-loss source.")
    first = await runner(bundle, postgres_pool).build(build_request)
    before = await snapshot(factory, index, first.document_id)
    await clear_marker(factory)
    build_request.source.absolute_path.write_text(
        "Changed candidate for lease loss.", encoding="utf-8"
    )
    observed = ObservedPool(postgres_pool)
    verified, resume = asyncio.Event(), asyncio.Event()
    releases: list[str] = []
    acquisitions: list[str] = []
    original_verify = DocumentBuildServices.verify_candidate
    original_publish = DocumentBuildServices.publish_revision
    publish_calls: list[str] = []

    class ObservedManager:
        def __init__(self, pool: Any) -> None:
            # The runner passes its public capture adapter into this real manager.
            self.manager = PostgresAdvisoryLockManager(pool)

        async def try_acquire(self, document_id: str) -> Any:
            acquisitions.append(document_id)
            lease = await self.manager.try_acquire(document_id)
            assert lease is not None

            class ObservedLease:
                async def release(self) -> None:
                    releases.append(document_id)
                    assert lease is not None
                    await lease.release()

            return ObservedLease()

    async def after_verified(services: DocumentBuildServices) -> None:
        await original_verify(services)
        verified.set()
        await resume.wait()

    async def counted_publish(services: DocumentBuildServices) -> None:
        publish_calls.append(services.context.revision_id)
        await original_publish(services)

    monkeypatch.setattr(DocumentBuildServices, "verify_candidate", after_verified)
    monkeypatch.setattr(DocumentBuildServices, "publish_revision", counted_publish)
    async with asyncio.timeout(45):
        task = asyncio.create_task(
            runner(bundle, observed, lock_manager_factory=ObservedManager).build(
                build_request
            )
        )
        try:
            await verified.wait()
            assert len(observed.connections) == 1
            connection = observed.connections[0]
            pid = connection.info.backend_pid
            owned_database = make_url(postgres_dsn).database
            assert owned_database is not None
            assert re.fullmatch(r"atlasrag_test_[0-9a-f]{32}", owned_database)
            assert connection.info.dbname == owned_database
            # Public checkout gives a separate usable SQL connection while the
            # exact acquired advisory-lock backend is still owned by the runner.
            control = await postgres_pool.getconn()
            try:
                assert control is not connection and control.info.backend_pid != pid
                cursor = await control.execute(
                    "SELECT datname FROM pg_stat_activity WHERE pid = %s", (pid,)
                )
                assert await cursor.fetchone() == {"datname": owned_database}
                cursor = await control.execute(
                    "SELECT pg_terminate_backend(%s) AS terminated", (pid,)
                )
                assert await cursor.fetchone() == {"terminated": True}
                await control.rollback()
            finally:
                await postgres_pool.putconn(control)
            # Ordinary UoW health cannot stand in for the acquired lease probe.
            async with factory() as unit:
                document = await unit.documents.get_by_id(first.document_id)
                assert (
                    document is not None
                    and document.current_revision_id == first.revision_id
                )
                assert (
                    await unit.runtime_metadata.get()
                ).query_cache_invalidation_required is False
            resume.set()
            with pytest.raises(DependencyUnavailableError):
                await task
        finally:
            resume.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    after = await assert_last_good(factory, index, before)
    assert after.ingestion_status is not IngestionStatus.FAILED
    assert after.failed_stage is None and after.building_revision_id is not None
    assert await marker(factory) is False
    assert publish_calls == []
    assert acquisitions == [first.document_id]
    assert releases == [first.document_id]
    assert observed.returned == observed.connections
    # This proves checkpoint fail-closed behavior, not fencing after a good probe.
