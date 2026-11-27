# Stage 10-A Implementation Completion

Status: **Stage 10-A (`A-R1` through `A-R6`) COMPLETE offline. (Superseding Stage 10-C4 closeout note: Stage 10-B real isolated integration, historical Stage 10-C1/C3 reviews & C2/C2-R1 remediations, Stage 10-C3-R1 final independent targeted re-review `PASS` on `be5a2876`, and Stage 10-C4 closeout are now COMPLETE — see [`stage-10b-completion.md`](stage-10b-completion.md), [`stage-10-acceptance.md`](stage-10-acceptance.md), [`stage-10-review/STAGE_10_C3_R1_REVIEW.md`](stage-10-review/STAGE_10_C3_R1_REVIEW.md), and [`stage-10-closeout/CLOSEOUT_REPORT.md`](stage-10-closeout/CLOSEOUT_REPORT.md). Stage 10 is `ACCEPTED / PENDING_MERGE`; `PR #11` remains open/unmerged; Stage 11 is `NOT_STARTED`.)**

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

## 10-A4 A-R1 concurrency/lifecycle additions

A-R1 tests cover guarded providers, maintenance freshness failure, gate
drain/recovery, task settlement, bootstrap/cleanup ordering and ownership
retention. They remain unchanged and passing.

## 10-A5 A-R2 runtime concurrency, health lifecycle and shutdown

Added `tests/stage10/runtime_harness.py` plus three focused deterministic
modules that keep the real `RuntimeGate`, `InFlightTaskRegistry`,
`HealthRegistry`, `MaintenanceCoordinator` and `ApplicationCoordinator`
boundaries and fake only technical providers and business callbacks:

- `tests/stage10/test_runtime_admission.py` — AR2-01, AR2-02, AR2-03, AR2-10.
- `tests/stage10/test_runtime_health.py` — AR2-04, AR2-05, AR2-06.
- `tests/stage10/test_runtime_lifecycle.py` — AR2-07, AR2-08, AR2-09, AR2-11.

Confirmed A-R2 defects fixed (each with a regression executed against the
reverted pre-fix path):

- **A-R2-F01** — cancellation-settled work was misreported as an unsettled
  shutdown failure because `wait_empty` checked the deadline before pruning
  settled tasks. Fix: prune settled tasks each loop iteration; release the
  Query Lease before unregistering settlement ownership.
- **A-R2-F02** — public `health()`/`readiness()` probed dependencies untracked.
  Fix: register them as `TaskKind.HEALTH` until the probe settles and map a
  closed registry to `RuntimeShuttingDownError`.
- **A-R2-F03** — stale health results could reopen admission after a lifecycle
  transition, and readiness used the snapshot's sampled mode. Fix: the gate
  carries a lifecycle generation, health snapshots record the generation at
  probe start, `set_admission_ready` ignores older epochs, the coordinator only
  applies SERVING-sampled decisions, and readiness refreshes on an epoch change
  and uses the current mode.
- **A-R2-F04** — aborting maintenance before mutation restored SERVING but left
  admission closed. Fix: restore the captured pre-maintenance admission only
  on the DRAINING→SERVING abort path.
- **A-R2-F05** — concurrent shutdown calls could double-close resources. Fix:
  single-flight `asyncio.Lock` with double-checked completion.
- **A-R2-F06** — post-shutdown operations raised generic `RuntimeError` from the
  closed registry. Fix: map closed registration to `RuntimeShuttingDownError`
  when the runtime is SHUTTING_DOWN.

A-R1 and A-R2 regressions (including cleanup ordering and ownership retention)
remain passing. G-01..G-04 and the deterministic portion of G-05 now have offline evidence;
this is not independent review and does not close Stage 10-C.

## 10-A6 A-R3 interrupted deletion and startup safety

Added `tests/stage10/ar3_support.py` plus three focused modules that keep the
real Stage 5 deletion/reconciliation/source-safety/verification services, the
real maintenance graph and the real coordinator boundaries, using the Stage 3
transaction-aware PostgreSQL fake and an independently recorded Milvus
projection fake:

