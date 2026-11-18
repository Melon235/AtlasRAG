"""Generic production observation values; no benchmark or gold data."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from warnings import warn

_INVOCATION: ContextVar[str | None] = ContextVar(
    "atlasrag_observation_invocation", default=None
)
_ATTEMPT: ContextVar[int] = ContextVar("atlasrag_observation_attempt", default=1)


def current_invocation_id() -> str | None:
    return _INVOCATION.get()


def current_attempt_index() -> int:
    return _ATTEMPT.get()


@contextmanager
def observation_invocation(invocation_id: str) -> Iterator[None]:
    if not invocation_id.strip():
        raise ValueError("invocation_id must be nonempty")
    token: Token[str | None] = _INVOCATION.set(invocation_id)
    try:
        yield
    finally:
        _INVOCATION.reset(token)


@contextmanager
def observation_attempt(attempt_index: int) -> Iterator[None]:
    if type(attempt_index) is not int or attempt_index < 1:
        raise ValueError("attempt_index must be positive")
    token: Token[int] = _ATTEMPT.set(attempt_index)
    try:
        yield
    finally:
        _ATTEMPT.reset(token)


class ObservationStage(StrEnum):
    DENSE = "DENSE"
    BM25 = "BM25"
    FUSION = "FUSION"
    RERANKER = "RERANKER"
    EVIDENCE = "EVIDENCE"
    SUFFICIENCY = "SUFFICIENCY"
    ROUTING = "ROUTING"
    ANSWER = "ANSWER"


@dataclass(frozen=True)
class ObservedCandidate:
    identity: str
    rank: int
    score: float | None = None
    document_id: str | None = None


@dataclass(frozen=True)
class ProductionObservation:
    stage: ObservationStage
    status: str
    candidates: tuple[ObservedCandidate, ...] = ()
    selected_identities: tuple[str, ...] = ()
    latency_ms: float | None = None
    degraded: bool = False
    invocation_id: str | None = None
    attempt_index: int = 1


class ProductionObserver(Protocol):
    def observe(self, event: ProductionObservation) -> None: ...


@dataclass(frozen=True)
class NullProductionObserver:
    def observe(self, event: ProductionObservation) -> None:
        return None


NULL_OBSERVER = NullProductionObserver()


def deliver_observation(
    observer: ProductionObserver, event: ProductionObservation
) -> None:
    """Deliver an observation without changing the business result."""
    try:
        observer.observe(event)
    except Exception:
        warn(
            "production observation delivery failed",
            RuntimeWarning,
            stacklevel=2,
        )
