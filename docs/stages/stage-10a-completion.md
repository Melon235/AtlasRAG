# Stage 10-A Implementation Completion

Status: **A-R1, A-R2 and A-R3 COMPLETE offline. Stage 10-A remains PARTIAL
because A-R4..A-R6 matrices are incomplete. Stage 10 overall remains PARTIAL;
acceptance remains UNSATISFIED; PR #11 remains open/unmerged.**

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
remain passing. G-01, G-02, G-03 and the deterministic portion of G-04 now have
offline evidence; this is not independent review and does not close Stage 10-C.

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

## 10-A7 Startup/cache remaining matrices

Startup source availability and interrupted-deletion matrices are complete
under A-R3. Redis ambiguity, marker-clear failure, Parent miss/TABLE and
freshness failure matrices remain required deterministic work (A-R4/A-R5).

## 10-A8 verification

- `make test-stage10`: PASS — 123 tests.
- `make verify`: PASS — 3417 passed, 1 skipped, 108 deselected.
- `ruff check .`, `ruff format --check .`, strict `mypy` (375 files),
  `make architecture-check`, `git diff --check`: PASS.
- Stage 9 raw artifact integrity: 12/12 SHA-256 and size matches.
- Real isolated PostgreSQL/Milvus/Redis and real GPU evidence: NOT_EXECUTED
  (Stage 10-B). The Stage 10 integration Makefile target remains an offline
  alias and is not represented as real integration.

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