- `tests/stage10/test_ar3_source_safety.py` — AR3-01, AR3-02, AR3-09.
- `tests/stage10/test_ar3_interrupted_deletion.py` — AR3-03..AR3-07.
- `tests/stage10/test_ar3_startup_recovery.py` — AR3-08, AR3-10.

Confirmed A-R3 defects fixed (each regression executed against the reverted
pre-fix path):

- **A-R3-F01** — A failed existence check authorized destructive deletion:
  `Path.exists()` collapsed PermissionError, I/O failure, symlinks and invalid
  paths to "missing", and deletion revalidation had no root or path validation.
  Fix: a fail-closed `classify_source` used by reconciliation, source safety and
  deletion; only a confirmed absent regular source authorizes deletion;
  unreadable/unavailable sources report `SOURCE_UNAVAILABLE`.
- **A-R3-F02** — Inaccessible or non-directory knowledge roots became an
  initialized-BLOCKED runtime (or even SERVING for an unreadable empty root)
  instead of the approved hard bootstrap failure. Fix: the coordinator maps
  `KNOWLEDGE_DIRECTORY_UNAVAILABLE` to `RuntimeBootstrapFailedError`, and
  `StartupSourceSafetyService` validates that the root is a directory and is
  scannable before reporting safety.
- **A-R3-F03** — Maintenance verdicts were not propagated into the health
  registry, so a runtime blocked before initial verification could complete a
  successful explicit recovery to SERVING yet remain unready and reject every
  query; an authoritative UNSAFE verdict also left knowledge marked verified.
  Fix: `run_maintenance`/`recover` record `knowledge_verified` from the
  returned `ServingSafety` before refreshing admission. The A-R2
  `test_unsafe_maintenance_outcome_is_not_overridden_by_healthy_probes`
  assertion was corrected to state that probe health remains but verified
  knowledge does not.

Preserved contracts: Milvus-first/PostgreSQL-last deletion, exact absence
verification before canonical deletion, marker set inside the canonical
transaction, no startup scan/sync, no automatic deletion for missing sources,
no new mode, no new schema, no second recovery algorithm.

## 10-A7 A-R4 Query Cache, freshness and concurrent cache correctness

Added `tests/stage10/cache_support.py` plus four focused modules using the real
`RedisCacheProvider` over a stateful raw-Redis transport fake and an explicit
marker transaction fake:

- `tests/stage10/test_ar4_identity.py` — AR4-01 identity/eligibility.
- `tests/stage10/test_ar4_commit.py` — AR4-02/03/04 prepare, ambiguity, concurrency.
- `tests/stage10/test_ar4_freshness.py` — AR4-05/06/08/11 invalidation, marker, recovery, namespace.
- `tests/stage10/test_ar4_lifecycle.py` — AR4-07/09/10/12 runtime lifecycle, exclusion and settlement.

Confirmed A-R4 defects fixed (each regression executed against the reverted
pre-fix path):

- **A-R4-F01** — `SessionQueryCache.commit` classified a submitted Redis
  `DependencyError` as BYPASSED, claiming non-execution although SET may have
  applied (timeout/lost connection). Fix: submitted dependency failures are
  conservatively UNKNOWN; BYPASSED is reserved for no-attempt paths. The F-03
  regression keeps its core assertion (no exception escapes settlement and no
  STORED claim) with the corrected outcome.
- **A-R4-F02** — Lazy recovery checked the due throttle outside the enforcement
  lock, so callers already queued on a held recovery could each repeat
  invalidation. Fix: re-check due eligibility inside the same lock as
  enforcement. A regression holds the lock during a delayed invalidation and
  proves one recovery attempt instead of five.

Verified lifecycle outcomes:

- Delayed/late submitted SETs settle inside the query lease; maintenance cannot
  enter while the write may still publish; the write is invalidated before
  serving. Drain timeout preserves the valid query and aborts maintenance.
- Shutdown with a stalled write raises `RuntimeShutdownFailedError`, retains
  resources/ownership and the query lease; after settlement a later shutdown
  succeeds.
- Redis outage with marker=true keeps serving with all cache reads/writes
  bypassed; controlled recovery invalidates stale entries, clears the marker
  and restores normal population. Redis failure does not block otherwise safe
  knowledge.
