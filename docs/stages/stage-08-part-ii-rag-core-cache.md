# Stage 8 — Part II: RagCore / Cache

## 1. Status

**COMPLETE — local, real-storage, and real-model end-to-end acceptance pass;
independent review findings addressed; PR open and unmerged.**

Base: `cc5f0d454de52e63675e1bc5373bae61996806b8` (Stage 7 merge).
Branch: `stage/08-part-ii-rag-core-cache`.

Stage 7 was closed after real SearXNG search, real DeepSeek generation/review/L1
citation, and real DeepSeek batch summary passed; PR #8 merged at `cc5f0d4`.

## 2. Implemented

- `QueryConstructionService` with DeepSeek structured rewrite, injected clock,
  timezone conversion, protected-term extraction/validation, maximum three
  rewrite attempts, and original-query fallback with explicit filters preserved.
  Only `DependencyError`, `ProviderResponseError`, and Pydantic
  `ValidationError` are treated as recoverable rewrite failures; unexpected
  programming `ValueError` propagates.
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
- `QueryCachePolicy`: local-only SUCCESS eligibility; rejects
  DEGRADED_SUCCESS/SAFE_FAILURE/Web-derived/no-citation, cache-disabled,
  unknown-provenance, and multilingual current/relative-time requests; size bound.
- `SessionQueryCache`: frozen query-cache key (session, runtime fingerprint,
  request hash), Redis get/put, payload FinalResponse validation, TTL, Redis
  soft-failure bypass/skip for dependency and provider-response errors, persisted
  freshness marker trust check before lookup and recheck before write; marker true
  bypasses both; marker read failure cannot trust cache; no marker clearing/global
  invalidation. Cache disabled performs zero Redis GET and zero Redis SET.
- `retrieval_graph`: frozen State, cache lookup before RagCore, trusted HIT skips
  RagCore, MISS runs RagCore then cache write; no cache metadata in State.
- Internal execution provenance: `RagCoreRunner.run_with_facts()` invokes a
  separately compiled internal full-state graph; public `run()` still uses the
  frozen `RagCoreOutput` schema and returns only `RagCoreResult`. Provenance is
  per invocation; direct graph invocation without provenance skips cache writes.
- Stage 8 commands, real PostgreSQL/Milvus/Redis integration, CI job.

## 3. Provenance review (B1/I1)

The original `expose_state=True` parameter was not applied to `StateGraph`, so
the public `RagCoreOutput` schema filtered `web_result`; `run_with_facts()` could
therefore miss Web invocation. The graph now uses `output_schema=None` only for
the internal facts compilation; the public compiled graph remains
`RagCoreOutput` and returns only `result`. No frozen State/Output/Result fields
were added, and public output is not the internal State.

Actual compiled-graph regressions cover:

```text
Local INSUFFICIENT + best_available_evidence
→ Web invoked + usable evidence
→ Answer SUCCESS citing only L1
→ run_with_facts().web_invoked == True
→ retrieval_graph cache policy rejects WEB_INVOKED
→ zero cache writes
```

```text
Local SUFFICIENT, no Web → web_invoked == False → eligible local result cached
```

```text
Web invoked but EMPTY / UNAVAILABLE → provenance remains True → no cache write
```

```text
direct retrieval graph without provenance → no cache write
concurrent independent local/Web invocations → correct per-request provenance
```

`SessionQueryCache.store()` now requires authoritative boolean provenance;
`web_invoked=None` writes nothing. The direct policy API also rejects unknown
provenance. No shared mutable facts dict is held on a runner; each
`RetrievalRunner.run()` creates a fresh per-invocation facts object.

## 4. Verification

- `make verify`: PASS — **3219 passed, 106 deselected**.
- `make test-stage8`: PASS — **26 focused tests**.
- `make stage8-verify`: PASS — full verify, focused tests, and real
  PostgreSQL/Milvus/Redis integration.
- `make test-stage8-integration`: PASS — real PostgreSQL/Milvus/Redis,
  Stage 3–5 ingestion/publication, local evidence/answer/cache HIT and
  freshness-marker bypass.
- `make stage8-provider-smoke`: PASS — real DeepSeek Query Construction and real
  synthetic local-model end-to-end Qwen/BGE/DeepSeek/PostgreSQL/Milvus/Redis
  flow; **2 tests passed**.
- `make stage7-provider-smoke`: PASS earlier (real SearXNG, DeepSeek
  generation/review/citations and batch summary); retained separately.
- `make architecture-check`, `git diff --check`, mypy, Ruff: PASS.
- Stage 2–7 regression suites: PASS on the final implementation HEAD
  `5602963ba1c6d4caf5934a9c6f5ee0b4e127bc91` (push CI `43597242251`, push
  Integration `43597242258`; PR CI `43597245730`, PR Integration `43597245744`).
- Final documentation HEAD checks are recorded in the final delivery/PR.

## 5. Real E2E precision (I5)

Synthetic corpus only:

- real Qwen3-Embedding-0.6B ingestion/query embedding;
- real Milvus retrieval;
- real BGE reranker;
- real DeepSeek Query Construction and Answer generation;
- deterministic Output Review and authoritative `L1` citation;
- real PostgreSQL and Redis.

The strengthened live test:
- computes the cache key with `runtime_fingerprint(runtime)` and the model
  identities actually injected (Qwen, BGE, DeepSeek);
- verifies the exact Redis key exists after the first eligible response;
- verifies the second request is a trusted HIT with no extra Query Construction
  or Generation calls;
- verifies a different answer-affecting runtime fingerprint cannot share the
  entry.
- Uses scoped deletion of `atlasrag:query:*` keys in the isolated test Redis
  database, not `FLUSHDB`; fixture-only Web remains explicitly labeled and is
  **not** live Web-provider acceptance.

## 6. Time-sensitive policy and timezone (I3/I4)

- The injected aware clock is converted with `astimezone(configured_timezone)`
  before provider time context; UTC/JST day-boundary tests cover the conversion.
- English current/relative terms and common Chinese expressions are bypassed at
  both lookup and write (`今天/昨天/明天/当前/目前/现在/最近/最新/实时/本月/今年/去年/
  上个月/这个月/上季度/本季度/上周/下周/本周/这周/近期/近来/近一周/近一个月/三天前/
  前两天`), plus 4-digit years including `2024年`.
- This is conservative heuristic coverage, not complete natural-language
  time-sensitivity detection; benchmark tuning remains Stage 9 work. The frozen
  request hash and cache-key projection were not changed.

## 7. Safety/limits

- No frozen DTO, State, public-output, migration, or cache-key changes.
- No Stage 9 benchmark, RuntimeGate/MaintenanceCoordinator, Query Cache global
  invalidation, marker clearing, CLI/UI, or Stage 10 work.
- Cache policy is intentionally conservative; formal strategy benchmarking and
  cache-race/query-maintenance exclusion remain Stage 9/10 work.
- Freshness trust checks are not a substitute for Stage 10 query/maintenance
  exclusion; integration runs on a non-concurrent verified snapshot.

## 8. Deferred/known limits

Full production serving lifecycle, cache-race isolation, true global cache
invalidation, formal cache/sufficiency strategy benchmarks, and complete
multilingual time-sensitivity detection remain later work. The local E2E's Web
runner is a fake fixture; Stage 7 live Web evidence is separate.
