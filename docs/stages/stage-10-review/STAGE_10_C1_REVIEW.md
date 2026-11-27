# Stage 10-C1 Independent Adversarial Review Record

- **Baseline Main Commit:** `2f3f487f9346a8067faabca1ee49820035d8ed8d`
- **Reviewed Frozen Candidate (`HEAD`):** `3a9de079281118879f247abc6e9b38a3239f7d5a`
- **Development Branch / PR:** `stage/10-part-iv-application-runtime` / `PR #11` (`OPEN / UNMERGED`)
- **Stage 10-C1 Verdict:** **FAIL** (6 release-blocking findings)

*(Historical self-review artifacts `R10-01-runtime-concurrency.md`, `R10-02-cache-knowledge-safety.md`, `R10-03-architecture-ownership.md`, and `R10-04-security-release.md` are preserved unmodified.)*

## 1. Release-Blocking Findings Confirmed Against `3a9de079`

| ID | Severity | Area | Summary | Disposition in Stage 10-C2 |
|---|---|---|---|---|
| `C10-RISK-01` | `HIGH` | Integration runner & fixture isolation | `scripts/stage10_verify.sh`, `Makefile`, `.github/workflows/integration.yml`, and `tests/integration/{conftest.py,stage10_helpers.py}` allowed default/dev env-file fallback (`atlasrag-local`), shared project/port/container/volume collisions, and `docker compose down -v --remove-orphans` teardown of pre-existing/non-Stage-10 volumes | Remediated (`scripts/stage10_isolation.py`, updated runner/fixtures/tests) |
| `F-R10-02-01` | `HIGH` | Global knowledge safety verification | `GlobalKnowledgeVerificationService._load_one` (`src/atlasrag/services/sync/verification.py`) used `map_document_from_stored`, failing valid `current_revision_id` `R1` whenever `building_revision_id` `R2` was `FAILED` or interrupted (`PARSED`/`CHUNKING`/`CHUNKED`/`INDEXING`) | Remediated (switched to `map_current_document_from_stored` + offline & real PG/Milvus regressions) |
| `F-R10-01-01` | `HIGH` | Startup rollback & ownership retention | `ApplicationCoordinator._start` and `ApplicationComposition.start` could drop cleanup error context or release `RuntimeOwnershipLock` prematurely on `close_resources` failure / ownership conflict / cancellation | Remediated + `R10-03-F02` retry/pool-close hardening |
| `F-R10-01-02` | `MEDIUM` | Cross-epoch health admission | With `refresh_interval > 0`, `HealthRegistry.refresh_if_stale()` and `ApplicationCoordinator.run_query()` could evaluate or cache stale-epoch unhealthy snapshots across drain abort without bounded current-epoch refresh/rejection | Remediated (lifecycle generation check in `refresh_if_stale` + coordinator cross-epoch guard) |
| `F-R10-01-03` | `MEDIUM` | Shared `ResourceGuard` shutdown lifecycle | `ResourceGuard.begin_shutdown()` existed in `src/atlasrag/runtime/resources.py` and `SharedResources.guard` was created in `composition.py`, but runtime shutdown never invoked `guard.begin_shutdown()` | Remediated (wired `begin_guard_shutdown` into `CoordinatorDependencies` and `shutdown()`) |
| `F-R10-02-02` | `MEDIUM` | Query Cache key safety vs domain validity | Domain-valid `UserTurnRequest.session_id` values containing `:` or ASCII control characters bypassed `UserTurnRequest` validation but raised `InvariantViolationError` in `query_cache_key` during cache lookup/commit | Remediated (conservative cache bypass before Redis/PG in `keys.py`/`policy.py`/`session.py`) |

## 2. Non-Blocking Observations Preserved from Stage 10-C1

| Observation ID(s) | C2 Disposition | Notes |
|---|---|---|
| `R10-03-F03` / `R10-04-F02` | `DEFERRED / NON-BLOCKING` | Preserved unchanged per targeted remediation scope |
| `R10-03-F04` | `DEFERRED / NON-BLOCKING` | Preserved unchanged per targeted remediation scope |
| `R10-01-F04`, `R10-01-F05` | `DEFERRED / NON-BLOCKING` | Preserved unchanged per targeted remediation scope |
| `OBS-R10-02-01`..`OBS-R10-02-03` | `DEFERRED / NON-BLOCKING` | Preserved unchanged per targeted remediation scope |
| `R10-03-F06`, `R10-03-F07` | `DEFERRED / NON-BLOCKING` | Preserved unchanged per targeted remediation scope |
| `R10-04-F03` | `RECONCILED IN C2` | Stale acceptance/completion/ledger/summary/handoff documentation reconciled alongside C1/C2/C3 records |
