"""Benchmark-owned observation events; no benchmark identifiers enter production."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class ObservationStage(StrEnum):
    RETRIEVAL_REQUEST = "RETRIEVAL_REQUEST"
    FINAL_RESPONSE = "FINAL_RESPONSE"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"


@dataclass(frozen=True)
class ObservationEvent:
    stage: ObservationStage
    status: str
    count: int
    elapsed_ms: float


class ObservationSink(Protocol):
    def record(self, event: ObservationEvent) -> None: ...


class JsonlObservationSink:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: ObservationEvent) -> None:
        import json

        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "stage": event.stage.value,
                        "status": event.status,
                        "count": event.count,
                        "elapsed_ms": event.elapsed_ms,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
