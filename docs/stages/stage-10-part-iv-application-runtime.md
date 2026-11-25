# Stage 10 — Part IV: Application Runtime Foundation

## Status

**PARTIAL — runtime foundation implemented and offline-verified. Isolated real
PostgreSQL/Milvus/Redis integration and real GPU/system acceptance were not
executed in this environment.**

Stage 9 remains closed under its amended acceptance scope. Stage 10 does not
merge automatically.

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
| `runtime/composition.py` | `ApplicationComposition`, `SharedResources`, `BusinessBundle` | Shared infrastructure and lifecycle root |
| `runtime/config.py` | `load_runtime_config` | Explicit JSON/env config loading |
| `services/sync/source_safety.py` | `StartupSourceSafetyService`, `SourceSafetyResult` | Conservative interrupted-deletion startup check |
| `services/local_evidence/parent_context.py` | optional cache in `ParentContextService` | TEXT_PARENT cache-aside |
| `repositories/postgres/chunks.py` | `CanonicalChunkHeader`, `get_canonical_context_header` | Lightweight canonical validation |
| `services/cache/session.py` | `prepare`/`commit`, `CacheCommitOutcome` | Completed-result cache commit boundary |

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

Pool minimum:

```text
2 * max_document_concurrency + 2
```

The `2*C` terms are the existing document-build advisory lease and UoW. Two
additional connections cover reconciliation/verification/health/runtime
metadata headroom. Default maximum is `max(16, minimum)`. Query UoWs are short
and at most one per query; the pool provides bounded backpressure. GPU
concurrency is not treated as database demand.

## 5. Cancellation and cache commit

`SessionQueryCache.prepare` performs policy, trust, size and payload
construction only. `commit` submits the Redis write and settles it under a
cancellation-aware boundary. Outcomes are `STORED`, `BYPASSED`, `UNKNOWN`.
Cancellation before commit does not submit. An applied write with lost
acknowledgement may leave a complete eligible entry and is never reported as
certainly absent. No same-key deletion is performed during cleanup.

The canonical Query Cache key is unchanged; no KB revision is included.

## 6. Freshness and Parent Cache

The persisted `query_cache_invalidation_required` marker remains authoritative.
The barrier invalidates only `atlasrag:query:*`, then clears the marker and
re-reads it. Failures and ambiguity leave the cache bypassed. Parent Cache keys
and unrelated keys are preserved.

Parent Cache is TEXT_PARENT-only. TABLE remains canonical. Cache hits validate
payload identity, current published revision and a lightweight canonical
header. Provenance is reconstructed from authoritative canonical metadata.
Cached content equality with PostgreSQL is not cryptographically proven; the
local disposable-cache assumption is explicit.

## 7. Deferred / not verified

- Real isolated PostgreSQL/Milvus/Redis integration suite.
- Real Qwen/BGE GPU initialization, MinerU guarded boundary measurement,
  RTX 3070 VRAM/OOM and SDK subprocess settlement.
- Real isolated infrastructure and GPU verification remain pending; the
  default production business composition now exists in
  `runtime/production.py`.
- Final transport/CLI/streaming/backup/performance work.
