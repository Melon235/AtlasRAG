"""Benchmark-owned composition of the real production Quality Run graph."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from atlasrag.application.answer import AnswerRunner
from atlasrag.application.local_evidence import LocalEvidenceRunner
from atlasrag.application.rag_core import RagCoreRunner
from atlasrag.application.retrieval import RetrievalRunner
from atlasrag.domain.enums import WebExecutionStatus
from atlasrag.domain.observation import ProductionObservation, ProductionObserver
from atlasrag.domain.web import WebEvidenceResult
from atlasrag.graphs.rag_core.subgraph import RagCoreServices
from atlasrag.graphs.retrieval.graph import RetrievalGraphServices
from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.services.cache.policy import QueryCachePolicy
from atlasrag.services.cache.session import SessionQueryCache
from atlasrag.services.rag_core.boundary import AnswerInputBuilder
from atlasrag.services.rag_core.completion import CompletionAssessor
from atlasrag.services.rag_core.query import QueryConstructionService
from atlasrag.services.rag_core.routing import CorrectiveRoutingService


class CollectingObserver:
    def __init__(self) -> None:
        self.events: list[ProductionObservation] = []

    def observe(self, event: ProductionObservation) -> None:
        self.events.append(event)


class QualityWiringError(ValueError):
    pass


class DisabledWebBoundary:
    """Quality Web OFF boundary: no provider/network calls are possible."""

    def __init__(self) -> None:
        self.calls = 0
        self.external_calls = 0

    async def run(self, request: object) -> WebEvidenceResult:
        self.calls += 1
        return WebEvidenceResult(execution_status=WebExecutionStatus.EMPTY, evidence=())


def build_quality_retrieval(
    *,
    query_construction: QueryConstructionService,
    local_evidence: LocalEvidenceRunner,
    routing: CorrectiveRoutingService,
    answer_input: AnswerInputBuilder,
    answer: AnswerRunner,
    completion: CompletionAssessor,
    observer: ProductionObserver,
    redis: RedisCacheProvider,
    uow_factory: Callable[[], object],
    runtime_fingerprint: str,
    ttl_seconds: int,
    clock: Callable[[], datetime],
    parent_cache_bypassed: bool,
) -> tuple[RetrievalRunner, DisabledWebBoundary, SessionQueryCache]:
    """Compose only real production services; fail closed on policy mismatch."""
    if not parent_cache_bypassed:
        raise QualityWiringError("Parent Cache must be absent or bypassed")
    web = DisabledWebBoundary()
    core = RagCoreRunner(
        RagCoreServices(
            query_construction=query_construction,
            local_evidence=local_evidence,
            routing=routing,
            web_evidence=web,  # type: ignore[arg-type]
            answer_input=answer_input,
            answer=answer,
            completion=completion,
            observer=observer,
        )
    )
    cache = SessionQueryCache(
        redis=redis,
        uow_factory=uow_factory,  # type: ignore[arg-type]
        runtime_fingerprint=runtime_fingerprint,
        ttl_seconds=ttl_seconds,
        policy=QueryCachePolicy(enabled=False),
        clock=clock,
    )
    return (
        RetrievalRunner(RetrievalGraphServices(cache=cache, rag_core=core)),
        web,
        cache,
    )


@dataclass(frozen=True)
class QualityRuntime:
    retrieval: RetrievalRunner
    observer: CollectingObserver
    queries: tuple[object, ...]
    gold: dict[str, dict[str, int]]
    pipeline_fingerprint: str
    runtime_fingerprint: str
    corpus_ready: bool
    web: DisabledWebBoundary
    cache_gets: object = field(default=lambda: 0)
    cache_sets: object = field(default=lambda: 0)
