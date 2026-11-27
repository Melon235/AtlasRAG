# Stage 11 Handoff: Local Server and Operational CLI

- **Predecessor Stage:** Stage 10 — Part IV: Application Runtime Foundation (`ACCEPTED / PENDING_MERGE`)
- **Reviewed Implementation HEAD:** `be5a287601ea988fe3b79dd4fed2c7aaafc4a7a8`
- **Next Planned Stage:** Stage 11 — Local Server and Operational CLI (`NOT_STARTED`)
- **Authorization Notice:** This document defines the completed Stage 10 runtime contracts consumed by Stage 11 and preserves deferred scope boundaries. It does **not** authorize starting Stage 11 implementation, creating a Stage 11 branch/worktree, or adding server/CLI/launcher/transport code.

---

## 1. Completed Stage 10 Runtime Contracts Consumed by Stage 11

Stage 11 must compose and expose the frozen in-process runtime foundation established in [`src/atlasrag/runtime/`](../../../src/atlasrag/runtime/):

1. **Composition Root & Production Wiring ([`composition.py`](../../../src/atlasrag/runtime/composition.py), [`production.py`](../../../src/atlasrag/runtime/production.py)):**
   - `ApplicationComposition(config).start()` is the sole production runtime composition root owning the shared PostgreSQL pool (`PostgresPool`, min size `2 * max_document_concurrency + 2`), single `MilvusIndexProvider` (`COLLECTION_NAME="atlas_chunks"`, `analyzer=bm25_analyzer`), bounded no-retry `RedisCacheProvider`, shared `ResourceGuard`, guarded Qwen embedding / optional BGE reranker, `GuardedMinerUParser`, `CacheFreshnessBarrier`, and `build_default_business`.
   - Operational config validation (`validate_runtime_composition_config`, `validate_shared_resource_contracts`) fails fast with `ConfigurationError` on unsupported semantic selectors or mismatched identities.
2. **Lifecycle & Operations Boundary ([`coordinator.py`](../../../src/atlasrag/runtime/coordinator.py)):**
   - `ApplicationCoordinator` exposes `run_query(request) -> QueryResponseEnvelope`, `run_maintenance() -> SyncResult`, `recover() -> SyncResult`, `health() -> HealthSnapshot`, `readiness() -> bool`, and idempotent single-flight `shutdown() -> None`.
   - `QueryResponseEnvelope` wraps `trace_id` and Part II `FinalResponse` without altering `FinalResponse` domain contracts.
3. **Single-Runtime Process Exclusion ([`ownership.py`](../../../src/atlasrag/runtime/ownership.py)):**
   - `RuntimeOwnershipLock` (`canonical_lock_path()` fixed at `~/.local/state/atlasrag/runtime.lock`, `0o700` parent, `0o600` regular non-symlink file, non-blocking exclusive `flock`) guarantees one runtime per OS user/WSL instance.
   - Startup `RuntimeOwnershipConflictError` does not close resources or release another runtime's lock.
   - Unsettled cleanup or shutdown errors (including `_pool_cleanup_failed` / `_cleanup_error` on bootstrap or `RuntimeShutdownFailedError` on unsettled workers) retain `RuntimeOwnershipLock` until process exit instead of releasing it prematurely.
4. **Atomic Admission, Drain & Task Tracking ([`gate.py`](../../../src/atlasrag/runtime/gate.py), [`modes.py`](../../../src/atlasrag/runtime/modes.py), [`tasks.py`](../../../src/atlasrag/runtime/tasks.py), [`maintenance.py`](../../../src/atlasrag/runtime/maintenance.py)):**
   - Six frozen `RuntimeMode` states (`STARTING`, `SERVING`, `DRAINING`, `MAINTENANCE`, `BLOCKED`, `SHUTTING_DOWN`).
   - `RuntimeGate` enforces mutual exclusion between admitted queries (`QueryLease`) and exclusive KB mutation/recovery (`MaintenanceLease` with `MaintenanceOrigin`), generation-guarded admission updates (`lifecycle_generation`), and origin-preserving drain timeout/cancellation aborts.
   - `InFlightTaskRegistry` tracks `TaskKind` ownership (`QUERY`, `MAINTENANCE`, `HEALTH`) and awaits task settlement before resource cleanup during `shutdown()`.
