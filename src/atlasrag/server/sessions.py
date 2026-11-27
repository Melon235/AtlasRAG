"""Bounded process-local SessionRegistry enforcing mandatory server-issued sessions."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic

from atlasrag.domain.errors import ErrorCode, InvariantViolationError
from atlasrag.providers.cache.keys import is_cache_key_component_safe
from atlasrag.server.config import ServerConfig
from atlasrag.server.errors import ServerHttpError, TransportErrorCode
from atlasrag.server.schemas import ClientKind


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SessionInfo:
    """Public metadata snapshot for a newly created process-local session."""

    session_id: str
    client_kind: ClientKind
    created_at: datetime
    expires_at: datetime


@dataclass
class _SessionEntry:
    session_id: str
    client_kind: ClientKind
    created_at: datetime
    last_activity_at: datetime
    expires_at: datetime
    last_activity_monotonic: float
    in_flight: int = 0
    closed: bool = False


@dataclass
class SessionLease:
    """Single-use active-query lease bound to one process-local session."""

    session_id: str
    client_kind: ClientKind
    released: bool = False


class SessionRegistry:
    """Bounded, in-flight-safe process-local session table.

    Retains strictly operational metadata; never stores transcripts, answers,
    evidence, or Redis cache keys, and never invokes Redis on closure.
    """

    def __init__(
        self,
        server_config: ServerConfig,
        *,
        clock: Callable[[], datetime] = _utc_now,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._max_sessions = server_config.max_sessions
        self._ttl_seconds = server_config.session_ttl_seconds
        self._max_in_flight_per_session = server_config.max_in_flight_per_session
        self._clock = clock
        self._monotonic = monotonic_clock
        self._lock = asyncio.Lock()
        self._sessions: dict[str, _SessionEntry] = {}

    def _is_expired(self, entry: _SessionEntry, now_mono: float) -> bool:
        return (now_mono - entry.last_activity_monotonic) >= self._ttl_seconds

    def _prune_idle_locked(self, now_mono: float) -> None:
        to_remove: list[str] = []
        for session_id, entry in self._sessions.items():
            if entry.in_flight > 0:
                if self._is_expired(entry, now_mono):
                    entry.closed = True
            elif entry.closed or self._is_expired(entry, now_mono):
                to_remove.append(session_id)
        for session_id in to_remove:
            self._sessions.pop(session_id, None)

    def _generate_session_id(self, client_kind: ClientKind) -> str:
        for _ in range(16):
            candidate = f"{client_kind.value}_{secrets.token_hex(16)}"
            if not is_cache_key_component_safe(candidate):
                raise InvariantViolationError
            if candidate not in self._sessions:
                return candidate
        raise InvariantViolationError

    async def create_session(self, client_kind: ClientKind) -> SessionInfo:
        """Create a new server-issued session or fail with RESOURCE_EXHAUSTED."""

        async with self._lock:
            now_mono = self._monotonic()
            now_utc = self._clock()
            self._prune_idle_locked(now_mono)
            if len(self._sessions) >= self._max_sessions:
                raise ServerHttpError(
                    ErrorCode.RESOURCE_EXHAUSTED,
                    "Maximum active session capacity reached.",
                )
            session_id = self._generate_session_id(client_kind)
            expires_at = now_utc + timedelta(seconds=self._ttl_seconds)
            self._sessions[session_id] = _SessionEntry(
                session_id=session_id,
                client_kind=client_kind,
                created_at=now_utc,
                last_activity_at=now_utc,
                expires_at=expires_at,
                last_activity_monotonic=now_mono,
            )
            return SessionInfo(
                session_id=session_id,
                client_kind=client_kind,
                created_at=now_utc,
                expires_at=expires_at,
            )

    async def acquire(
        self,
        session_id: str,
        *,
        client_kind: ClientKind | None = None,
    ) -> SessionLease:
        """Validate session existence/classification and acquire an in-flight slot."""

        if not is_cache_key_component_safe(session_id):
            raise ServerHttpError(TransportErrorCode.SESSION_NOT_FOUND)
        async with self._lock:
            now_mono = self._monotonic()
            now_utc = self._clock()
            entry = self._sessions.get(session_id)
            if entry is None or entry.closed:
                raise ServerHttpError(TransportErrorCode.SESSION_NOT_FOUND)
            if self._is_expired(entry, now_mono):
                if entry.in_flight == 0:
                    self._sessions.pop(session_id, None)
                else:
                    entry.closed = True
                raise ServerHttpError(TransportErrorCode.SESSION_NOT_FOUND)
            if client_kind is not None and entry.client_kind is not client_kind:
                raise ServerHttpError(
                    TransportErrorCode.FORBIDDEN,
                    "Request client classification does not match session kind.",
                )
            if entry.in_flight >= self._max_in_flight_per_session:
                raise ServerHttpError(
                    ErrorCode.RESOURCE_EXHAUSTED,
                    "Session concurrent query limit reached.",
                )
            entry.in_flight += 1
            entry.last_activity_monotonic = now_mono
            entry.last_activity_at = now_utc
            entry.expires_at = now_utc + timedelta(seconds=self._ttl_seconds)
            return SessionLease(session_id=session_id, client_kind=entry.client_kind)

    async def release(self, lease: SessionLease) -> None:
        """Release one acquired session lease and purge if closed/expired and idle."""

        async with self._lock:
            if lease.released:
                return
            lease.released = True
            entry = self._sessions.get(lease.session_id)
            if entry is None:
                return
            if entry.in_flight > 0:
                entry.in_flight -= 1
            if entry.in_flight == 0 and (
                entry.closed or self._is_expired(entry, self._monotonic())
            ):
                self._sessions.pop(lease.session_id, None)

    async def close_session(
        self,
        session_id: str,
        *,
        client_kind: ClientKind | None = None,
    ) -> None:
        """Close a session for new work without cancelling settled-pending queries."""

        if not is_cache_key_component_safe(session_id):
            raise ServerHttpError(TransportErrorCode.SESSION_NOT_FOUND)
        async with self._lock:
            now_mono = self._monotonic()
            entry = self._sessions.get(session_id)
            if entry is None or entry.closed:
                raise ServerHttpError(TransportErrorCode.SESSION_NOT_FOUND)
            if self._is_expired(entry, now_mono):
                if entry.in_flight == 0:
                    self._sessions.pop(session_id, None)
                else:
                    entry.closed = True
                raise ServerHttpError(TransportErrorCode.SESSION_NOT_FOUND)
            if client_kind is not None and entry.client_kind is not client_kind:
                raise ServerHttpError(
                    TransportErrorCode.FORBIDDEN,
                    "Request client classification does not match session kind.",
                )
            entry.closed = True
            if entry.in_flight == 0:
                self._sessions.pop(session_id, None)

    def active_count(self) -> int:
        """Return total tracked entries (including closed entries awaiting settlement)."""

        return len(self._sessions)

    def contains(self, session_id: str) -> bool:
        """Return whether session_id currently exists in the process-local registry."""

        return session_id in self._sessions

    def in_flight(self, session_id: str) -> int:
        """Return current active in-flight query count for session_id (0 if absent)."""

        entry = self._sessions.get(session_id)
        return 0 if entry is None else entry.in_flight
