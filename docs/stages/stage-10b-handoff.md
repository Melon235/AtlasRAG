# Stage 10-B Handoff: Isolated Real Infrastructure Integration

## 1. Current Authoritative State

- **Branch / PR:** `stage/10-part-iv-application-runtime` (`PR #11` OPEN and UNMERGED against `main`)
- **Stage 10-A (`A-R1`–`A-R6`):** COMPLETE (offline verified; `make test-stage10` = `304 passed`, `make verify` = `3611 passed, 1 skipped, 108 deselected`, `mypy` = `386 source files`, `ruff`, `make architecture-check`, `git diff --check` PASS, Stage 9 raw artifacts `12/12` verified)
- **Stage 10-B (Isolated Real PostgreSQL / Milvus / Redis Integration):** `NOT_STARTED`
- **Stage 10-C (Independent Review & Release Gate):** `NOT_STARTED`
- **Stage 10 Overall / Acceptance:** `PARTIAL` / `UNSATISFIED`

## 2. Stage 10-B Objective and Required Isolation

Stage 10-B verifies `ApplicationComposition`, `ApplicationCoordinator`,
`RuntimeGate`, `MaintenanceCoordinator`, `CacheFreshnessBarrier`,
`SessionQueryCache`, and `ParentContextService` against **real isolated local
containers** (`postgres:17.8-alpine`, `redis:8.2.10-alpine`, `milvus:v2.6.20`
with `etcd`/`minio` per `deploy/local/compose.yaml`), while keeping **fake/mock
model, LLM, and SearXNG boundaries** so Stage 10-B remains GPU-free,
cost-free, and deterministic:

- **PostgreSQL isolation:** dedicated temporary schema/database (or clean
  per-test truncation/migration inside the isolated test database); never mutate
  default development data.
- **Milvus isolation:** dedicated Stage 10-B collection namespace/prefix or
  isolated test collection (`COLLECTION_NAME="atlas_chunks"` inside isolated
  test container or dedicated collection teardown), never mixing with Stage 9
  preserved artifacts or live pilot data.
- **Redis isolation:** dedicated isolated Redis DB (`ATLASRAG_TEST_REDIS_URL`
  or dedicated container `FLUSHDB` only inside Stage 10-B test fixture setup/teardown).
- **Ownership lock isolation:** temporary per-test `0o700` lock directory so
  tests never collide with active user-wide `~/.local/state/atlasrag/runtime.lock`.

## 3. Mandatory Real-Service Integration Scenarios

1. **Bootstrap & Empty KB:** `ApplicationComposition.start()` against live
   PostgreSQL/Milvus/Redis with empty clean knowledge directory reaches
   `SERVING`, passes `health()`/`readiness()`, creates/validates `atlas_chunks`,
   and closes cleanly.
2. **Controlled Synthetic Maintenance & Revision Lifecycle:** run controlled
   build/sync -> `MAINTENANCE` -> `SERVING`, verify published revision in PG +
   Milvus, verify `atlasrag:query:*` swept + `atlasrag:fence:query_write` rotated.
3. **Query Admission Concurrency & Maintenance Drain:** concurrent queries under
   `QueryLease` + maintenance wait in `DRAINING` until query leases settle.
4. **Live `AR4-T01` Redis Transport Settlement & Write Fencing:**
   - Real `Redis` (`decode_responses=False`, bounded `socket_connect_timeout`,
     `socket_timeout`, `health_check_interval`, `retry_on_timeout=False`,
     `retry_on_error=[]`, `Retry(NoBackoff(), 0)`).
   - Verify Lua `EVAL` `_GET_FENCED_QUERY_SCRIPT`, `_PUT_FENCED_QUERY_SCRIPT`,
     `_PUT_UNFENCED_IF_UNPROTECTED_SCRIPT`, `_ENSURE_QUERY_WRITE_FENCE_SCRIPT`
     execute cleanly against real Redis 8.2.
   - Verify delayed/dropped/stale-fenced writes are rejected (`0`) by real Redis
     after maintenance invalidation or fence rotation/restart.
5. **Query Cache Hit, Session/Fingerprint Isolation & Parent Cache:**
   - `SessionQueryCache` round-trip under real Redis & PG marker (`require_write_fence=True`).
   - `ParentContextService` (`TEXT_PARENT` cached hit/miss/header validation vs
     `TABLE` canonical read) and `atlasrag:parent:*` survival across `atlasrag:query:*`
     invalidation.
6. **PostgreSQL Freshness Marker Outage & Recovery:** Redis unavailable during
   maintenance leaves `query_cache_invalidation_required = True`, serves without
   cache, then clears marker and sweeps stale keys once Redis returns.
7. **Interrupted Deletion / Source Safety & Shutdown Cleanup:** Milvus-first /
   PG-last deletion, recovery from `BLOCKED`, orderly `shutdown()`, pool close,
   and `RuntimeOwnershipLock` release.

## 4. Makefile & CI Updates Required in Stage 10-B

- **Makefile:** replace the temporary offline alias `test-stage10-integration`
  (`Makefile:75`) with an explicit real isolated integration command
  (`ATLASRAG_RUN_INTEGRATION=1` against `tests/integration/test_stage10_*.py`).
- **GitHub Actions (`.github/workflows/integration.yml`):** add/update the
  Stage 10 integration job (`stage10-runtime` / `stage10-integration`) to
  provision isolated PostgreSQL, Redis, and Milvus (using `uv sync --frozen`
  and never representing the offline alias as live integration).

## 5. Explicit Non-Goals for Stage 10-B

- Do **not** run real Qwen/BGE GPU weight initialization, MinerU CUDA smoke, or
  RTX 3070 VRAM/OOM measurement inside `make test-stage10-integration` (keep
  separate hardware smoke / deployment verification gates).
- Do **not** upgrade Stage 10-C independent review statuses or start Stage 11
  transport/API/CLI/streaming/backup/performance work.
