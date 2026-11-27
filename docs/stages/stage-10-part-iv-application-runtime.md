# Stage 10 — Part IV: Application Runtime Foundation

## Status

**COMPLETE / ACCEPTED / MERGED (`PASS`) — Stage 10-A (`A-R1`–`A-R6` offline,
`352` tests), Stage 10-B (`12/12` isolated real PostgreSQL/Milvus/Redis
integration), historical Stage 10-C1 (`FAIL` on `3a9de079`), Stage 10-C2,
historical Stage 10-C3 (`FAIL` on `5f74b201`, `5/6` closed), Stage 10-C2-R1
(`be5a2876`), Stage 10-C3-R1 final independent targeted re-review (`PASS`), and
Stage 10-C4 closeout (`ad96d23f`) COMPLETE; `PR #11` merged into `main` at
`833d1150d3cc508971f2dc92b6d625718be2fe78` (`10/10` post-merge CI/Integration
jobs PASS). Original acceptance requirements: `23/23` SATISFIED (`6/6`
blocking findings CLOSED, `10/10` gaps CLOSED within approved scope).
Under [`ADR-001-part-iv-web-mcp-architecture.md`](../architecture/ADR-001-part-iv-web-mcp-architecture.md), Stage 11 is
`Shared Application Server` (`NOT_STARTED`).**

Stage 9 remains closed under its amended acceptance scope (12/12 artifacts
verified). Stage 10 does not merge automatically.

## 1. Objective

Provide one in-process application boundary over existing Part I and Part II
services:

- one composition/lifecycle root;
- one machine-local ownership lock;
- one RuntimeGate;
- one shared ResourceGuard;
- one PostgreSQL pool, Milvus provider, Redis client and Qwen/BGE model owner;
- atomic query admission, exclusive maintenance, recovery, cache freshness,
  health/readiness and graceful shutdown.

No server, CLI, Web UI, streaming transport, backup engine or Performance
Runner is included.

## 2. Implemented modules

| Module | Symbols | Purpose |
|---|---|---|
| `runtime/modes.py` | `RuntimeMode` | Six frozen modes |
| `runtime/ownership.py` | `RuntimeOwnershipLock`, `canonical_lock_path` | Fixed per-user `flock` ownership |
| `runtime/gate.py` | `RuntimeGate`, `QueryLease`, `MaintenanceLease`, `MaintenanceOrigin` | Atomic admission, drain, origin-aware abort |
| `runtime/tasks.py` | `InFlightTaskRegistry`, `TaskKind` | Actual task ownership for shutdown |
| `runtime/resources.py` | `ResourceGuard`, `GuardedEmbeddingProvider`, `GuardedRerankerProvider` | Shared bounded GPU guard |
| `runtime/health.py` | `HealthRegistry`, `HealthSnapshot`, `DependencyHealth` | Bounded single-flight probes |
| `runtime/cache_freshness.py` | `CacheFreshnessBarrier`, `CacheFreshnessState` | Persisted marker invalidation/clear |
| `runtime/maintenance.py` | `MaintenanceCoordinator` | Existing `IndexSyncApplication` wrapper |
| `runtime/coordinator.py` | `ApplicationCoordinator`, `CoordinatorDependencies`, `QueryResponseEnvelope` | Start/query/maintenance/recovery/health/shutdown |
| `runtime/composition.py` | `ApplicationComposition`, `SharedResources`, `BusinessBundle`, `validate_runtime_composition_config`, `validate_shared_resource_contracts` | Shared infrastructure, startup retries, soft degrade, config validation and lifecycle root |
| `runtime/production.py` | `build_default_business` | Default Part I/II wiring with write-fenced Query Cache and strict contract validation |
| `runtime/config.py` | `load_runtime_config` | Explicit JSON/env config loading with secret redaction |
| `config/models.py` | `RuntimeOperationalConfig` | Bounded operational timeouts, pool minimum, and fingerprint exclusion |
| `providers/cache/keys.py` | `QUERY_WRITE_FENCE_KEY`, `parent_cache_key`, `query_cache_key` | Frozen cache key and write-fence key contracts |
| `providers/cache/redis.py` | `RedisCacheProvider`, atomic Lua write-fence operations | Technical cache and fenced Query Cache read/write boundary |
| `services/sync/source_safety.py` | `StartupSourceSafetyService`, `SourceSafetyResult` | Conservative interrupted-deletion startup check |
| `services/local_evidence/parent_context.py` | optional cache in `ParentContextService` | TEXT_PARENT cache-aside |
| `repositories/postgres/chunks.py` | `CanonicalChunkHeader`, `get_canonical_context_header` | Lightweight canonical validation |
| `services/cache/session.py` | `prepare`/`commit`, `CacheCommitOutcome`, `require_write_fence` | Completed-result cache commit boundary and fence enforcement |

