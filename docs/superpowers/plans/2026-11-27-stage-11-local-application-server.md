# Stage 11 Implementation Plan — Local Application Server

- **Status:** `READY_FOR_OWNER_REVIEW` (Planning Only — **Do NOT begin implementation until explicit owner approval**)
- **Baseline (`origin/main`):** `9cd2344f2076d60ee53568eb35aaa625474dc645`
- **Design Specification:** [`docs/stages/stage-11-local-application-server.md`](../../stages/stage-11-local-application-server.md)
- **Acceptance Matrix:** [`docs/stages/stage-11-acceptance.md`](../../stages/stage-11-acceptance.md)
- **Scope Refinement:** [`docs/architecture/ADR-002-part-iv-web-demo-and-mcp-milestone-refinement.md`](../../architecture/ADR-002-part-iv-web-demo-and-mcp-milestone-refinement.md)

---

## 1. Execution Rules & Dependency Order

Implementation proceeds sequentially across five dependency-ordered Work Packages (`A` -> `B` -> `C` -> `D` -> `E`):

```text
[WP-A: Server Foundation & Minimal Runtime Extensions]
          |
          +-------------------+
          v                   v
[WP-B: Sessions & Query]   [WP-C: Safe SSE & Control Operations]
          |                   |
          +---------+---------+
                    v
      [WP-D: Operational CLI & Process Launcher]
                    |
                    v
[WP-E: Offline, Real-Transport, Real-Infra & Independent Review]
```

**Global Stop Conditions (Apply to ALL Work Packages):**
1. Stop immediately if any requirement forces modifying `UserTurnRequest`, `QueryFilters`, `FinalResponse`, `FinalCitation`, `SessionQueryCache` key format, `CacheFreshnessBarrier` invariants, or `RuntimeGate` transition rules.
2. Stop immediately if `ApplicationComposition` or `ApplicationCoordinator` would be instantiated outside `atlasrag serve`.
3. Stop immediately if any Stage 13 MCP SDK/endpoints/migrations or Stage 12 Web frontend assets are pulled into Stage 11.
4. Keep `make verify` green at the end of every work package.

---

## 2. Work Package A — Server Foundation & Minimal Runtime Extensions

### Scope
- Add `fastapi>=0.143.0,<0.144.0` and `uvicorn>=0.54.0,<0.55.0` to `pyproject.toml` `[project.dependencies]` and `[project.scripts]` (`atlasrag = "atlasrag.cli.main:main"`), matching locked `uv.lock` versions (`fastapi==0.143.0`, `uvicorn==0.54.0`).
- Implement `ServerConfig` & loader (`src/atlasrag/server/config.py`), transport DTOs (`src/atlasrag/server/schemas.py`), stable error translation (`src/atlasrag/server/errors.py`), and local transport security (`src/atlasrag/server/auth.py`).
- Implement the minimal backward-compatible Stage 10 runtime extensions (`HealthRegistry` live `query_cache` callback, `ApplicationCoordinator.status(refresh=...)`, `QueryCacheStatus` + `QueryResponseEnvelope.cache_status` / `elapsed_ms`, `SessionQueryCache.lookup_with_status`, `RetrievalRunner` fact propagation, and `InvocationObserverMux` wiring in `production.py`).
- Implement skeleton `ApplicationServerService` (`src/atlasrag/server/service.py`) and FastAPI app factory with lifespan (`src/atlasrag/server/app.py`) exposing `/api/v1/health/live`, `/api/v1/health/ready`, and `/api/v1/status`.

### Dependencies
- Accepted Stage 10 runtime (`origin/main` `9cd2344f`) and owner approval of `OD11-01`..`OD11-03`.

### Expected Files / Modules
- Modified: `pyproject.toml`, `uv.lock` (only if `uv lock --check` requires metadata sync for promoting existing `fastapi`/`uvicorn` entries), `scripts/check_import_boundaries.py`.
- Minimal extensions to existing Stage 10 files:
  - `src/atlasrag/runtime/health.py`
  - `src/atlasrag/runtime/coordinator.py`
  - `src/atlasrag/runtime/composition.py`
  - `src/atlasrag/runtime/production.py`
  - `src/atlasrag/services/cache/session.py`
  - `src/atlasrag/graphs/retrieval/graph.py`
  - `src/atlasrag/application/retrieval.py`
