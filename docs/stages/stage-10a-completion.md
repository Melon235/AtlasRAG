# Stage 10-A Implementation Completion

Status: **A-R1 COMPLETE. Stage 10-A remains PARTIAL because A-R2..A-R5 offline
matrices are incomplete. Stage 10 overall remains PARTIAL.**

## 10-A1 Production business composition

Public entry point:

```python
composition = ApplicationComposition(validated_runtime_config)
runtime = await composition.start()  # default production builder
```

`ApplicationComposition.start` still accepts an optional `BusinessBuilder` for
deterministic tests; the production default is `build_default_business` in
`src/atlasrag/runtime/production.py`.

Assembled services:

**Part I:** `ParserRouter` with `GuardedMinerUParser(MinerUParser())`,
`DoclingParser`, `MarkdownParser`, `TextParser`; `BuildResources`;
`DocumentBuildRunner`; `DocumentRecoveryService`/`DocumentRecoveryRunner`;
`BuildExecutionService`; `CurrentIndexRepairService`;
`StartupReconciliationService`; `GlobalKnowledgeVerificationService`;
`StartupSourceSafetyService`; `SyncServices`; `IndexSyncApplication`.

**Part II:** `QueryConstructionService` with DeepSeek rewrite; `DenseRetriever`;
`BM25Retriever`; `HybridRetrievalService`; `RerankerService`; Parent Cache;
`UniqueEvidenceSelector`; deterministic sufficiency; `CorrectiveRoutingService`;
SearXNG/Web preparation/strategy; DeepSeek generation; `IdentityCompression`;
`OutputReviewService`; `CitationResolver`; `AnswerRunner`; `RagCoreRunner`;
`SessionQueryCache`; `RetrievalRunner`.

Shared identity: the same `PostgresPool`, `MilvusIndexProvider`, guarded Qwen,
shared `ResourceGuard`, Redis provider and HTTP clients are used for Part I and
Part II. `DocumentBuildOwner` is not constructed.

## 10-A2 MinerU guard

`GuardedMinerUParser` in `runtime/resources.py` acquires the shared
`ResourceGuard` around `MinerUParser.parse` on the invoking thread. No
`asyncio.to_thread` was introduced. Production composition injects the guarded
parser into `ParserRouter`. Fake-parser tests prove guard acquisition/release
and exclusion only; real MinerU subprocess/CUDA settlement remains deferred.

## 10-A3 F-01 through F-04

- F-01: coordinator regression `test_coordinator_does_not_reacquire_preowned_lock`.
- F-02: `test_maintenance_freshness_failure_blocks_runtime`.
- F-03: `test_cache_commit_classifies_redis_failure_without_raising`.
- F-04: `test_valid_text_parent_cache_hit_reconstructs_authoritative_provenance`.

## 10-A4 concurrency/lifecycle

Added guard/maintenance tests; existing gate/drain/recovery/task tests retained.
Shutdown unsettled-worker and admission-to-registration race tests remain
future Stage 10-B/C evidence.

## 10-A5 startup/cache

Added startup source tests and Parent TEXT_PARENT hit test. Full interrupted
deletion, Redis ambiguity, Parent miss/TABLE and freshness failure matrices
remain required deterministic work.

## 10-A6 verification

See Stage 10 acceptance report for actual counts and workflow IDs.

## Stage 10-B handoff

Required next:

- real isolated PostgreSQL/Milvus/Redis fixtures, separate DB/collection/Redis
  namespace, fake model/LLM/Web providers;
- startup empty KB, controlled synthetic maintenance, query concurrency, Query
  Cache hit/isolation, Parent Cache, freshness marker/invalidation, Redis
  outage/recovery, interrupted deletion and shutdown cleanup;
- Makefile: replace the offline `test-stage10-integration` alias with a real
  isolated suite command;
- CI: make the Stage 10 integration job provision/use isolated infrastructure
  and `uv sync --frozen`; do not represent the current offline job as real
  integration.