`DocumentBuildRunner` is reused directly with injected shared resources. No
`owns_resources`, open, or close extension was added. `DocumentBuildOwner`
remains unchanged for standalone tools.

## 3. Runtime transitions

```text
STARTING -> SERVING         verified safe and ready
STARTING -> BLOCKED         initialized, unsafe knowledge
STARTING -> process failure hard bootstrap/root failure
SERVING -> DRAINING         normal maintenance
BLOCKED -> DRAINING         explicit recovery
DRAINING -> SERVING         abort with SERVING origin
DRAINING -> BLOCKED         abort with BLOCKED origin
DRAINING -> MAINTENANCE     all query leases settled
MAINTENANCE -> SERVING      authoritative safe + freshness handled
MAINTENANCE -> BLOCKED      unsafe/exception/cancellation
any applicable -> SHUTTING_DOWN
```

`SHUTTING_DOWN` is terminal. Recovery abort returns to BLOCKED, not SERVING.
Maintenance after mutation failure remains BLOCKED.

## 4. Ownership and pool

Canonical lock:

```python
Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/state/atlasrag/runtime.lock"
```

No path override. Scope is one runtime per OS user per supported WSL instance.
Lock file is never unlinked on normal release; fd held for process lifetime.

PostgreSQL pool sizing contract (`src/atlasrag/config/models.py`):

```text
postgres_pool_max_size >= 2 * max_document_concurrency + 2
postgres_pool_min_size <= postgres_pool_max_size
```

The `2*C` terms are the existing document-build advisory lease and UoW. Two
additional connections cover reconciliation/verification/health/runtime
metadata headroom. Validated defaults are `postgres_pool_min_size = 2` and
`postgres_pool_max_size = 16`. Query UoWs are short
and at most one per query; the pool provides bounded backpressure. GPU
concurrency is not treated as database demand.

## 5. Cancellation, Redis transport and cache commit

`ApplicationComposition.start` configures `redis.from_url` with bounded
`socket_connect_timeout`, `socket_timeout`, `socket_keepalive=True`,
`health_check_interval`, `retry_on_timeout=False`, `retry_on_error=[]`, and
`Retry(NoBackoff(), 0)`.

`SessionQueryCache.prepare` performs policy, trust, active write-fence check,
size, and payload construction with zero Redis mutations. When enabled in
production (`require_write_fence=True`), `commit` executes an atomic Lua
write-fenced `SET` against `QUERY_WRITE_FENCE_KEY` (`atlasrag:fence:query_write`)
and settles under a cancellation-aware boundary (`STORED`, `BYPASSED`,
`UNKNOWN`). Cancellation before commit does not submit. Transport ambiguities
classify conservatively as `UNKNOWN`, while stale delayed writes are rejected
server-side by Redis whenever the fence is revoked or rotated.

The canonical Query Cache key is unchanged; no KB revision is included.

## 6. Freshness barrier and Parent Cache

