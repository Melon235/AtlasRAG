"""Quality-run loop over the injected real production retrieval boundary."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from time import monotonic
from uuid import uuid4

from atlasrag.domain.observation import observation_invocation
from benchmarks.contracts.models import BenchmarkQuery
from benchmarks.observation.events import (
    ObservationEvent,
    ObservationSink,
    ObservationStage,
)
from benchmarks.runners.manifests import CaseObservation
from benchmarks.runners.quality import RetrievalCallable, run_quality_case


@dataclass(frozen=True)
class QualityPolicies:
    web_enabled: bool = False
    query_cache_enabled: bool = False
    parent_cache_enabled: bool = False

    def validate(self) -> None:
        if self.web_enabled or self.query_cache_enabled or self.parent_cache_enabled:
            raise ValueError(
                "quality runs require Web, Query Cache, and Parent Cache OFF"
            )


@dataclass(frozen=True)
class QualityCaseResult:
    query_id: str
    repetition_index: int
    invocation_id: str
    observation: CaseObservation


@dataclass(frozen=True)
class QualityRunResult:
    run_id: str
    cases: tuple[QualityCaseResult, ...]


async def run_quality(
    queries: Sequence[BenchmarkQuery],
    *,
    run_id: str,
    repetitions: int,
    policies: QualityPolicies,
    retrieve: RetrievalCallable,
    observation: ObservationSink | None = None,
) -> QualityRunResult:
    policies.validate()
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be positive")
    cases: list[QualityCaseResult] = []
    for query in queries:
        for repetition in range(repetitions):
            started = monotonic()
            if observation is not None:
                observation.record(
                    ObservationEvent(
                        ObservationStage.RETRIEVAL_REQUEST, "STARTED", 1, 0.0
                    )
                )
            invocation_id = uuid4().hex
            with observation_invocation(invocation_id):
                case = await run_quality_case(
                    query,
                    run_id=run_id,
                    repetition_index=repetition,
                    retrieve=retrieve,
                )
            if observation is not None:
                elapsed = (monotonic() - started) * 1000
                if case.final_response is None:
                    observation.record(
                        ObservationEvent(
                            ObservationStage.TECHNICAL_FAILURE, "FAILED", 1, elapsed
                        )
                    )
                else:
                    observation.record(
                        ObservationEvent(
                            ObservationStage.FINAL_RESPONSE,
                            "COMPLETED",
                            len(case.final_response.citations),
                            elapsed,
                        )
                    )
            cases.append(
                QualityCaseResult(
                    query_id=query.query_id,
                    repetition_index=repetition,
                    invocation_id=invocation_id,
                    observation=case.observation,
                )
            )
    return QualityRunResult(run_id=run_id, cases=tuple(cases))