- Marker ambiguity is conservative: applied-with-lost-ack is reported BYPASSED,
  but the persisted PostgreSQL marker remains the sole authority for trust;
  trust is only exercised after complete invalidation and a confirming read.
- Canonical marker corruption propagates; invalidation is limited to
  `atlasrag:query:*` (Parent and unrelated keys preserved, no FLUSH).

Assumptions recorded: Redis remains disposable derived storage; the supported
lifecycle does not protect against an arbitrary stale Redis snapshot restored
externally. The Redis client uses default redis-py socket timeout/retry
settings; bounded caller waiting is provided by the coordinator timeout and
does not prove remote non-execution.

Pending transport follow-up closed offline in A-R6 (live Redis verification
remains Stage 10-B):

```text
AR4-T01 — Redis Transport Settlement

Deterministic Offline Portion: RESOLVED (A-R6)
- Explicit bounded connect/socket/health-check and zero-retry policy configured
  in production Redis client construction.
- Server-side atomic Lua write fencing prevents delayed or unacknowledged
  in-flight Query Cache writes from surviving past invalidation, barrier
  revocation, or runtime restart.

Real Infrastructure Portion: PENDING Stage 10-B
- Real isolated Redis transport interruption/restart and end-to-end settlement
  checks during Stage 10-B.
```

## 10-A8 A-R5 Parent Context Cache, canonical validation and verification

Added `tests/stage10/parent_cache_support.py`,
`tests/stage10/test_ar5_parent_cache.py`, and
`tests/stage10/test_ar5_lifecycle.py`, preserved
`tests/stage10/test_parent_cache.py::test_valid_text_parent_cache_hit_reconstructs_authoritative_provenance`
(with canonical chunk/header fixture identities), and added repository header
contract tests in `tests/persistence/test_postgres_contracts.py`.

Confirmed A-R5 defects fixed (each regression executed against `6ffab31`):

- **A-R5-F01** — `CanonicalChunkHeader` omitted `sheet_name`, `metadata`, and
  `strategy_metadata`, so cache hits skipped `ChunkPersistenceMetadataV1`,
  `ChunkStrategyMetadataV1`, `normalize_section_path`, and `derive_chunk_id`
  validation. Fix: projected all non-content metadata columns in
  `CanonicalChunkHeader` and `ChunkRepository` (including batch
  `list_canonical_context_headers`), added shared `validate_text_parent_invariants`
  and `validate_text_parent_chunk_header` (falling back to full read only when
  a lightweight stub omits metadata).
- **A-R5-F02** — `ParentContextService.expand` bypassed Parent Cache whenever
  any `TABLE` reference appeared in `refs`. Fix: resolve `TEXT_PARENT` entries
  via cache-aside while keeping `TABLE` canonical, preserving duplicate checks
  across the complete input and reassembling exact original `refs` order.
- **A-R5-F03** — Cache fill unconditionally instantiated `CachedParentContext`,
  raising `ValidationError` on legal whitespace-only nonempty `TEXT_PARENT`
  content. Fix: preserve exact canonical content, return valid `LocalEvidence`,
  and bypass cache population when content cannot be represented by
  `CachedParentContext`.
- **A-R5-F04** — `_expand_text_cached` opened a PostgreSQL UoW across Redis
  `get_parent`, repeated document lookups, and read misses one-by-one via
  `get_by_id`. Fix: execute Redis GETs before opening one short PostgreSQL UoW,
  deduplicate document reads, batch header and miss (`list_by_ids`) queries,
  and perform Redis `put_parent` writes after UoW exit.
- **A-R5-F05** — `ChunkRepository.get_canonical_context_header` normalized
  missing rows/metadata inconsistently and leaked `ValidationError` on corrupt
  rows. Fix: validate rows via `_validate_header_row` and
  `validate_canonical_row` so corrupt rows raise `CanonicalDataError` and mark
  the UoW failed.

## 10-A9 A-R6 final offline correctness and contract reconciliation

Added deterministic tests across `tests/stage10/test_ar6_redis_transport.py`,
`tests/stage10/test_ar6_freshness_lifecycle.py`,
`tests/stage10/test_ar6_composition_contracts.py`, and
`tests/contract/test_config_and_fingerprints.py`:

- **A-R6.1 (`AR4-T01` deterministic resolution):**
  - `RuntimeOperationalConfig` now defines bounded
    `redis_connect_timeout_seconds`, `redis_socket_timeout_seconds`, and
    `redis_health_check_interval_seconds` (excluded from `pipeline_fingerprint`
    and `runtime_fingerprint`), and `ApplicationComposition.start` configures
    `redis.from_url` with explicit timeouts, TCP keepalive, health-check
    interval, `retry_on_timeout=False`, `retry_on_error=[]`, and
    `Retry(NoBackoff(), 0)`.
  - `RedisCacheProvider` and `providers/cache/keys.py` expose technical
    write-fence primitives (`QUERY_WRITE_FENCE_KEY`, `get_query_write_fence`,
    `revoke_query_write_fence`, `establish_query_write_fence`,
    `ensure_query_write_fence`, `get_fenced_query`, `put_fenced_query`) using
    atomic Lua `EVAL` checks.
  - `SessionQueryCache` requires an active established write fence when
    `require_write_fence=True` (enabled in production and Stage 10 runtime
    harnesses), and `CacheFreshnessBarrier` revokes any active fence *before*
    scanning/unlinking `atlasrag:query:*`, establishes a fresh fence *before*
    clearing the authoritative PostgreSQL marker (and once on `marker=False`
    startup), while preserving the active fence across healthy periodic
    `recover_if_due()` inside concurrent `QueryLease`s and preserving all
    `atlasrag:parent:*` keys.
  - `DelayedRemoteRawRedis` exercises delayed server execution after client
    timeout/connection drop/`UNKNOWN` + `QueryLease` release, revocation prior
    to marker clear, `UNSAFE`->`BLOCKED`->`recover()`, cancellation settlement,
    and restart with `marker=False`: stale writes are rejected server-side (`0`).
- **A-R6.2 (Production composition contract reconciliation):**
  - `ApplicationComposition.start` now enforces `startup_attempts` /
    `startup_retry_delay_seconds` exclusively on transient startup dependency
    errors (cleaning up failed pools cleanly), soft-degrades BGE reranker
    initialization failures to `None`/`_UnavailableReranker`, initializes
    `MilvusIndexProvider` with frozen `COLLECTION_NAME` (`"atlas_chunks"`) and
    `analyzer=config.part_i.bm25_analyzer`, and wires a soft SearXNG HTTP probe
    into `HealthRegistry`.
  - `validate_runtime_composition_config` and
    `validate_shared_resource_contracts` fail explicitly with
    `ConfigurationError` before resource acquisition or business construction on
    any unsupported `ModelConfig`, `PartIConfig`, `RetrievalConfig`, or
    `AnswerConfig` value or mismatched `SharedResources` identity/dimension/analyzer.

## 10-A10 Verification

*(Historical Stage 10-A snapshot below preserved unchanged; superseded by Stage 10-B `12/12` real-infrastructure completion, Stage 10-C3-R1 `PASS` on `be5a2876`, and [`stage-10-closeout/CLOSEOUT_REPORT.md`](stage-10-closeout/CLOSEOUT_REPORT.md).)*

- `make test-stage10`: PASS — 304 tests.
- `make verify`: PASS — 3611 passed, 1 skipped, 108 deselected.
- `ruff check .`, `ruff format --check .` (472 files), strict `mypy` (386
  source files), `make architecture-check`, `git diff --check`: PASS.
- Stage 9 raw artifact integrity: 12/12 SHA-256 and size matches.
- `AR4-T01` deterministic portion: RESOLVED offline; real Redis verification
  remains Stage 10-B.
- Stage 10-B (isolated real PostgreSQL/Milvus/Redis integration): NOT_STARTED.
- Stage 10-C (independent review): NOT_STARTED.
- Real Qwen/BGE GPU initialization, MinerU SDK subprocess settlement, RTX 3070
  VRAM/OOM verification, OS signal delivery, launcher termination, and
  deployment performance measurement remain deferred to later deployment/performance
  stages.

## Stage 10-B handoff

See [`stage-10b-handoff.md`](stage-10b-handoff.md) for the complete Stage 10-B
real isolated infrastructure gate specification.