The persisted `query_cache_invalidation_required` marker remains authoritative.
When `marker=True`, `CacheFreshnessBarrier` revokes the write fence first,
invalidates only `atlasrag:query:*`, establishes a new write fence, and clears
then re-reads the PostgreSQL marker. On initial startup (`marker=False`), it
rotates the active write fence without scanning or deleting valid query entries,
while healthy periodic `recover_if_due()` inside concurrent `QueryLease`s
preserves the live fence unless Redis restarted without one. Parent Cache and
unrelated keys are preserved.

Parent Cache is TEXT_PARENT-only. TABLE remains canonical. Cache hits validate
payload identity, current published revision and a lightweight canonical
header. Provenance is reconstructed from authoritative canonical metadata.
Cached content equality with PostgreSQL is not cryptographically proven; the
local disposable-cache assumption is explicit.

## 7. Stage status, handoff and deferred verification

- **Stage 10-A (`A-R1`–`A-R6`):** COMPLETE (offline verified; see
  [`stage-10a-completion.md`](stage-10a-completion.md),
  [`stage-10a-verification-ledger.md`](stage-10a-verification-ledger.md), and
  [`stage-10-acceptance.md`](stage-10-acceptance.md)).
- **Stage 10-B (isolated real PostgreSQL/Milvus/Redis integration):**
  COMPLETE (`12/12` real-infra tests pass with fail-closed `scripts/stage10_isolation.py`
  ownership; `AR4-T01` live Redis settlement verified — see
  [`stage-10b-completion.md`](stage-10b-completion.md)).
- **Stage 10-C1 / C2 / C3 / C2-R1 / C3-R1 / C4 (independent review & closeout):**
  Historical Stage 10-C1 `FAIL` on `3a9de079` ([`stage-10-review/STAGE_10_C1_REVIEW.md`](stage-10-review/STAGE_10_C1_REVIEW.md)),
  Stage 10-C2 COMPLETE ([`stage-10-review/STAGE_10_C2_REMEDIATION.md`](stage-10-review/STAGE_10_C2_REMEDIATION.md)),
  historical Stage 10-C3 `FAIL` on `5f74b201` with `5/6` blockers closed and
  `F-R10-01-01` / `C3-RISK-01` open ([`stage-10-review/STAGE_10_C3_REVIEW.md`](stage-10-review/STAGE_10_C3_REVIEW.md)),
  Stage 10-C2-R1 targeted remediation COMPLETE ([`stage-10-review/STAGE_10_C2_R1_REMEDIATION.md`](stage-10-review/STAGE_10_C2_R1_REMEDIATION.md)),
  Stage 10-C3-R1 final independent targeted re-review **PASS / READY_FOR_FINAL_ACCEPTANCE**
  on `be5a2876` ([`stage-10-review/STAGE_10_C3_R1_REVIEW.md`](stage-10-review/STAGE_10_C3_R1_REVIEW.md)),
  and Stage 10-C4 closeout COMPLETE ([`stage-10-closeout/CLOSEOUT_REPORT.md`](stage-10-closeout/CLOSEOUT_REPORT.md),
  [`stage-10-closeout/ACCEPTANCE_AUDIT.md`](stage-10-closeout/ACCEPTANCE_AUDIT.md),
  [`stage-10-closeout/STAGE_11_HANDOFF.md`](stage-10-closeout/STAGE_11_HANDOFF.md);
  see also [`stage-10-review/REVIEW_SUMMARY.md`](stage-10-review/REVIEW_SUMMARY.md)).
- Real Qwen/BGE GPU initialization, MinerU guarded boundary measurement, RTX
  3070 VRAM/OOM and SDK subprocess settlement, OS signal delivery, launcher
  termination, and Stage 11–14 Part IV deliverables (**Stage 11** Shared
  Application Server, **Stage 12** Web Application, **Stage 13** Inbound MCP
  Server, **Stage 14** Deployment, Backup/Restore & Final Validation — see
  [`ADR-001-part-iv-web-mcp-architecture.md`](../architecture/ADR-001-part-iv-web-mcp-architecture.md))
  remain explicitly deferred beyond Stage 10 (`Stage 11: NOT_STARTED`).
