"""Bounded process-local MaintenanceOperationRegistry (HTTP control ledger only)."""

from __future__ import annotations

import asyncio
import re
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from atlasrag.domain.errors import (
    AtlasRAGError,
    MaintenanceBusyError,
    RuntimeNotReadyError,
    RuntimeShuttingDownError,
)
from atlasrag.runtime.coordinator import ApplicationCoordinator
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.server.config import ServerConfig
from atlasrag.server.errors import (
    ServerHttpError,
    TransportErrorCode,
    translate_exception,
)
from atlasrag.server.schemas import (
    OperationAcceptedResponse,
    OperationKind,
    OperationStatus,
    OperationStatusResponse,
    OperationSummary,
)
from atlasrag.services.sync.models import ServingSafety, SyncOutcome, SyncResult

_SAFE_CODE_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_MAX_UNSAFE_REASONS = 32


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_code(value: str | None) -> str | None:
    if value is None or _SAFE_CODE_PATTERN.fullmatch(value) is None:
        return None
    return value


def _project_summary(result: SyncResult) -> OperationSummary:
    return OperationSummary(
        checked_documents=result.checked_documents,
        planned_builds=result.planned_builds,
        planned_deletions=result.planned_deletions,
        ready_builds=result.ready_builds,
        failed_builds=result.failed_builds,
        source_mutated_builds=result.source_mutated_builds,
        technical_build_failures=result.technical_build_failures,
        deleted_documents=result.deleted_documents,
        failed_deletions=result.failed_deletions,
        verified_documents=result.verified_documents,
        verified_records=result.verified_records,
    )


def _project_unsafe_reasons(reasons: tuple[str, ...]) -> tuple[str, ...]:
    filtered = [
        reason for reason in reasons if _SAFE_CODE_PATTERN.fullmatch(reason) is not None
    ]
    return tuple(filtered[:_MAX_UNSAFE_REASONS])


@dataclass
class _OperationEntry:
    operation_id: str
    kind: OperationKind
    status: OperationStatus
    started_at: datetime
    accepted_at: datetime | None = None
    finished_at: datetime | None = None
    sync_outcome: SyncOutcome | None = None
    serving_safety: ServingSafety | None = None
    unsafe_reasons: tuple[str, ...] = ()
    blocked_code: str | None = None
    summary: OperationSummary | None = None
    error_code: str | None = None
    message: str | None = None