- New server modules:
  - `src/atlasrag/server/__init__.py`
  - `src/atlasrag/server/config.py`
  - `src/atlasrag/server/schemas.py`
  - `src/atlasrag/server/errors.py`
  - `src/atlasrag/server/auth.py`
  - `src/atlasrag/server/service.py`
  - `src/atlasrag/server/app.py`

### Allowed Production Changes
- Only the minimal backward-compatible Stage 10 extensions listed above (zero behavioral changes to existing Stage 10 callers/tests) plus new `src/atlasrag/server/*` modules.

### Required Tests (`tests/stage11/`)
- `test_server_config_and_errors.py`: loopback validation, non-loopback rejection, body/query bounds, all 15 `ErrorCode` + transport code HTTP mappings, secret/path redaction.
- `test_server_auth.py`: Host allowlist, Origin/Referer cross-origin rejection, `X-AtlasRAG-Client` CSRF guard, 64 KiB body limit (`413`), `0o600` `control.token` validation, zero wildcard CORS.
- `test_server_health_and_lifespan.py`: startup `SERVING` vs `BLOCKED` vs hard bootstrap failure (`RuntimeBootstrapFailedError`, `RuntimeOwnershipConflictError`), distinct `/health/live`, `/health/ready`, `/status`, soft Redis/SearXNG/BGE degradation, zero DeepSeek probe calls.
- Boundary test update verifying `service.py` / `schemas.py` / `errors.py` do not import `fastapi`/`starlette`/`uvicorn`.

### Acceptance Criteria
- Satisfies `S11-01` (boundary), `S11-03`, `S11-04`, `S11-19`, `S11-20`, `S11-26` (regression). All existing `tests/stage10/` tests pass unmodified.

### Stop Conditions
- If promoting `fastapi`/`uvicorn` in `pyproject.toml` triggers unexpected version downgrades/conflicts in `uv.lock`, stop and report before proceeding.

---

## 3. Work Package B — Session Registry & Synchronous Query API

### Scope
- Implement process-local bounded `SessionRegistry` (`src/atlasrag/server/sessions.py`) with `web_<hex>` / `cli_<hex>` ID generation, idle TTL, `max_sessions` pruning, `max_in_flight_per_session`, and in-flight-safe deletion/expiration.
- Wire `POST /api/v1/sessions`, `DELETE /api/v1/sessions/{session_id}`, and `POST /api/v1/query` in `service.py` and `app.py`.
- Implement transport concurrency semaphore (`max_concurrent_queries`), request timeout (`query_timeout_seconds`), task-wrapped coordinator execution (`asyncio.create_task`), and client-disconnect cancellation + mandatory task settlement.
- Connect truthful `cache_status` (`HIT`, `MISS`, `BYPASSED`, `UNKNOWN`), `elapsed_ms`, and fresh `trace_id` to `QueryResponse`.

### Dependencies
- Work Package A complete and verified.

### Expected Files / Modules
- New: `src/atlasrag/server/sessions.py`
- Updated: `src/atlasrag/server/service.py`, `src/atlasrag/server/app.py`, `src/atlasrag/server/schemas.py`

