"""Whole-build leases, fresh hashing, checkpoint loss, and cancellation settlement."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from stage3.test_candidate_services import _document

from atlasrag.domain.errors import (
    ConfigurationError,
    DependencyUnavailableError,
    InvariantViolationError,
    SourceMutationError,
)
from atlasrag.repositories.postgres.records import RuntimeMetadata
from atlasrag.services.ingestion.identity import content_hash
from stage4.build_fakes import resources
from stage4.test_document_build import module


class Session:
    def __init__(self) -> None:
        self.dead = False
        self.probes = 0
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


class Pool:
    def __init__(self) -> None:
        self.held: dict[str, Session] = {}
        self.sessions: list[Session] = []
        self.events: list[tuple[str, str]] = []
        self.releases = 0
        self.release_error: BaseException | None = None
        self.contended = asyncio.Event()
        self.acquiring: asyncio.Event | None = None
        self.allow_acquire: asyncio.Event | None = None

    async def getconn(self) -> Session:
        connection = Session()
        self.sessions.append(connection)
        return connection

    async def putconn(self, connection: Session) -> None:
        pass

    async def health(self) -> None:
        raise AssertionError("pool.health cannot prove actual lease ownership")


class Metadata:
    def __init__(self, session: Session) -> None:
        self.session = session

    async def get(self) -> RuntimeMetadata:
        self.session.probes += 1
        if self.session.dead:
            raise DependencyUnavailableError()
        return RuntimeMetadata(query_cache_invalidation_required=False)


class Lease:
    def __init__(
        self, owner: Pool, pool: Any, connection: Session, document_id: str
    ) -> None:
        self.owner, self.pool, self.connection, self.document_id = (
            owner,
            pool,
            connection,
            document_id,
        )

    async def release(self) -> None:
        self.owner.releases += 1
        self.owner.events.append(("release", self.document_id))
        del self.owner.held[self.document_id]
        await self.pool.putconn(self.connection)
        if self.owner.release_error is not None:
            raise self.owner.release_error


class Manager:
    def __init__(self, owner: Pool, pool: Any) -> None:
        self.owner, self.pool = owner, pool

    async def try_acquire(self, document_id: str) -> Lease | None:
        connection = await self.pool.getconn()
        if document_id in self.owner.held:
            self.owner.contended.set()
            await self.pool.putconn(connection)
            return None
        self.owner.held[document_id] = connection
        self.owner.events.append(("acquire", document_id))
        if self.owner.acquiring is not None:
            self.owner.acquiring.set()
            assert self.owner.allow_acquire is not None
            await self.owner.allow_acquire.wait()
        return Lease(self.owner, self.pool, connection, document_id)


def runner(root: Path, *, concurrency: int = 2) -> tuple[Any, Any, Any, Any, Any, Any]:
    application = module("atlasrag.application.document_build")
    bundle, context, factory, parser, index = resources(root)
    pool = Pool()
    result = application.DocumentBuildRunner(
        resources=bundle,
        pool=pool,
        lock_manager_factory=lambda captured: Manager(pool, captured),
        metadata_repository_factory=Metadata,
        max_concurrent_builds=concurrency,
        pool_capacity=2 * concurrency,
        contention_poll_seconds=0.001,
    )
    request = module("atlasrag.graphs.indexing.contracts").BuildRequest(
        source=context.source,
        expected_pipeline_fingerprint=context.pipeline_fingerprint,
    )
    return result, request, pool, factory, parser, index


@pytest.mark.asyncio
async def test_lock_covers_success_and_probes_actual_session(tmp_path: Path) -> None:
    build, request, pool, _, _, _ = runner(tmp_path)
    result = await build.build(request)
    assert result.outcome == "READY"
    assert not pool.held and pool.releases == 1
    assert len(pool.sessions) == 1
    assert pool.sessions[0].probes >= 9
    assert pool.sessions[0].rollbacks == pool.sessions[0].probes


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["business", "technical", "mutation"])
async def test_release_once_for_all_exits(tmp_path: Path, kind: str) -> None:
    build, request, pool, _, parser, _ = runner(tmp_path)
    error = ValueError("primary")
    parser.failed = kind == "business"
    parser.mutate = kind == "mutation"
    if kind == "technical":
        parser.error = error
        pool.release_error = RuntimeError("cleanup")
        with pytest.raises(ValueError) as caught:
            await build.build(request)
        assert caught.value is error
    else:
        result = await build.build(request)
        assert result.outcome == ("FAILED" if kind == "business" else "SOURCE_MUTATED")
    assert not pool.held and pool.releases == 1


@pytest.mark.asyncio
async def test_hash_is_created_after_contention_wait(tmp_path: Path) -> None:
    build, request, pool, _, _, _ = runner(tmp_path)
    from atlasrag.domain.ingestion.source import derive_document_id, derive_revision_id

    document_id = derive_document_id(request.source.relative_source_path)
    pool.held[document_id] = Session()
    task = asyncio.create_task(build.build(request))
    await asyncio.wait_for(pool.contended.wait(), 3)
    request.source.absolute_path.write_text("new authoritative bytes", encoding="utf-8")
    expected_hash = content_hash(request.source)
    del pool.held[document_id]
    result = await task
    assert result.revision_id == derive_revision_id(
        document_id, expected_hash, request.expected_pipeline_fingerprint
    )
    assert pool.releases == 1


@pytest.mark.asyncio
async def test_cancellable_contention_has_no_candidate_or_release(
    tmp_path: Path,
) -> None:
    build, request, pool, factory, _, _ = runner(tmp_path)
    from atlasrag.domain.ingestion.source import derive_document_id

    pool.held[derive_document_id(request.source.relative_source_path)] = Session()
    task = asyncio.create_task(build.build(request))
    await asyncio.wait_for(pool.contended.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not factory.database.documents and pool.releases == 0


@pytest.mark.asyncio
async def test_cancel_during_acquisition_releases_eventual_lease_once(
    tmp_path: Path,
) -> None:
    build, request, pool, factory, _, _ = runner(tmp_path)
    pool.acquiring, pool.allow_acquire = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(build.build(request))
    await asyncio.wait_for(pool.acquiring.wait(), 3)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert pool.held and not task.done()
    pool.allow_acquire.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not pool.held and pool.releases == 1 and not factory.database.documents


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["parse", "hash", "chunk"])
async def test_actual_worker_settles_under_repeated_cancel_before_unlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    build, request, pool, factory, parser, _ = runner(tmp_path)
    entered, release = asyncio.Event(), threading.Event()
    settling, repeated = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    services = module("atlasrag.services.indexing.build")
    original_finish = services.finish_task

    async def observed_finish(task: Any) -> Any:
        settling.set()
        return await original_finish(task)

    monkeypatch.setattr(services, "finish_task", observed_finish)

    def barrier() -> None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test did not release worker"

    async def parse(source: Any, context: Any) -> Any:
        def work() -> Any:
            barrier()
            return _document(context, "completed worker")

        return await asyncio.to_thread(work)

    if operation == "parse":
        monkeypatch.setattr(parser, "parse", parse)
    else:
        target = (
            module("atlasrag.application.document_build")
            if operation == "hash"
            else services
        )
        name = "build_candidate_context" if operation == "hash" else "build_chunks"
        original = getattr(target, name)

        def work(*args: Any) -> Any:
            barrier()
            return original(*args)

        monkeypatch.setattr(target, name, work)
    task = asyncio.create_task(build.build(request))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await asyncio.wait_for(settling.wait(), 3)
        task.cancel()
        loop.call_soon(repeated.set)
        await repeated.wait()
        assert pool.held and pool.releases == 0 and not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.releases == 1 and not pool.held
    if operation == "hash":
        assert not factory.database.documents
    else:
        assert next(
            iter(factory.database.documents.values())
        ).ingestion_status.value == ("PARSING" if operation == "parse" else "CHUNKING")


@pytest.mark.asyncio
async def test_lease_loss_after_verify_prevents_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build, request, pool, factory, _, _ = runner(tmp_path)
    services = module("atlasrag.services.indexing.build").DocumentBuildServices
    original = services.verify_candidate

    async def verify(self: Any) -> None:
        await original(self)
        assert pool.held
        next(iter(pool.held.values())).dead = True

    monkeypatch.setattr(services, "verify_candidate", verify)
    with pytest.raises(DependencyUnavailableError):
        await build.build(request)
    stored = next(iter(factory.database.documents.values()))
    assert (
        stored.ingestion_status.value == "INDEXING"
        and stored.current_revision_id is None
    )
    assert pool.releases == 1 and not factory.database.query_cache_invalidation_required


@pytest.mark.asyncio
async def test_fingerprint_mismatch_fails_before_lock(tmp_path: Path) -> None:
    build, request, pool, _, _, _ = runner(tmp_path)
    changed = request.model_copy(update={"expected_pipeline_fingerprint": "a" * 64})
    with pytest.raises(ConfigurationError):
        await build.build(changed)
    assert not pool.sessions


def test_injected_pool_capacity_contract(tmp_path: Path) -> None:
    app = module("atlasrag.application.document_build")
    bundle, _, _, _, _ = resources(tmp_path)
    pool = Pool()
    with pytest.raises(ConfigurationError):
        app.DocumentBuildRunner(
            resources=bundle,
            pool=pool,
            lock_manager_factory=lambda captured: Manager(pool, captured),
            metadata_repository_factory=Metadata,
            max_concurrent_builds=2,
            pool_capacity=3,
        )


@pytest.mark.asyncio
async def test_process_owner_initializes_shared_providers_once_and_owns_pool_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = module("atlasrag.application.document_build")
    assert hasattr(app, "DocumentBuildOwner"), "missing explicit process resource owner"
    bundle, context, _, _, index = resources(tmp_path)
    events: list[str] = []
    capacities: list[int] = []

    class OwnedPool(Pool):
        def __init__(self, dsn: str, *, min_size: int, max_size: int) -> None:
            super().__init__()
            capacities.append(max_size)

        async def open(self) -> None:
            events.append("pool.open")

        async def close(self) -> None:
            events.append("pool.close")

    async def initialize(*, allow_download: bool = False) -> None:
        assert not allow_download
        events.append("embedding.initialize")

    async def close_index() -> None:
        events.append("index.close")

    monkeypatch.setattr(
        module("atlasrag.repositories.postgres.pool"), "PostgresPool", OwnedPool
    )
    monkeypatch.setattr(
        module("atlasrag.providers.embedding.qwen"),
        "QwenEmbeddingProvider",
        lambda **kwargs: bundle.embedding,
    )
    monkeypatch.setattr(bundle.embedding, "initialize", initialize, raising=False)
    monkeypatch.setattr(index, "close", close_index, raising=False)
    owner = app.DocumentBuildOwner(
        config=bundle.config, postgres_dsn="injected", index=index
    )
    request = module("atlasrag.graphs.indexing.contracts").BuildRequest(
        source=context.source,
        expected_pipeline_fingerprint=context.pipeline_fingerprint,
    )
    with pytest.raises(InvariantViolationError):
        await owner.build(request)
    observed: list[Any] = []

    async def build(self: Any, request: Any) -> Any:
        observed.append(self.resources.embedding)
        return module("atlasrag.graphs.indexing.contracts").BuildResult(
            document_id=context.document_id,
            revision_id=context.revision_id,
            outcome="READY",
        )

    monkeypatch.setattr(app.DocumentBuildRunner, "build", build)
    await owner.open()
    await owner.open()
    await owner.build(request)
    await owner.build(request)
    await owner.close()
    await owner.close()
    assert capacities == [2 * bundle.config.maintenance.max_document_concurrency]
    assert observed == [bundle.embedding, bundle.embedding]
    assert events == ["pool.open", "embedding.initialize", "index.close", "pool.close"]


@pytest.mark.asyncio
async def test_same_document_cannot_prepare_until_prior_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build, request, pool, _, parser, _ = runner(tmp_path)
    first_entered, finish_first = asyncio.Event(), asyncio.Event()
    calls = 0
    first_error = ValueError("first invocation")
    persistence = module("atlasrag.services.ingestion.persistence")
    original_prepare = persistence.prepare_candidate

    async def prepare(*args: Any, **kwargs: Any) -> Any:
        pool.events.append(("prepare", args[1].document_id))
        return await original_prepare(*args, **kwargs)

    async def parse(source: Any, context: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_entered.set()
            await finish_first.wait()
            raise first_error
        return _document(context, "second attempt")

    monkeypatch.setattr(persistence, "prepare_candidate", prepare)
    monkeypatch.setattr(parser, "parse", parse)
    first = asyncio.create_task(build.build(request))
    await asyncio.wait_for(first_entered.wait(), 3)
    second = asyncio.create_task(build.build(request))
    await asyncio.wait_for(pool.contended.wait(), 3)
    assert [event for event, _ in pool.events].count("prepare") == 1
    finish_first.set()
    with pytest.raises(ValueError) as caught:
        await first
    assert caught.value is first_error
    assert (await second).outcome == "READY"
    assert [event for event, _ in pool.events] == [
        "acquire",
        "prepare",
        "release",
        "acquire",
        "prepare",
        "release",
    ]


@pytest.mark.asyncio
async def test_different_documents_progress_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlasrag.domain.ingestion.source import SourceFile

    build, request, pool, factory, parser, _ = runner(tmp_path)
    path = tmp_path / "second.txt"
    path.write_text("independent", encoding="utf-8")
    second_request = module("atlasrag.graphs.indexing.contracts").BuildRequest(
        source=SourceFile.from_knowledge_directory(tmp_path, "second.txt"),
        expected_pipeline_fingerprint=request.expected_pipeline_fingerprint,
    )
    both, release = asyncio.Event(), asyncio.Event()
    entered: set[str] = set()

    async def parse(source: Any, context: Any) -> Any:
        entered.add(context.document_id)
        if len(entered) == 2:
            both.set()
        await release.wait()
        return _document(context, "independent document")

    monkeypatch.setattr(parser, "parse", parse)
    tasks = [
        asyncio.create_task(build.build(item)) for item in (request, second_request)
    ]
    try:
        await asyncio.wait_for(both.wait(), 3)
        assert len(pool.held) == 2
    finally:
        release.set()
    results = await asyncio.gather(*tasks)
    assert all(result.outcome == "READY" for result in results)
    assert len(factory.database.documents) == 2
    assert pool.releases == 2 and not pool.held


@pytest.mark.asyncio
async def test_cancel_embedding_stops_before_remaining_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from atlasrag.services.ingestion.chunking import ChunkingProfile

    build, request, pool, factory, _, index = runner(tmp_path)
    build.resources = replace(
        build.resources,
        chunking_profile=ChunkingProfile(parent_max_chars=100, child_max_chars=3),
    )
    embedding = build.resources.embedding
    entered, cancelled = asyncio.Event(), asyncio.Event()
    calls = 0

    async def embed(texts: Any) -> Any:
        nonlocal calls
        calls += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(embedding, "embed", embed)
    task = asyncio.create_task(build.build(request))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    await asyncio.wait_for(cancelled.wait(), 3)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 1 and "insert" not in index.events
    assert pool.releases == 1 and not pool.held
    assert (
        next(iter(factory.database.documents.values())).ingestion_status.value
        == "INDEXING"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_owner_startup_settles_and_closes_preserving_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    app = module("atlasrag.application.document_build")
    bundle, _, _, _, index = resources(tmp_path)
    events: list[str] = []
    entered, finished, release = asyncio.Event(), threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    primary = TimeoutError("startup")

    class OwnedPool(Pool):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__()

        async def open(self) -> None:
            events.append("pool.open")

        async def close(self) -> None:
            events.append("pool.close")

    async def initialize_embedding(*, allow_download: bool = False) -> None:
        events.append("embedding.initialize")

    async def initialize_index() -> None:
        if not cancel:
            raise primary

        def work() -> None:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            finished.set()

        await asyncio.to_thread(work)

    async def close_index() -> None:
        events.append("index.close")
        if cancel:
            assert finished.is_set(), (
                "closed index before initialization worker settled"
            )
        else:
            raise RuntimeError("cleanup must not replace startup failure")

    monkeypatch.setattr(
        module("atlasrag.repositories.postgres.pool"), "PostgresPool", OwnedPool
    )
    monkeypatch.setattr(
        module("atlasrag.providers.embedding.qwen"),
        "QwenEmbeddingProvider",
        lambda **kwargs: bundle.embedding,
    )
    monkeypatch.setattr(
        bundle.embedding, "initialize", initialize_embedding, raising=False
    )
    monkeypatch.setattr(index, "initialize", initialize_index)
    monkeypatch.setattr(index, "close", close_index, raising=False)
    owner = app.DocumentBuildOwner(
        config=bundle.config, postgres_dsn="injected", index=index
    )
    if not cancel:
        with pytest.raises(TimeoutError) as caught:
            await owner.open()
        assert caught.value is primary
    else:
        task = asyncio.create_task(owner.open())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            # Await a loop barrier after cancellation was delivered to startup.
            delivered = asyncio.Event()
            loop.call_soon(delivered.set)
            await delivered.wait()
            assert "index.close" not in events
            assert not task.done(), "startup cancellation abandoned the issued worker"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert events[-2:] == ["index.close", "pool.close"]
    await owner.close()
    assert events.count("index.close") == events.count("pool.close") == 1


@pytest.mark.asyncio
async def test_expected_revision_mismatch_rejects_before_candidate_creation(
    tmp_path: Path,
) -> None:
    build, request, pool, factory, _, _ = runner(tmp_path)
    expected = "rev_" + "a" * 64
    mismatched = request.model_copy(update={"expected_revision_id": expected})
    with pytest.raises(SourceMutationError):
        await build.build(mismatched)
    assert factory.database.documents == {}
    assert not pool.held and pool.releases == 1
