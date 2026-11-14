# Stage 8 — Part II: RagCore / Cache

## 1. Status

**IMPLEMENTATION COMPLETE — LOCAL/REAL-STORAGE GATES PASS; FULL REAL-MODEL
END-TO-END LIVE ACCEPTANCE PENDING.**

Base: `cc5f0d454de52e63675e1bc5373bae61996806b8` (Stage 7 merge).
Branch: `stage/08-part-ii-rag-core-cache`.

Stage 7 was closed after real SearXNG search, real DeepSeek generation/review/L1
citation, and real DeepSeek batch summary passed; PR #8 merged at `cc5f0d4`.

## 2. Implemented

- `QueryConstructionService` with DeepSeek structured rewrite, injected clock,
  protected-term extraction/validation, maximum three rewrite attempts, and
  original-query fallback with explicit filters preserved.
- `DeepSeekQueryRewriteProvider` reusing the Stage 7 DeepSeek transport/model/key
  boundary; no arbitrary filters or IDs can be emitted.
- `CorrectiveRoutingService`: SUFFICIENT→ANSWER, UNAVAILABLE→WEB,
  AMBIGUOUS retry (original query, drop file_name), INSUFFICIENT+file_name retry
  (baseline retrieval query, drop file_name), meaningful-difference guard,
  hard filters preserved, bounded by router hyperparameter.
- `AnswerInputBuilder`: only terminal selected local evidence is carried;
  INSUFFICIENT carries at most one best match; AMBIGUOUS/UNAVAILABLE carry none;
  Local/Web citation IDs assigned L1.. / W1.. at the final boundary.
- `RagCoreRunner` / `rag_core_subgraph`: frozen `RagCoreState`, construct →
  local → bounded route/retry → Web or Answer boundary → Answer → completion;
  controlled no-evidence SAFE_FAILURE; no prior local State inherited.
- `CompletionAssessor`: SUCCESS / DEGRADED_SUCCESS / SAFE_FAILURE from frozen
  results and answer outcome.
- `QueryCachePolicy`: conservative local-only SUCCESS eligibility; no
  DEGRADED_SUCCESS/SAFE_FAILURE/Web-derived/time-sensitive/no-citation writes;
  size bound.
- `SessionQueryCache`: frozen query-cache key (session, runtime fingerprint,
  request hash), Redis get/put, payload FinalResponse validation, TTL, Redis
  soft-failure bypass/skip, persisted freshness marker trust check before lookup
  and recheck before write; marker true bypasses both; marker read failure
  cannot trust cache; no marker clearing/global invalidation.
- `retrieval_graph`: frozen State, cache lookup before RagCore, trusted HIT skips
  RagCore, MISS runs RagCore then cache write; no cache metadata in State.
- Stage 8 commands, real PostgreSQL/Milvus/Redis integration, CI job.

## 3. Verification

- `make verify`: PASS — 3208 passed, 105 deselected.
- `make test-stage8`: PASS — 15 focused tests.
- `make test-stage8-integration`: PASS — real PostgreSQL/Milvus/Redis,
  Stage 3–5 ingestion/publication, local evidence/answer/cache HIT and
  freshness-marker bypass.
- `make stage8-provider-smoke`: **PASS for real DeepSeek Query Construction**
  (`deepseek-chat`, synthetic Chinese identifier query; ERR-104 and
  manual-v2.3.pdf preserved).
- `make stage7-provider-smoke`: PASS earlier (real SearXNG, DeepSeek
  generation/review/citations and batch summary).
- Stage 2–7 regression gates: to be recorded in final closeout.
- Full real model/storage end-to-end (real DeepSeek rewrite + Qwen/BGE retrieval
  + real generation in one Stage 8 invocation): **PENDING**; not claimed as
  passed.

## 4. Safety/limits

- No frozen DTO or State changes.
- No Stage 9 benchmark, RuntimeGate/MaintenanceCoordinator, Query Cache global
  invalidation, marker clearing, CLI/UI, or Stage 9 work.
- Cache policy is intentionally conservative and benchmark-policy validation is
  deferred.
- Freshness trust checks are not a substitute for Stage 10 query/maintenance
  exclusion; integration runs on a non-concurrent verified snapshot.

## 5. Deferred/known limits

Full production serving lifecycle, cache-race isolation, true global cache
invalidation, and formal cache/sufficiency strategy benchmarks remain Stage 9/
10 work.