class MaintenanceOperationRegistry:
    """Bounded process-local control-plane operation ledger.

    Does not duplicate `InFlightTaskRegistry`: coordinator runtime execution remains
    the sole source of `TaskKind.MAINTENANCE` resource tracking.
    """

    def __init__(
        self,
        *,
        coordinator: ApplicationCoordinator,
        server_config: ServerConfig,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._coordinator = coordinator
        self._max_retained = server_config.max_retained_operations
        self._clock = clock
        self._lock = asyncio.Lock()
        self._operations: OrderedDict[str, _OperationEntry] = OrderedDict()
        self._active_operation_id: str | None = None
        self._active_task: asyncio.Task[None] | None = None

    @property
    def active_operation_id(self) -> str | None:
        return self._active_operation_id

    @property
    def active_task(self) -> asyncio.Task[None] | None:
        return self._active_task

    def retained_count(self) -> int:
        return len(self._operations)

    def _generate_operation_id(self) -> str:
        for _ in range(16):
            candidate = f"op_{secrets.token_hex(12)}"
            if candidate not in self._operations:
                return candidate
        raise RuntimeError("failed to generate unique operation_id")

    def _prune_terminal_locked(self) -> None:
        while len(self._operations) > self._max_retained:
            evict_key: str | None = None
            for op_id, entry in self._operations.items():
                if op_id != self._active_operation_id and entry.status in {
                    OperationStatus.SUCCEEDED,
                    OperationStatus.FAILED,
                }:
                    evict_key = op_id
                    break
            if evict_key is None:
                break
            self._operations.pop(evict_key, None)

    def _validate_pre_reservation_state(self, kind: OperationKind) -> None:
        mode = self._coordinator.gate.mode
        if mode is RuntimeMode.SHUTTING_DOWN:
            raise RuntimeShuttingDownError
        if self._active_operation_id is not None:
            raise MaintenanceBusyError
        if mode in {RuntimeMode.DRAINING, RuntimeMode.MAINTENANCE}:
            raise MaintenanceBusyError
        if kind is OperationKind.SYNC and mode is not RuntimeMode.SERVING:
            raise RuntimeNotReadyError
        if kind is OperationKind.RECOVER and mode is not RuntimeMode.BLOCKED:
            raise RuntimeNotReadyError

    async def start_operation(
        self,
        kind: OperationKind,
        *,
        on_draining_hook: Callable[[], None] | None = None,
    ) -> OperationAcceptedResponse:
        """Atomically reserve an operation and return 202 metadata post-DRAINING."""

        draining_event = asyncio.Event()
        pre_admission_error: BaseException | None = None

        async with self._lock:
            self._validate_pre_reservation_state(kind)
            operation_id = self._generate_operation_id()
            started_at = self._clock()
            entry = _OperationEntry(
                operation_id=operation_id,
                kind=kind,
                status=OperationStatus.STARTING,
                started_at=started_at,
            )
            self._operations[operation_id] = entry
            self._active_operation_id = operation_id
            self._prune_terminal_locked()

            def _handle_draining() -> None:
                if on_draining_hook is not None:
                    on_draining_hook()
                entry.accepted_at = self._clock()
                entry.status = OperationStatus.RUNNING
                draining_event.set()

            async def _run_operation() -> None:
                nonlocal pre_admission_error
                try:
                    runner: Callable[..., Awaitable[SyncResult]] = (
                        self._coordinator.run_maintenance
                        if kind is OperationKind.SYNC
                        else self._coordinator.recover
                    )
                    result = await runner(on_draining=_handle_draining)
                except asyncio.CancelledError:
                    if not draining_event.is_set():
                        pre_admission_error = RuntimeShuttingDownError()
                        return
                    async with self._lock:
                        entry.status = OperationStatus.FAILED
                        entry.finished_at = self._clock()
                        entry.error_code = "RUNTIME_SHUTTING_DOWN"
                        entry.message = (
                            "Operation was cancelled during runtime shutdown."
                        )
                        if self._active_operation_id == operation_id:
                            self._active_operation_id = None
                            self._active_task = None
                        self._prune_terminal_locked()
                except BaseException as exc:
                    if not draining_event.is_set():
                        pre_admission_error = exc
                        return
                    _, translated = translate_exception(exc)
                    async with self._lock:
                        entry.status = OperationStatus.FAILED
                        entry.finished_at = self._clock()
                        entry.error_code = translated.error_code
                        entry.message = translated.message
                        if self._active_operation_id == operation_id:
                            self._active_operation_id = None
                            self._active_task = None
                        self._prune_terminal_locked()
                else:
                    async with self._lock:
                        entry.status = (
                            OperationStatus.FAILED
                            if result.outcome is SyncOutcome.FAILED
                            else OperationStatus.SUCCEEDED
                        )
                        entry.finished_at = self._clock()
                        entry.sync_outcome = result.outcome
                        entry.serving_safety = result.serving_safety
                        entry.unsafe_reasons = _project_unsafe_reasons(
                            result.unsafe_reasons
                        )
                        entry.blocked_code = _safe_code(result.blocked_code)
                        entry.summary = _project_summary(result)
                        if self._active_operation_id == operation_id:
                            self._active_operation_id = None
                            self._active_task = None
                        self._prune_terminal_locked()

            op_task: asyncio.Task[None] = asyncio.create_task(_run_operation())
            self._active_task = op_task

        draining_wait = asyncio.create_task(draining_event.wait())
        try:
            await asyncio.wait(
                {draining_wait, op_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            await self._settle_task_quietly(draining_wait, cancel=True)
            if not draining_event.is_set():
                await self._settle_task_quietly(op_task, cancel=True)
                await self._rollback_unadmitted(operation_id)
            raise
        finally:
            await self._settle_task_quietly(draining_wait, cancel=True)

        if not draining_event.is_set():
            await self._settle_task_quietly(op_task, cancel=False)
            await self._rollback_unadmitted(operation_id)
            if pre_admission_error is not None:
                if isinstance(pre_admission_error, (AtlasRAGError, ServerHttpError)):
                    raise pre_admission_error
                raise ServerHttpError(
                    TransportErrorCode.INTERNAL_ERROR,
                    "Maintenance operation could not be admitted.",
                ) from pre_admission_error
            raise RuntimeShuttingDownError

        current_mode = self._coordinator.gate.mode
        if current_mode is RuntimeMode.SHUTTING_DOWN:
            # Operation entered DRAINING and stays server-owned in self._active_task;
            # refuse false 202 acceptance once shutdown has begun.
            raise RuntimeShuttingDownError

        assert entry.accepted_at is not None
        return OperationAcceptedResponse(
            operation_id=entry.operation_id,
            kind=entry.kind,
            status=OperationStatus.RUNNING,
            accepted_at=entry.accepted_at,
        )

    async def _settle_task_quietly(
        self, task: asyncio.Task[Any], *, cancel: bool
    ) -> None:
        if cancel and not task.done():
            task.cancel()
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError:
                pass
        if not task.cancelled():
            try:
                task.exception()
            except BaseException:
                pass

    async def _rollback_unadmitted(self, operation_id: str) -> None:
        async with self._lock:
            self._operations.pop(operation_id, None)
            if self._active_operation_id == operation_id:
                self._active_operation_id = None
                self._active_task = None

    async def get_operation(self, operation_id: str) -> OperationStatusResponse:
        async with self._lock:
            entry = self._operations.get(operation_id)
            if entry is None:
                raise ServerHttpError(TransportErrorCode.OPERATION_NOT_FOUND)
            return OperationStatusResponse(
                operation_id=entry.operation_id,
                kind=entry.kind,
                status=entry.status,
                sync_outcome=entry.sync_outcome,
                serving_safety=entry.serving_safety,
                runtime_mode=self._coordinator.gate.mode,
                unsafe_reasons=entry.unsafe_reasons,
                blocked_code=entry.blocked_code,
                summary=entry.summary,
                started_at=entry.started_at,
                finished_at=entry.finished_at,
                error_code=entry.error_code,
                message=entry.message,
            )

    async def wait_active(self) -> None:
        """Await any active server-owned maintenance task until settled."""

        task = self._active_task
        if task is not None and not task.done():
            await self._settle_task_quietly(task, cancel=False)