5. **Bounded GPU Guard & Health/Readiness ([`resources.py`](../../../src/atlasrag/runtime/resources.py), [`health.py`](../../../src/atlasrag/runtime/health.py)):**
   - `ResourceGuard` (`gpu_concurrency`), `GuardedEmbeddingProvider`, `GuardedRerankerProvider`, and `GuardedMinerUParser` arbitrate GPU-bound operations on one semaphore; `begin_shutdown()` prevents queued guard work from starting post-shutdown.
   - `HealthRegistry` (`HealthSnapshot`, `DependencyHealth`) runs bounded single-flight probes classifying PostgreSQL, Milvus, and Qwen embedding as hard readiness dependencies and Redis, SearXNG, and BGE reranker as soft-degradable dependencies.
6. **Freshness Barrier, Config & Error Contracts ([`cache_freshness.py`](../../../src/atlasrag/runtime/cache_freshness.py), [`config.py`](../../../src/atlasrag/runtime/config.py), [`config/models.py`](../../../src/atlasrag/config/models.py), [`domain/errors.py`](../../../src/atlasrag/domain/errors.py)):**
   - `CacheFreshnessBarrier` (`CacheFreshnessState.TRUSTED` / `BYPASSED`, `active_write_fence`) enforces PostgreSQL invalidation-marker authority and atomic Lua Redis write fencing across startup, maintenance, outage bypass, and recovery.
   - `load_runtime_config()` loads checked-in JSON (`ATLASRAG_CONFIG`) plus explicit `ATLASRAG_CONFIG_JSON` overrides with secret-redacted `ConfigurationError`; `RuntimeOperationalConfig` operational knobs remain excluded from `pipeline_fingerprint` and `runtime_fingerprint`.
   - Frozen error contracts preserved for Stage 11 translation: `RuntimeOwnershipConflictError`, `RuntimeNotReadyError`, `RuntimeShuttingDownError`, `MaintenanceBusyError`, `MaintenanceDrainTimeoutError`, `RuntimeBootstrapFailedError`, `RuntimeShutdownFailedError`, `ConfigurationError`.

---

## 2. Architectural Constraints Stage 11 Must Enforce

Per the frozen Architecture Manual ([`AtlasRAG_Complete_Final_Architecture_Design_Manual.md`](../../architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md)) and Stage 10 boundaries:

- **One runtime-owning local server:** Only the long-lived local runtime/server process calls `ApplicationComposition(config).start()` and holds `RuntimeOwnershipLock`.
- **Thin operational clients:** CLI query, maintenance, recovery, health, readiness, and status commands must talk to the single runtime-owning server over the Stage 11 local control transport rather than instantiating a second `ApplicationComposition`, pool, Milvus client, or model owner.
- **No query + mutation concurrency:** All server endpoints must respect `ApplicationCoordinator` / `RuntimeGate` exclusion; rejected concurrent requests map explicitly to `RuntimeNotReadyError` / `MaintenanceBusyError` / `RuntimeShuttingDownError`.
- **No unsafe forced in-process cleanup:** Do not bypass `InFlightTaskRegistry` settlement or release `RuntimeOwnershipLock` when cleanup fails or workers fail to settle; launcher-level forced termination and recovery sit outside in-process resource cleanup.
- **Preserve cache semantics:** Keep `SessionQueryCache` (`require_write_fence=True`, `expected_write_fence`, unsafe session-key bypass, `UNKNOWN` settlement) and `ParentContextService` (`TEXT_PARENT` only, `TABLE` canonical, `atlasrag:parent:*` preservation) intact.

---

## 3. Preserved Deferrals Allocated Beyond Stage 10

The following Part IV and transferred Part III items remain explicitly deferred beyond Stage 10 and were **not** claimed as complete in Stage 10 closeout:

- Localhost server, HTTP/IPC control transport, and operational CLI commands.
- Process-launcher ownership, OS signal integration (`SIGINT`/`SIGTERM`), forced process termination, and launcher-level recovery.
- Backup and restore workflows.
- Web UI and safe response/status streaming.
- Real Qwen/BGE CUDA weight initialization, GPU resource validation, and RTX 3070 VRAM/OOM measurements.
- Real MinerU SDK subprocess execution and cancellation settlement verification.
- Performance Runner and engineering latency/throughput/token telemetry (transferred Stage 9 requirements `S9-17` and `S9-21`) and approved Stage 9 evaluation/ablation deferrals (`S9-08`, `S9-19`, `S9-20`, `S9-22`).
- Preserved non-blocking Stage 10 review observations (`R10-03-F03`/`R10-04-F02`, `R10-03-F04`, `R10-01-F04`, `R10-01-F05`, `OBS-R10-02-01`..`03`, `R10-03-F06`, `R10-03-F07`).
