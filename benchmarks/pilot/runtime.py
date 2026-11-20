"""Real Pilot runtime adapter composed from production services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from atlasrag.application.answer import AnswerRunner
from atlasrag.application.local_evidence import LocalEvidenceRunner
from atlasrag.config.models import RuntimeConfig
from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.services.local_evidence.service import LocalEvidenceServices
from atlasrag.services.rag_core.boundary import AnswerInputBuilder
from atlasrag.services.rag_core.completion import CompletionAssessor
from atlasrag.services.rag_core.query import QueryConstructionService
from atlasrag.services.rag_core.routing import CorrectiveRoutingService
from benchmarks.pilot.identity import PilotInputs
from benchmarks.wiring.quality import (
    CollectingObserver,
    QualityRuntime,
    build_quality_retrieval,
)


@dataclass(frozen=True)
class PilotRuntimeComponents:
    query_construction: QueryConstructionService
    local_services: LocalEvidenceServices
    routing: CorrectiveRoutingService
    answer_input: AnswerInputBuilder
    answer: AnswerRunner
    completion: CompletionAssessor
    redis: RedisCacheProvider
    uow_factory: Callable[[], object]
    runtime: RuntimeConfig
    runtime_fingerprint: str
    pipeline_fingerprint: str
    parent_cache_bypassed: bool
    ttl_seconds: int = 0


def build_pilot_quality_runtime(
    inputs: PilotInputs, components: PilotRuntimeComponents
) -> QualityRuntime:
    if not components.parent_cache_bypassed:
        raise ValueError("Parent Cache must be bypassed")
    observer = CollectingObserver()
    local = LocalEvidenceRunner(replace(components.local_services, observer=observer))
    retrieval, web, cache = build_quality_retrieval(
        query_construction=components.query_construction,
        local_evidence=local,
        routing=components.routing,
        answer_input=components.answer_input,
        answer=components.answer,
        completion=components.completion,
        observer=observer,
        redis=components.redis,
        uow_factory=components.uow_factory,
        runtime_fingerprint=components.runtime_fingerprint,
        ttl_seconds=components.ttl_seconds,
        clock=lambda: datetime.now(UTC),
        parent_cache_bypassed=True,
    )
    return QualityRuntime(
        retrieval=retrieval,
        observer=observer,
        queries=inputs.queries,
        gold=inputs.gold_canonical,
        pipeline_fingerprint=components.pipeline_fingerprint,
        runtime_fingerprint=components.runtime_fingerprint,
        corpus_ready=True,
        web=web,
        cache_gets=lambda: 0,
        cache_sets=lambda: 0,
    )
