"""Cancellation-aware bounded GPU execution guard and guarded providers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol

from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.source import CandidateBuildContext, SourceFile
from atlasrag.providers.parsing.base import DocumentParser


class EmbeddingLike(Protocol):
    @property
    def identity(self) -> Any: ...
    async def initialize(self, *, allow_download: bool = False) -> None: ...
    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...
    async def embed_query(self, query: str) -> tuple[float, ...]: ...


class RerankerLike(Protocol):
    @property
    def identity(self) -> Any: ...
    async def initialize(self, *, allow_download: bool = False) -> None: ...
    async def score(self, query: str, passages: Sequence[str]) -> tuple[float, ...]: ...


class ResourceGuard:
    """One shared non-reentrant GPU slot guard."""

    def __init__(self, *, capacity: int) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self._capacity = capacity
        self._semaphore = asyncio.Semaphore(capacity)
        self._closed = False
        self._condition = asyncio.Condition()

    @property
    def capacity(self) -> int:
        return self._capacity

    async def begin_shutdown(self) -> None:
        async with self._condition:
            self._closed = True

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self._closed:
            from atlasrag.domain.errors import RuntimeShuttingDownError

            raise RuntimeShuttingDownError
        await self._semaphore.acquire()
        try:
            if self._closed:
                from atlasrag.domain.errors import RuntimeShuttingDownError

                raise RuntimeShuttingDownError
            yield
        finally:
            self._semaphore.release()


class GuardedEmbeddingProvider:
    def __init__(self, provider: EmbeddingLike, guard: ResourceGuard) -> None:
        self._provider = provider
        self._guard = guard

    @property
    def identity(self) -> Any:
        return self._provider.identity

    async def initialize(self, *, allow_download: bool = False) -> None:
        async with self._guard.slot():
            await self._provider.initialize(allow_download=allow_download)

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        async with self._guard.slot():
            return await self._provider.embed(texts)

    async def embed_query(self, query: str) -> tuple[float, ...]:
        async with self._guard.slot():
            return await self._provider.embed_query(query)


class GuardedRerankerProvider:
    def __init__(self, provider: RerankerLike, guard: ResourceGuard) -> None:
        self._provider = provider
        self._guard = guard

    @property
    def identity(self) -> Any:
        return self._provider.identity

    async def initialize(self, *, allow_download: bool = False) -> None:
        async with self._guard.slot():
            await self._provider.initialize(allow_download=allow_download)

    async def score(self, query: str, passages: Sequence[str]) -> tuple[float, ...]:
        async with self._guard.slot():
            return await self._provider.score(query, passages)


class GuardedMinerUParser:
    """Run the existing MinerU adapter on its invoking thread under the guard."""

    def __init__(self, parser: DocumentParser, guard: ResourceGuard) -> None:
        self._parser = parser
        self._guard = guard

    async def parse(
        self, source: SourceFile, context: CandidateBuildContext
    ) -> DocumentContainer:
        async with self._guard.slot():
            return await self._parser.parse(source, context)
