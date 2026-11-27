"""Shared idempotent asyncio cancellation and task-settlement helpers."""

from __future__ import annotations

import asyncio
from typing import Any


def request_cancel_once(task: asyncio.Task[Any]) -> bool:
    """Cancel task iff it is active and not already in a cancelling state."""

    if task.done():
        return False
    if task.cancelling() == 0:
        task.cancel()
    return True


async def settle_task_no_raise(
    task: asyncio.Task[Any] | None,
    *,
    cancel: bool,
    already_cancelled: bool = False,
) -> tuple[bool, bool]:
    """Await actual task settlement via asyncio.wait without duplicate cancellation."""

    if task is None:
        return False, already_cancelled
    outer_cancelled = False
    cancel_sent = already_cancelled or task.cancelling() > 0
    if cancel and not cancel_sent and not task.done():
        request_cancel_once(task)
        cancel_sent = True
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError:
            outer_cancelled = True
            if not cancel_sent and not task.done():
                request_cancel_once(task)
                cancel_sent = True
    if not task.cancelled():
        try:
            task.exception()
        except BaseException:
            pass
    return outer_cancelled, cancel_sent