### Allowed Production Changes
- `src/atlasrag/server/*` only (unless a bug in WP-A's `QueryResponseEnvelope` wiring is found).

### Required Tests (`tests/stage11/`)
- `test_sessions.py`: ID safety (`is_cache_key_component_safe`), `max_sessions` exhaustion (`503 RESOURCE_EXHAUSTED`), idle TTL pruning, deletion while query in-flight (new queries `404`, active query finishes cleanly), zero Redis scan/del on delete.
- `test_query_api.py`: `UserTurnRequest` + `QueryFilters` forwarding, rejection of `top_k`/`web_enabled`/`model` (`422`), `FinalResponse` & citation preservation, `HIT`/`MISS`/`BYPASSED`/`UNKNOWN` truthfulness, cross-session cache isolation, `query_timeout_seconds` cancellation & `QueryLease` settlement, transport semaphore saturation (`503 RESOURCE_EXHAUSTED`).

### Acceptance Criteria
- Satisfies `S11-06`, `S11-07`, `S11-08`, `S11-09`, `S11-12`, `S11-15` (HTTP path).

### Stop Conditions
- If any test shows leaked `QueryLease`, leaked `SessionRecord` refcount, or unvalidated extra query option passing through, stop and fix before WP-C.

---

## 4. Work Package C — Safe SSE Streaming & Operational Control Endpoints

### Scope
- Implement `InvocationEventBus` (`src/atlasrag/server/events.py`) projecting `ProductionObservation` onto strict SSE allowlist (`accepted`, `progress`, `final`, `error`) with bounded `progress_queue`, drop-oldest overflow for slow consumers, dedicated terminal event slot, heartbeats, and cache-hit non-fabrication.
- Wire `POST /api/v1/query/stream` in `app.py` and `service.py`, converging on the exact same `ApplicationServerService.execute_query` execution path as `POST /api/v1/query`, with disconnect detection -> `query_task.cancel()` -> mandatory settlement await.
- Implement bounded process-local `MaintenanceOperationRegistry` (`src/atlasrag/server/operations.py`) and wire `POST /api/v1/control/sync`, `POST /api/v1/control/recover`, `GET /api/v1/control/operations/{operation_id}`, and `POST /api/v1/control/shutdown` (all Bearer-token protected).

### Dependencies
- Work Package B complete and verified.

### Expected Files / Modules
- New: `src/atlasrag/server/events.py`, `src/atlasrag/server/operations.py`
- Updated: `src/atlasrag/server/service.py`, `src/atlasrag/server/app.py`, `src/atlasrag/server/schemas.py`

### Allowed Production Changes
- `src/atlasrag/server/*` (and optional minimal `ObservationStage` hook in `rag_core/subgraph.py` / `retrieval/graph.py` only if genuine `CACHE` / `WEB` stages are instrumented cleanly without altering RAG routing/semantics).

### Required Tests (`tests/stage11/`)
- `test_sse_streaming.py`: wire format (`accepted`, `progress`, `final`, `error`), `: heartbeat` frames, slow-consumer queue overflow without blocking RAG or losing `final`, cache-hit SSE emits zero retrieval/answer progress events, concurrent streams never leak across `trace_id`s, disconnect cancels and settles `QueryLease`, forbidden-content assertion (zero CoT/prompts/candidates/stacktraces).
- `test_control_operations.py`: Bearer auth enforcement (`401`), `sync` in `SERVING` (`202` -> `SUCCEEDED`), `sync` in `BLOCKED` (`503`), `recover` in `BLOCKED` (`202` -> `SERVING`), `recover` in `SERVING` (`503`), duplicate maintenance (`409 MAINTENANCE_BUSY`), drain timeout (`FAILED` + `MAINTENANCE_DRAIN_TIMEOUT`, mode restored), operation survives client disconnect, FIFO bounded retention (`32`), `POST /control/shutdown` closes admission immediately (`503 RUNTIME_SHUTTING_DOWN`).

### Acceptance Criteria
- Satisfies `S11-05` (recovery), `S11-13`, `S11-14`, `S11-15` (SSE path), `S11-16`, `S11-17`, `S11-18`.

### Stop Conditions
- If SSE observation delivery ever blocks a graph coroutine or drops a terminal `final`/`error` event under backpressure, stop and resolve before WP-D.

---

## 5. Work Package D — Operational CLI & Process Launcher Lifecycle

### Scope
- Implement `ServerLauncher` (`src/atlasrag/server/launcher.py`): programmatic `uvicorn.Server` (`workers=1`, `reload=False`), `SIGINT`/`SIGTERM` handlers closing gate admission immediately, `timeout_graceful_shutdown >= 2 * shutdown_timeout_seconds + 5.0`, `server.json` & `control.token` lifecycle, exit codes (`0`, `2`, `3`, `4`), and lock retention on `RuntimeShutdownFailedError`.
- Implement `argparse` + `httpx` CLI (`src/atlasrag/cli/main.py` & `__init__.py`): `serve`, `ask`, `health`, `status`, `sync`, `recover`, `stop` (including `--force` bounded `SIGTERM -> SIGKILL` escalation strictly on verified server PID), and `~/.local/state/atlasrag/cli_session.json` (`0o600`) for same-session CLI cache hits.

### Dependencies
- Work Packages A, B, and C complete and verified.

### Expected Files / Modules
- New: `src/atlasrag/server/launcher.py`, `src/atlasrag/cli/__init__.py`, `src/atlasrag/cli/main.py`
- Updated: `scripts/check_import_boundaries.py` (enforce CLI never imports `atlasrag.runtime.composition`, `fastapi`, `uvicorn` except in `serve` handler).

### Allowed Production Changes
- `src/atlasrag/server/launcher.py`, `src/atlasrag/cli/*`, `scripts/check_import_boundaries.py`.

### Required Tests (`tests/stage11/`)
- `test_launcher_lifecycle.py`: signal handling, `shutdown_failed` -> exit `4` + lock retained, ownership conflict -> exit `3`, config failure -> exit `2`, verified PID check before `--force` termination.
- `test_cli.py`: `argparse` parsing, `httpx.MockTransport` for `ask` (JSON & `--stream`), `cli_session.json` reuse across invocations + `--new-session` + `--session-id` + auto-renew on `404`, `health`, `status`, `sync --wait`, `recover --wait`, `stop`, exit code matrix (`0/1/2/3/4/130/143`), safe error formatting.

### Acceptance Criteria
- Satisfies `S11-01`, `S11-02`, `S11-21`, `S11-22`, `S11-23`.

### Stop Conditions
- If any CLI command other than `serve` triggers `ApplicationComposition`, or if `stop --force` can target an unverified PID, stop immediately.

---

## 6. Work Package E — Real-Transport, Real-Infra Integration & Independent Review

### Scope
- Add Makefile targets (`test-stage11`, `test-stage11-integration`, `stage11-verify`) and Stage 11 CI/integration workflow coverage (`.github/workflows/integration.yml`) preserving existing CI tests (`tests/test_ci_workflow.py`).
- Implement **Layer B** real-process Uvicorn transport integration suite (`tests/stage11/test_real_server_transport.py`).
- Implement **Layer C** isolated real PostgreSQL/Milvus/Redis integration suite (`tests/integration/test_stage11_server_e2e.py` + `scripts/stage11_verify.sh` reusing `scripts/stage10_isolation.py` fail-closed isolation).
- Execute full verification gates, Stage 9 `12/12` integrity check, and **Layer D Independent Review** before producing Stage 11 closeout report, committing, and pushing for PR review.

### Dependencies
- Work Packages A–D complete and verified.

### Expected Files / Modules
- New: `tests/stage11/test_real_server_transport.py`, `tests/integration/test_stage11_server_e2e.py`, `scripts/stage11_verify.sh`, Stage 11 completion/review docs.
- Updated: `Makefile`, `.github/workflows/integration.yml` (additive job only, preserving all `stage2`–`stage10` jobs and assertions in `tests/test_ci_workflow.py`), `tests/test_ci_workflow.py` (additive Stage 11 CI check).

### Allowed Production Changes
- Bug fixes uncovered during real-process or real-infra integration only (must stay strictly within Stage 11 scope and pass all regression checks).

### Required Tests & Verification Gates
1. `make verify` (`ruff format --check .`, `ruff check .`, `mypy src/atlasrag scripts tests`, `make architecture-check`, `pytest` including Layer A & Layer B tests).
2. `make test-stage10` (Stage 10 regression).
3. `make test-stage11-integration` (Layer C isolated real PostgreSQL `17.8` / Milvus `2.6.20` / Redis `8.2.10` with teardown `remaining=0`).
4. Stage 9 artifact integrity verification (`12/12` match against `docs/stages/stage-09-closeout/artifact_integrity.json`).
5. Independent Review covering architecture compliance, lifecycle/concurrency, security, cache correctness, isolation, and acceptance matrix `S11-01`–`S11-26`.

### Acceptance Criteria
- `26/26` Stage 11 acceptance requirements (`S11-01`–`S11-26`) **SATISFIED**, independent review **PASS**, remote CI & Integration checks green, Stage 9 `12/12` intact, and Stage 11 branch pushed for owner review (never auto-merged).

### Stop Conditions
- Any leaked container/volume/process (`remaining != 0`), any regression in `make verify` or `make test-stage10`, any `S11-*` failure, or any open blocking independent-review finding blocks completion.
