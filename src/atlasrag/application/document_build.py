"""Whole-build lease ownership and explicit process-level build composition."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Generic, Protocol, TypeVar

from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import ConfigurationError, InvariantViolationError
from atlasrag.domain.ingestion.source import derive_document_id
from atlasrag.graphs.indexing.contracts import BuildRequest, BuildResult
from atlasrag.graphs.indexing.document_build import build_document_build_graph
from atlasrag.repositories.postgres.errors import postgres_error_boundary
from atlasrag.repositories.postgres.records import RuntimeMetadata
from atlasrag.services.indexing.build import (
    BuildResources,
    DocumentBuildServices,
    finish_task,
    settled_operation,
)
from atlasrag.services.indexing.ports import CandidateIndex
from atlasrag.services.ingestion.identity import build_candidate_context


class RollbackSession(Protocol):
    async def rollback(self) -> None: ...


_Session = TypeVar("_Session", bound=RollbackSession)


class LeasePool(Protocol[_Session]):
    async def getconn(self) -> _Session: ...
    async def putconn(self, connection: _Session) -> None: ...


class BuildLease(Protocol):
    async def release(self) -> None: ...


class BuildLockManager(Protocol):
    async def try_acquire(self, document_id: str) -> BuildLease | None: ...


class LeaseMetadata(Protocol):
    def get(self) -> Awaitable[RuntimeMetadata]: ...


class OwnedCandidateIndex(CandidateIndex, Protocol):
    async def close(self) -> None: ...


class _LeasePoolCapture(Generic[_Session]):
    """Capture only this invocation's actual lease session via public getconn.

    Probe intentionally couples lease liveness to the required runtime_metadata
    singleton, using its existing repository and rolling back the read promptly.
    A successful checkpoint is not fencing and cannot eliminate subsequent loss.
    The original lease remains the sole release owner.
    """

    def __init__(
        self, pool: LeasePool[_Session], repository: Callable[[_Session], LeaseMetadata]
    ) -> None:
        self.pool = pool
        self.repository = repository
        self.connection: _Session | None = None
        self.lost: BaseException | None = None
        self.mutex = asyncio.Lock()

    async def getconn(self) -> _Session:
        if self.connection is not None:
            raise InvariantViolationError
        connection = await self.pool.getconn()
        self.connection = connection
        return connection

    async def putconn(self, connection: _Session) -> None:
        if connection is not self.connection:
            raise InvariantViolationError
        try:
            await self.pool.putconn(connection)
        finally:
            self.connection = None

    async def _probe(self) -> None:
        async with self.mutex:
            if self.lost is not None:
                raise self.lost
            connection = self.connection
            if connection is None:
                raise InvariantViolationError
            primary: BaseException | None = None
            try:
                with postgres_error_boundary():
                    RuntimeMetadata.model_validate(
                        await self.repository(connection).get()
                    )
            except BaseException as error:
                primary = error
                raise
            finally:
                try:
                    with postgres_error_boundary():
                        await connection.rollback()
                except BaseException as error:
                    if primary is None:
                        raise
                    primary.add_note(f"lease probe rollback: {type(error).__name__}")

    async def probe(self) -> None:
        try:
            await settled_operation(self._probe())
        except BaseException as error:
            self.lost = error
            raise

    async def release(self, lease: BuildLease) -> None:
        async with self.mutex:
            await lease.release()


class DocumentBuildRunner(Generic[_Session]):
    """Inject shared resources; create a fresh graph/attempt/session per build.

    For an externally owned pool, pool_capacity is the caller's capacity
    declaration, not introspection. Reserve one lease and one UoW connection per
    concurrent build; callers must also account for other users of that pool.
    """

    def __init__(
        self,
        *,
        resources: BuildResources,
        pool: LeasePool[_Session],
        lock_manager_factory: Callable[[LeasePool[_Session]], BuildLockManager],
        metadata_repository_factory: Callable[[_Session], LeaseMetadata],
        max_concurrent_builds: int,
        pool_capacity: int,
        contention_poll_seconds: float = 0.05,
    ) -> None:
        if (
            type(max_concurrent_builds) is not int
            or max_concurrent_builds < 1
            or type(pool_capacity) is not int
            or pool_capacity < 2 * max_concurrent_builds
            or isinstance(contention_poll_seconds, bool)
            or not isinstance(contention_poll_seconds, (float, int))
            or not math.isfinite(contention_poll_seconds)
            or contention_poll_seconds <= 0
        ):
            raise ConfigurationError
        self.resources = resources
        self.pool = pool
        self._manager = lock_manager_factory
        self._metadata = metadata_repository_factory
        self._slots = asyncio.Semaphore(max_concurrent_builds)
        self._poll_seconds = contention_poll_seconds

    async def build(self, request: BuildRequest) -> BuildResult:
        request = BuildRequest.model_validate(request)
        if request.expected_pipeline_fingerprint != pipeline_fingerprint(
            self.resources.config
        ):
            raise ConfigurationError
        document_id = derive_document_id(request.source.relative_source_path)
        async with self._slots:
            return await self._locked_build(request, document_id)

    async def _locked_build(
        self, request: BuildRequest, document_id: str
    ) -> BuildResult:
        capture = _LeasePoolCapture(self.pool, self._metadata)
        manager = self._manager(capture)
        lease: BuildLease | None = None
        services: DocumentBuildServices | None = None
        primary: BaseException | None = None
        try:
            while lease is None:
                acquisition = asyncio.create_task(manager.try_acquire(document_id))
                try:
                    lease = await asyncio.shield(acquisition)
                except asyncio.CancelledError as cancellation:
                    # Acquisition may already own a session lock: settle and
                    # recover its returned lease before executing finally.
                    try:
                        lease = await finish_task(acquisition)
                    except BaseException as error:
                        cancellation.add_note(
                            f"lease acquisition settlement: {type(error).__name__}"
                        )
                    raise cancellation
                if lease is None:
                    await asyncio.sleep(self._poll_seconds)
            await capture.probe()
            context = await settled_operation(
                asyncio.to_thread(
                    build_candidate_context,
                    request.source,
                    request.expected_pipeline_fingerprint,
                )
            )
            services = DocumentBuildServices(
                self.resources, context, checkpoint=capture.probe
            )
            graph = build_document_build_graph(services)
            invocation = asyncio.create_task(graph.ainvoke({"context": context}))
            try:
                output = await asyncio.shield(invocation)
            except asyncio.CancelledError as cancellation:
                # Send exactly one cancellation into LangGraph. Repeated caller
                # cancels cannot interrupt its node/worker cleanup and unlock early.
                invocation.cancel()
                try:
                    await finish_task(invocation)
                except BaseException as error:
                    if not isinstance(error, asyncio.CancelledError):
                        cancellation.add_note(
                            f"graph settlement: {type(error).__name__}"
                        )
                raise cancellation
            return BuildResult.model_validate(output["result"])
        except BaseException as error:
            primary = error
            raise
        finally:
            if services is not None:
                services.invalidate()
            if lease is not None:
                try:
                    await settled_operation(capture.release(lease))
                except BaseException as error:
                    if primary is None:
                        raise
                    primary.add_note(f"build lease release: {type(error).__name__}")


class DocumentBuildOwner:
    """Explicit process owner for shared Qwen/parsers/index and a dedicated pool.

    Construction loads no model assets and opens no connections. Call open once
    at process startup, await all builds, then close. The supplied index's
    lifecycle is transferred to this owner. Qwen's frozen adapter has no unload
    API: its single model lives for this owner's process lifetime. This is a
    document-build composition root, not a maintenance/serving coordinator.
    """

    def __init__(
        self, *, config: RuntimeConfig, postgres_dsn: str, index: OwnedCandidateIndex
    ) -> None:
        from atlasrag.providers.embedding.qwen import QwenEmbeddingProvider
        from atlasrag.providers.parsing.docling import DoclingParser
        from atlasrag.providers.parsing.markdown import MarkdownParser
        from atlasrag.providers.parsing.mineru import MinerUParser
        from atlasrag.providers.parsing.routing import ParserRouter
        from atlasrag.providers.parsing.text import TextParser
        from atlasrag.repositories.postgres.locks import PostgresAdvisoryLockManager
        from atlasrag.repositories.postgres.pool import PostgresPool
        from atlasrag.repositories.postgres.runtime_metadata import (
            RuntimeMetadataRepository,
        )
        from atlasrag.repositories.postgres.uow import PostgresUnitOfWork

        runtime = RuntimeConfig.model_validate(config)
        concurrency = runtime.maintenance.max_document_concurrency
        capacity = 2 * concurrency
        self._pool = PostgresPool(postgres_dsn, min_size=1, max_size=capacity)
        self._embedding = QwenEmbeddingProvider(device=runtime.models.model_device)
        self._index = index
        resources = BuildResources(
            config=runtime,
            uow_factory=lambda: PostgresUnitOfWork(self._pool),
            parsers=ParserRouter(
                MinerUParser(), DoclingParser(), MarkdownParser(), TextParser()
            ),
            embedding=self._embedding,
            index=index,
            clock=lambda: datetime.now(UTC),
        )
        self._runner = DocumentBuildRunner(
            resources=resources,
            pool=self._pool,
            lock_manager_factory=PostgresAdvisoryLockManager,
            metadata_repository_factory=RuntimeMetadataRepository,
            max_concurrent_builds=concurrency,
            pool_capacity=capacity,
        )
        self._lifecycle = asyncio.Lock()
        self._opened = False
        self._closed = False
        self._active_builds = 0

    async def open(self, *, allow_download: bool = False) -> None:
        async with self._lifecycle:
            if self._closed:
                raise InvariantViolationError
            if self._opened:
                return
            try:
                await self._pool.open()
                await settled_operation(
                    self._embedding.initialize(allow_download=allow_download)
                )
                await settled_operation(self._index.initialize())
            except BaseException as primary:
                self._closed = True
                await self._close_resources(primary)
                raise
            self._opened = True

    async def build(self, request: BuildRequest) -> BuildResult:
        if not self._opened or self._closed:
            raise InvariantViolationError
        self._active_builds += 1
        try:
            return await self._runner.build(request)
        finally:
            self._active_builds -= 1

    async def _close_resources(self, primary: BaseException | None = None) -> None:
        first = primary
        for close in (self._index.close, self._pool.close):
            try:
                await settled_operation(close())
            except BaseException as error:
                if first is None:
                    first = error
                else:
                    first.add_note(f"build owner cleanup: {type(error).__name__}")
        if primary is None and first is not None:
            raise first

    async def close(self) -> None:
        async with self._lifecycle:
            if self._closed:
                return
            if self._active_builds:
                raise InvariantViolationError
            self._closed = True
            await self._close_resources()
