"""Generic production observation values; no benchmark or gold data."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


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


class ProductionObserver(Protocol):
    def observe(self, event: ProductionObservation) -> None: ...


@dataclass(frozen=True)
class NullProductionObserver:
    def observe(self, event: ProductionObservation) -> None:
        return None


NULL_OBSERVER = NullProductionObserver()
