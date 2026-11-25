"""Application-owned registry for work that can use runtime resources."""

from __future__ import annotations

import asyncio
from enum import StrEnum


class TaskKind(StrEnum):
    QUERY = "QUERY"
    MAINTENANCE = "MAINTENANCE"
    HEALTH = "HEALTH"
    STARTUP = "STARTUP"
    CACHE = "CACHE"


class InFlightTaskRegistry:
    """Track actual tasks, not only counters, for shutdown settlement."""

    def __init__(self) -> None:
        self._tasks: dict[asyncio.Task[object], TaskKind] = {}
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        async with self._lock:
            self._closed = True

    def register(self, task: asyncio.Task[object], kind: TaskKind) -> None:
        if self._closed:
            raise RuntimeError("task registry is closed")
        if task.done():
            raise RuntimeError("cannot register completed task")
        self._tasks[task] = kind

    def unregister(self, task: asyncio.Task[object]) -> None:
        self._tasks.pop(task, None)

    def snapshot(
        self, kind: TaskKind | None = None
    ) -> tuple[asyncio.Task[object], ...]:
        return tuple(
            task
            for task, task_kind in self._tasks.items()
            if kind is None or task_kind is kind
        )

    async def wait_empty(self, *, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while self._tasks:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            done, _ = await asyncio.wait(
                tuple(self._tasks),
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                self.unregister(task)
        return True

    def cancel_all(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
