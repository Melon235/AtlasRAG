# Stage 8 — Part II: RagCore / Cache

## Status and base

Implementation complete. Base: Stage 7 merge
`cc5f0d454de52e63675e1bc5373bae61996806b8`.

## Frozen boundaries reused

`UserTurnRequest`, `LocalRetrievalRequest`, `LocalEvidenceResult`, `WebEvidenceResult`,
`AnswerInput`, `AnswerLocalEvidence`, `AnswerWebEvidence`, `FinalResponse`,
`RagCoreResult`, `RagCoreState`, `RetrievalGraphState`, request hash,
runtime fingerprint, query-cache key and Stage 2 Redis primitives are unchanged.

## Query Construction

One `LocalRetrievalRequest` per request. `original_query` is always the raw
query; only a structured `retrieval_query` may be rewritten. DeepSeek provider
uses the Stage 7 transport/model/key boundary. Explicit `QueryFilters` are copied
unchanged; no model-inferred filters. Protected identifiers (error codes, files,
versions, API/class names) are extracted independently and must survive each
rewrite. Maximum three rewrite attempts; recoverable malformed/term-loss
failures fall back to `retrieval_query = original_query`, preserving filters.
Injected aware clock/timezone; no transcript/history is supplied.

## Corrective routing

Pure deterministic service over base/current request, final local result and
attempt index. Default max_retries = 1 (one corrective attempt). SUFFICIENT →
ANSWER (including DEGRADED+SUFFICIENT); UNAVAILABLE → WEB; AMBIGUOUS with budget
→ original query and optional file_name removal; INSUFFICIENT + file_name with
budget → baseline retrieval query and file_name removal; otherwise WEB. Hard
filters are preserved; a meaningfully identical request never retries; only
actual invocations increment `local_attempt_index`. Each attempt uses a fresh
Local Evidence runner invocation/State.

## Evidence → Answer / citations

Terminal local status drives the frozen AnswerInput:
SUFFICIENT → all selected local evidence; INSUFFICIENT → at most one best
available match; AMBIGUOUS/UNAVAILABLE → no local evidence. Web evidence is
included only when acquired. Citation IDs L1../W1.. are assigned after final
selection, preserving order; no child fields/scores enter AnswerInput.
No-evidence outcome is a controlled SAFE_FAILURE without fabricated citations.

## RagCore graph

`construct_query → local_evidence → route_local_evidence →`
`ANSWER → build_answer_input → answer → finalize_result`
`RETRY_LOCAL → prepare_retry → local_evidence`
`WEB → web_evidence → build_answer_input`.
Dependencies are injected; State contains only frozen execution fields.
Answer subgraph regeneration is independent and bounded.

Completion matrix: answer SAFE_FAILURE or no evidence → SAFE_FAILURE;
successful answer with local/web capability degradation → DEGRADED_SUCCESS;
otherwise SUCCESS.

## Query Cache

Lookup occurs before Query Construction in `retrieval_graph`. Frozen key is
`atlasrag:query:{session_id}:{runtime_fingerprint}:{request_hash}`.
`CachedQueryResult` stores only a validated complete `FinalResponse`.
`QueryCachePolicy` caches only local-citation SUCCESS, non-time-sensitive
requests; DEGRADED_SUCCESS, SAFE_FAILURE, Web-derived/no-citation, marker-true,
and unknown-trust results are skipped. Redis is disposable: lookup/write
dependency failures bypass/skip without failing RAG. The persisted
`query_cache_invalidation_required` marker is read before lookup and rechecked
before write; marker true bypasses both; marker read failure cannot trust cache.
No marker clearing, global invalidation, RuntimeGate, or maintenance drain is
implemented; Stage 10 owns that.

## Verification

`make verify`, Stage 8 focused/real PostgreSQL-Milvus-Redis integration,
Stage 2–7 regression suites, explicit real DeepSeek Query Construction and real
synthetic end-to-end Qwen/BGE/DeepSeek/PostgreSQL/Milvus/Redis acceptance,
architecture and diff checks. Stage 9 benchmark and Stage 10 lifecycle are not
started.

## Known limits

Cache policy/freshness trust is not a substitute for Stage 10 query/maintenance
exclusion. Short TTL does not replace the freshness marker. Cache strategy
eligibility remains subject to later benchmark/operational validation.
