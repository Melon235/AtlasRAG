# Stage 11 Detailed Design — Local Application Server

- **Status:** `COMPLETE / READY_FOR_PR_REVIEW` (Incorporates `OD11-01 = B`, `OD11-02 = B`, `OD11-03 = B`, `D11-02-AMENDMENT-01`, `WP-A`..`WP-E`, and independent Layer D audit `PASS` — see [`stage-11-closeout/CLOSEOUT_REPORT.md`](stage-11-closeout/CLOSEOUT_REPORT.md))
- **Baseline (`origin/main`):** `9cd2344f2076d60ee53568eb35aaa625474dc645` (`PR #11` & `PR #12` **MERGED**)
- **Approved Owner Decisions:** `OD11-01 = B`, `OD11-02 = B`, `OD11-03 = B` (**OWNER_APPROVED**)
- **Governing Authorities:**
  - [`docs/architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md`](../architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md)
  - [`docs/architecture/ADR-001-part-iv-web-mcp-architecture.md`](../architecture/ADR-001-part-iv-web-mcp-architecture.md)
  - [`docs/architecture/ADR-002-part-iv-web-demo-and-mcp-milestone-refinement.md`](../architecture/ADR-002-part-iv-web-demo-and-mcp-milestone-refinement.md)
  - [`docs/stages/stage-10-closeout/STAGE_11_HANDOFF.md`](stage-10-closeout/STAGE_11_HANDOFF.md)

---

## 1. Design Scope & Architectural Objective

Stage 11 delivers the **Local Application Server** and **Operational CLI** as a thin, reliable application adapter around the accepted Stage 10 in-process runtime (`ApplicationComposition` + `ApplicationCoordinator`).

Together with **Stage 12 (Web Demo and Existing RAG Acceptance)**, Stage 11 forms **Milestone 1**: demonstrating and accepting the existing complete Part I/II RAG pipeline through a lightweight same-origin Web Demo without modifying Part I, Part II, or Part III semantics.

### Explicit In-Scope Boundaries

- Long-lived loopback single-runtime server (`atlasrag serve`) using FastAPI + Uvicorn (`workers=1`, `reload=False`).
- Framework-independent application service boundary (`ApplicationServerService`) sitting above `ApplicationCoordinator`, cleanly reusable by both Stage 11/12 HTTP+SSE routes and future Stage 13 MCP adapters.
- Versioned HTTP/SSE transport (`/api/v1/*`) with Pydantic v2 `FrozenModel` transport DTOs and envelopes preserving `UserTurnRequest`, `QueryFilters`, and `FinalResponse` unchanged.
- Process-local bounded `SessionRegistry` preserving `SessionQueryCache` isolation.
- Single authoritative ASGI `receive` disconnect monitoring (`SseStreamResponse` for SSE; post-body `receive` monitor for JSON HTTP) + `QuerySupervisor` cancellation settlement across both JSON HTTP and SSE paths, with actual ASGI `send()` timeouts on SSE frames.
- Authoritative `ApplicationCoordinator`-minted invocation identity (`trace_id`) bound to an isolated request stream channel after real `QueryLease` admission and prior to SSE HTTP `200` header commitment.
- Safe stage/status SSE streaming (`accepted`, `progress`, `final`, `error`) projected from `ProductionObservation` without exposing raw observations, unreviewed tokens, CoT, or private evidence.
- Process-local bounded `MaintenanceOperationRegistry` (control-plane HTTP ledger, never duplicating `InFlightTaskRegistry`) with exception-safe `on_draining` `MaintenanceLease` handoff before HTTP `202` and explicit `SyncOutcome` + `ServingSafety` reporting.
- Local Transport Authorization (loopback bind, Host/Origin validation, browser simple-form CSRF guard, `0o600` Bearer control token written and cleaned up strictly under authoritative `RuntimeOwnershipLock` ownership, request/response size bounds).
- Thin `argparse` + `httpx` operational CLI (`serve`, `ask`, `health`, `status`, `sync`, `recover`, `stop`) with pre-shutdown process-identity capture (`pidfd` + post-open `/proc/<pid>/stat` start time + `boot_id`) and instance-specific clean-shutdown receipt (`shutdown-<server_instance_id>.json`).

### Explicit Out-of-Scope Boundaries (Deferred)

- Web Demo HTML/CSS/JS frontend implementation and bounded real-model/real-KB Web Demo acceptance execution (Stage 12).
- Domain-aware MCP server (`search_domain_docs`, `search_external_evidence`, FastMCP/MCP SDK, zero-trust security metadata/ACLs, Evidence Mode, Evidence Cache, Domain Epoch) (Stage 13).
- Coordinated backup and restore (`§90–91`), full RTX 3070 8GB VRAM/OOM stress and dual-entrypoint validation, real MinerU SDK subprocess settlement, combined Web+MCP concurrency/isolation, and transferred Stage 9 performance runner (`S9-17`, `S9-21`) (Stage 14).
- Multi-tenant RBAC, OAuth, persistent DB conversation history, Celery/Redis task queues, WebSockets, daemon managers, or reverse proxies.

---

## 2. System Architecture Diagram

```text
+-------------------------------------------------------------------+
| Local Clients (WSL2 / Windows Host via Loopback 127.0.0.1)        |
|                                                                   |
|  Stage 12 Web Demo (Browser)          Operational CLI (thin)      |
|  - Same-origin HTML/CSS/JS            - atlasrag ask/health/status|
|  - Fetch + POST SSE                   - atlasrag sync/recover/stop|
|  - Knowledge Plane only               - Knowledge + Control token |
+---------------+-----------------------------------+---------------+
                | HTTP / SSE (127.0.0.1)            | HTTP / SSE (Bearer token for /control/*)
                v                                   v
+-------------------------------------------------------------------+
| FastAPI + Uvicorn ASGI Server (workers=1, reload=False)           |
|  - Host / Origin / CSRF / Body Size Middleware                    |
|  - Control Plane Bearer Auth Dependency (/api/v1/control/*)       |
|  - Single Authoritative ASGI receive/send (SseStreamResponse)     |
|  - Ownership-Safe Lifespan: Lock -> Token -> Bind -> server.json  |
+-------------------------------+-----------------------------------+
                                |
                                v
+-------------------------------------------------------------------+
| ApplicationServerService (Framework-Independent Adapter Boundary) |
|  - QuerySupervisor (timeout + disconnect -> cancel + settlement)  |
|  - SessionRegistry (process-local, bounded, in-flight safe)       |
|  - MaintenanceOperationRegistry (HTTP ledger, not task registry)  |
|  - InvocationEventBus (authoritative trace_id -> StreamChannel)   |
|  - Transport Backpressure Semaphore + Error Translation           |
+-------------------------------+-----------------------------------+
                                |
                                v
+-------------------------------------------------------------------+
| Existing Stage 10 Runtime (SOLE Runtime Owner)                    |
|  ApplicationComposition -> ApplicationCoordinator                 |
|   +-- RuntimeOwnershipLock (~/.local/state/atlasrag/runtime.lock) |
|   +-- RuntimeGate (SERVING / DRAINING / MAINTENANCE / BLOCKED /   |
|   |                SHUTTING_DOWN) & QueryLease / MaintenanceLease |
|   +-- InFlightTaskRegistry (SOLE runtime task registry)           |
|   +-- HealthRegistry (PG, Milvus, Redis, SearXNG, Qwen, BGE)      |
|   +-- MaintenanceCoordinator + CacheFreshnessBarrier              |
|   +-- ResourceGuard (gpu_concurrency)                             |
+-------------------------------+-----------------------------------+
                                |
                                v
+-------------------------------------------------------------------+
| Existing Part I / Part II Services & Infrastructure               |
|  RetrievalRunner (SessionQueryCache) -> RagCoreRunner             |
|  IndexSyncApplication -> PostgreSQL / Milvus / Redis /            |
|  Qwen Embedding / BGE Reranker / DeepSeek / SearXNG               |
+-------------------------------------------------------------------+
```

---

## 3. Inspected Current Runtime Interfaces & Required Minimal Extensions

Every Stage 11 design decision is grounded in the accepted Stage 10 source files on `origin/main` (`9cd2344f`):

### 3.1 Verified Existing Contracts

| Module | Inspected Symbols & Exact Behavior | Stage 11 Impact |
|---|---|---|
| [`src/atlasrag/runtime/composition.py`](../../src/atlasrag/runtime/composition.py) | `ApplicationComposition(config).start(build_business=None) -> ApplicationCoordinator`; acquires `RuntimeOwnershipLock`, retries PG/Milvus/Qwen startup, softly degrades BGE on non-hard errors, initializes `CacheFreshnessBarrier`, `RuntimeGate`, `InFlightTaskRegistry`, `HealthRegistry`, `MaintenanceCoordinator`, runs `coordinator._start()`. Retains `RuntimeOwnershipLock` on `_pool_cleanup_failed` or `_close_resources()` errors. | Sole composition root invoked exclusively inside `atlasrag serve` lifespan startup. |
| [`src/atlasrag/runtime/coordinator.py`](../../src/atlasrag/runtime/coordinator.py) | `ApplicationCoordinator`: `run_query(request: UserTurnRequest) -> QueryResponseEnvelope`, `run_maintenance() -> SyncResult`, `recover() -> SyncResult`, `health() -> HealthSnapshot`, `readiness() -> bool`, `shutdown() -> None`. Uses `_begin_operation(TaskKind)` + `observation_invocation(invocation)` after `acquire_query()`, and releases `ownership` at line 284 after `close_resources()`. | All HTTP/SSE queries, health/status checks, maintenance, recovery, and shutdown flow through this single coordinator. Requires minimal `on_admitted`, `on_draining`, `on_shutdown_settled` hooks and `status(refresh=...)`. |
| [`src/atlasrag/runtime/ownership.py`](../../src/atlasrag/runtime/ownership.py) | `RuntimeOwnershipLock`: fixed `~/.local/state/atlasrag/runtime.lock` (`0o700` dir, `0o600` regular non-symlink file, `O_NOFOLLOW`, `flock(LOCK_EX | LOCK_NB)`), writes `pid=<pid>\n`. | Guarantees single runtime per OS user. Never overridden, bypassed, or touched by competing startup metadata writes. |
| [`src/atlasrag/runtime/gate.py`](../../src/atlasrag/runtime/gate.py) | `RuntimeGate`: atomic mode (`STARTING`, `SERVING`, `DRAINING`, `MAINTENANCE`, `BLOCKED`, `SHUTTING_DOWN`), `lifecycle_generation`, `active_queries`, `acquire_query() -> QueryLease`, `begin_maintenance()`, `begin_recovery()`, `begin_shutdown()`. | Enforces atomic serving vs maintenance mutual exclusion; HTTP routes never duplicate lock/mode state. |
| [`src/atlasrag/runtime/tasks.py`](../../src/atlasrag/runtime/tasks.py) | `InFlightTaskRegistry`: tracks actual `asyncio.Task` objects by `TaskKind` (`QUERY`, `MAINTENANCE`, `HEALTH`, `STARTUP`, `CACHE`), supports `close()`, `wait_empty(timeout)`, `cancel_all()`. | Sole runtime task registry; HTTP/SSE supervisors must await task settlement before releasing outer resources. |
| [`src/atlasrag/runtime/health.py`](../../src/atlasrag/runtime/health.py) | `HealthRegistry`: single-flight `_refresh_lock`, bounded `probe_timeout`, `refresh()`, `refresh_if_stale()`, `readiness()`. Hard dependencies: `postgres`, `milvus`, `embedding` + `_knowledge_verified`. Soft dependencies: `redis`, `searxng`, `reranker`. **Gap:** `query_cache="UNKNOWN"` hardcoded at `health.py:125`. | Used for `/health/ready` (`readiness()`) and `/status`. Requires live `query_cache` callback + manual refresh cooldown. |
| [`src/atlasrag/runtime/cache_freshness.py`](../../src/atlasrag/runtime/cache_freshness.py) | `CacheFreshnessBarrier`: `state` (`TRUSTED` / `BYPASSED`), `active_write_fence`, `enforce()`, `recover_if_due()`. | Authoritative input for live `query_cache` status projection. |
| [`src/atlasrag/runtime/config.py`](../../src/atlasrag/runtime/config.py) & [`src/atlasrag/config/models.py`](../../src/atlasrag/config/models.py) | `load_runtime_config()` loads `ATLASRAG_CONFIG` + `ATLASRAG_CONFIG_JSON` into `RuntimeConfig` (`FrozenModel`) with `_redacted()` on `ValidationError`. Pool contract: `postgres_pool_max_size >= 2 * max_document_concurrency + 2` (default `16`), `postgres_pool_min_size <= postgres_pool_max_size` (default `2`). | Preserved intact; server transport settings live in a separate `ServerConfig` (`FrozenModel`) excluded from both `pipeline_fingerprint` and `runtime_fingerprint`. |
| [`src/atlasrag/domain/requests.py`](../../src/atlasrag/domain/requests.py) | `UserTurnRequest(session_id: StrictStr, query: StrictStr, filters: QueryFilters | None = None)`; `QueryFilters(file_name, source_type, sheet_name)`. Both `FrozenModel` (`extra="forbid"`). | Exact query input contract. No unsupported `top_k`, `web_enabled`, model override, or Milvus filter fields permitted. |
| [`src/atlasrag/domain/results.py`](../../src/atlasrag/domain/results.py) | `FinalResponse(answer_text: StrictStr, citations: tuple[FinalCitation, ...])`; `FinalCitation = FinalLocalCitation | FinalWebCitation`. | Returned unmodified inside `QueryResponse.response`. |
| [`src/atlasrag/domain/observation.py`](../../src/atlasrag/domain/observation.py) | `ObservationStage` (`DENSE`, `BM25`, `FUSION`, `RERANKER`, `EVIDENCE`, `SUFFICIENCY`, `ROUTING`, `ANSWER`), `ProductionObservation`, `ProductionObserver`, `observation_invocation(invocation_id)`. | **Gap:** `build_default_business` leaves `observer=NULL_OBSERVER` on `LocalEvidenceServices` and `RagCoreServices`. |
| [`src/atlasrag/services/cache/session.py`](../../src/atlasrag/services/cache/session.py) & [`src/atlasrag/graphs/retrieval/graph.py`](../../src/atlasrag/graphs/retrieval/graph.py) | `SessionQueryCache`: `lookup(request) -> FinalResponse | None`, `prepare(...)`, `commit(...) -> CacheCommitOutcome` (`STORED`, `BYPASSED`, `UNKNOWN`). `RetrievalRunner.run(request) -> FinalResponse`. | **Gap:** Neither `SessionQueryCache.lookup` nor `RetrievalRunner.run` exposes lookup outcome (`HIT` vs `MISS` vs `BYPASSED`) to `ApplicationCoordinator`. |
| [`src/atlasrag/domain/errors.py`](../../src/atlasrag/domain/errors.py) | `ErrorCode` (`15` codes) and `AtlasRAGError` hierarchy (`safe_message` without secrets/stack traces). | All 15 codes mapped 1:1 to HTTP responses without duplicating meanings. |

### 3.2 Required Minimal Stage 10 Interface Extensions (`OD11-01`, `S11-DESIGN-F03`, `S11-DESIGN-F05`, Pre-Release Hook)

All extensions use backward-compatible defaults so existing Stage 10 callers and tests remain 100% compatible:

1. **Truthful Query Cache Lookup Outcome & Elapsed Timing (`coordinator.py`, `session.py`, `retrieval/graph.py`, `application/retrieval.py`):**
   - Add `QueryCacheStatus(StrEnum)` (`HIT`, `MISS`, `BYPASSED`, `UNKNOWN`).
   - Add `SessionQueryCache.lookup_with_status(request) -> tuple[FinalResponse | None, QueryCacheStatus]` returning the exact lookup outcome (`HIT`, `MISS`, or `BYPASSED`); keep `lookup()` delegating to it.
   - Record `execution_facts["cache_status"]` in `build_retrieval_graph` and expose `RetrievalRunner.run_with_facts(request) -> RetrievalExecution(final_response, cache_status)` while keeping `RetrievalRunner.run(request) -> FinalResponse` intact.
   - Extend `QueryResponseEnvelope`:
     ```python
     @dataclass(frozen=True)
     class QueryResponseEnvelope:
         trace_id: str
         response: FinalResponse
         cache_status: QueryCacheStatus = QueryCacheStatus.UNKNOWN
         elapsed_ms: float | None = None
     ```
   - `ApplicationCoordinator.run_query` records monotonic `elapsed_ms` and actual lookup `cache_status` (defaulting conservatively to `UNKNOWN` when an injected test `retrieval` callable does not report cache facts).
2. **Authoritative Invocation-Admission Hook (`coordinator.py` — `S11-DESIGN-F03`):**
   - Extend `ApplicationCoordinator.run_query`:
     ```python
     async def run_query(
         self,
         request: UserTurnRequest,
         *,
         on_admitted: Callable[[str], None] | None = None,
     ) -> QueryResponseEnvelope:
     ```
   - Exact ordering inside `run_query`:
     1. Health/epoch check (`TaskKind.HEALTH`).
     2. `lease = await self._deps.gate.acquire_query()`.
     3. `current = self._begin_operation(TaskKind.QUERY)`.
     4. `invocation = uuid4().hex` (minted exclusively by `ApplicationCoordinator`).
     5. If `on_admitted is not None`: call `on_admitted(invocation)` **before** entering `with observation_invocation(invocation):` or calling `self._deps.retrieval(request)`. (If `on_admitted` raises, `finally:` releases `lease` and unregisters `current` cleanly.)
3. **Exception-Safe Maintenance Handoff Hook (`coordinator.py`, `maintenance.py` — `S11-DESIGN-F05`):**
   - Add optional `on_draining: Callable[[], None] | None = None` to `ApplicationCoordinator.run_maintenance` / `recover` and `MaintenanceCoordinator.run` / `recover_blocked`, invoked synchronously immediately after `await self._gate.begin_maintenance()` / `begin_recovery()` acquires `MaintenanceLease` (`RuntimeMode.DRAINING`) and inside `try ... except BaseException: await lease.abort(); raise` before `await lease.wait_for_drain(...)`:
     - If `on_draining()` raises for any reason, `await lease.abort()` executes immediately so the `MaintenanceLease` is **never** stranded in `DRAINING`.
4. **Pre-Release Shared Metadata Cleanup Hook (`coordinator.py`, `composition.py`, `D11-02-AMENDMENT-01`):**
   - Add optional `on_shutdown_settled: Callable[[], Awaitable[None] | None] | None = None` on `CoordinatorDependencies` / `ApplicationComposition.start(...)`, invoked inside `ApplicationCoordinator.shutdown()` **strictly after** `await self._deps.close_resources()` succeeds and **strictly before** `self._deps.ownership.release()`.
   - Verifies maintenance settlement and performs instance-verified removal of shared discovery/control metadata (`server.json`, `control.token`) strictly under `RuntimeOwnershipLock`, while never writing `CLEAN_SHUTDOWN` prior to `ownership.release()`. If `RuntimeShutdownFailedError`, `close_resources()`, maintenance settlement, or shared cleanup fails, `ownership.release()` is not executed and no clean receipt is ever published. Under `D11-02-AMENDMENT-01`, the instance-scoped `shutdown-<server_instance_id>.json` (`CLEAN_SHUTDOWN`) commit marker is published only **after** `ownership.release()` and all mandatory lifecycle checks succeed.
5. **Per-Invocation `ProductionObserver` Routing (`production.py`, `coordinator.py`, `composition.py`):**
   - Wire an invocation-scoped `ProductionObserver` multiplexer (`InvocationObserverMux`, dispatching strictly by `event.invocation_id or current_invocation_id()`) into `LocalEvidenceServices.observer` and `RagCoreServices.observer` in `build_default_business`, accepted via optional `observer: ProductionObserver | None = None` on `ApplicationComposition.start(...)`.
6. **Truthful `query_cache` Health Projection & Bounded Status Read (`health.py`, `coordinator.py`):**
   - Add optional `query_cache_status: Callable[[DependencyHealth], str] | None = None` on `HealthRegistry` so `HealthSnapshot.query_cache` reflects real `CacheFreshnessBarrier.state` + `config.cache.query_cache_enabled` + current snapshot's `redis` health (`"ACTIVE"` only when `enabled and barrier.state is TRUSTED and redis is HEALTHY`; `"DISABLED"` when disabled; `"BYPASSED"` otherwise).
   - Add `ApplicationCoordinator.status(self, *, refresh: bool = False) -> HealthSnapshot` using `refresh_if_stale()` when `refresh=False` and `refresh()` (with server-side manual cooldown) when `refresh=True`.

---

## 4. Design Decisions (`D11-01` through `D11-12`)

### D11-01 — Technology Stack and Package Structure

#### Recommended Dependencies & Lockfile Compatibility

Inspection of `pyproject.toml` and `uv.lock` confirms:

- Repository Python constraint: `requires-python = ">=3.11,<3.12"` (`.venv`: `Python 3.11.16`).
- Locked core libraries in `uv.lock`: `pydantic==2.13.5`, `pydantic-core==2.46.5`, `httpx==0.28.1`, `anyio==4.14.2`, `langgraph==1.2.15`.
- Transitive/optional-group versions **already resolved in `uv.lock`** (via `parsing-mineru` group): `fastapi==0.143.0` (`starlette==1.7.0`) and `uvicorn==0.54.0` (`click`, `h11==0.16.0`).

**Recommended `[project.dependencies]` additions (Stage 11 implementation phase only; unchanged now):**

| Package | Recommended Constraint | Locked `uv.lock` Version | Compatibility Rationale |
|---|---|---|---|
| `fastapi` | `>=0.143.0,<0.144.0` | `0.143.0` | Matches existing `uv.lock` resolution; full Pydantic v2 (`2.13.5`) and Starlette `1.7.0` / AnyIO `4.14.2` compatibility; zero conflict with `mineru` group. |
| `uvicorn` | `>=0.54.0,<0.55.0` | `0.54.0` | Plain ASGI server without `[standard]` extras (`watchfiles` / `websockets`), aligning with `reload=False` and no WebSockets. |

And `[project.scripts]` will declare `atlasrag = "atlasrag.cli.main:main"`.

#### Minimal Package Structure

```text
src/atlasrag/
├── server/
│   ├── __init__.py
│   ├── config.py        # ServerConfig (FrozenModel) & loader (excluded from fingerprints)
│   ├── schemas.py       # Pydantic v2 FrozenModel HTTP/SSE/Operation/Error DTOs
│   ├── errors.py        # ErrorCode + transport error -> (HTTP status, code, safe_message)
│   ├── auth.py          # Host allowlist, Origin/CSRF guard, O_NOFOLLOW 0o600 Bearer token
│   ├── sessions.py      # Bounded process-local SessionRegistry (in-flight refcounted)
│   ├── events.py        # InvocationEventBus + StreamChannel (strict SSE allowlist)
│   ├── operations.py    # Bounded process-local MaintenanceOperationRegistry (HTTP ledger only)
│   ├── service.py       # Framework-independent ApplicationServerService + QuerySupervisor
│   ├── app.py           # Thin FastAPI app factory, lifespan, middleware, SseStreamResponse, routes
│   └── launcher.py      # Ownership-safe Uvicorn launcher, pre-release cleanup, pidfd identity, exit codes
└── cli/
    ├── __init__.py
    └── main.py          # argparse + httpx thin CLI (serve, ask, health, status, sync, recover, stop)
```

**Boundary enforcement:** `scripts/check_import_boundaries.py` will forbid `service.py`, `sessions.py`, `events.py`, `operations.py`, `schemas.py`, `errors.py`, and `cli/main.py` from importing `fastapi`, `starlette`, or `uvicorn` (only `app.py`, `auth.py` if middleware, and `launcher.py` touch FastAPI/Uvicorn).

---

### D11-02 — Single Runtime Ownership, Ownership-Safe Metadata, and Bounded Shutdown (`S11-DESIGN-F01`, `S11-DESIGN-F04`, Final Closure)

`atlasrag serve` is the **sole production runtime-owning entrypoint**:

- `uvicorn.Config(..., host=server_cfg.host, port=server_cfg.port, workers=1, reload=False, timeout_graceful_shutdown=runtime_cfg.maintenance.shutdown_timeout_seconds)`.
- `workers = 1`, `reload = False`, `ApplicationComposition = 1`, `ApplicationCoordinator = 1`.

#### Ownership-Safe Startup & Metadata Replacement Invariants

1. **Zero pre-lock state mutation:** Before `RuntimeOwnershipLock.acquire()` succeeds inside `ApplicationComposition.start()`, the process **must not** create, overwrite, rotate, or remove `control.token`, `server.json`, `shutdown-*.json`, or any shared server state file. Any shutdown signal received prior to acquiring ownership aborts startup without mutating shared metadata.
2. **Failed competing startup:** A second `atlasrag serve` hits `RuntimeOwnershipConflictError` inside `ApplicationComposition.start()` and exits `3` without altering the running server's `control.token`, `server.json`, `runtime.lock`, or resources.
3. **Stale recovery, socket bind & metadata ordering:** Once `RuntimeOwnershipLock.acquire()` succeeds, the new process is the sole runtime owner: any stale `server.json` left by an abnormally exited prior runtime is safely cleared/replaced under verified directory and `O_NOFOLLOW` regular-file rules, `control.token` is published under authoritative ownership, and new `server.json` is written **only after** both `lifespan` startup and Uvicorn TCP socket binding succeed:

```text
1. Pre-Lock Config Load & Validation
   └── load_runtime_config() + load_server_config() -> invalid: exit 2 (disk untouched)

2. Uvicorn Server.startup() -> Step A: FastAPI Lifespan Startup
   ├── coordinator = await ApplicationComposition(runtime_config).start(
   │       observer=event_bus,
   │       on_shutdown_settled=owned_shared_cleanup,
   │   )
   │    ├── RuntimeOwnershipLock.acquire()
   │    │    └── RuntimeOwnershipConflictError -> abort -> exit 3 (shared files untouched)
   │    ├── Open PG/Milvus/Redis/HTTP/Qwen(+BGE), reconcile, source_safety, verify
   │    │    ├── Hard failure -> cleanup + release lock (unless cleanup failed) -> exit 4 (shared files untouched)
   │    │    └── Unsafe KB -> RuntimeMode.BLOCKED (alive, not ready) | Safe -> SERVING
   └── NOW (authoritative lock owner — safe recovery of stale files from dead processes):
        ├── Generate server_instance_id = uuid4().hex, control_token = secrets.token_hex(32)
        ├── Safely clear any stale server.json left by a dead runtime (O_NOFOLLOW/UID/regular verified)
        ├── Prune stale shutdown-*.json receipts beyond latest 8 (under lock ownership)
        └── Atomically write ~/.local/state/atlasrag/control.token (0o700 dir, temp+replace, 0o600, O_NOFOLLOW)

3. Uvicorn Server.startup() -> Step B: TCP Socket Bind (127.0.0.1:<port>)
   ├── Bind fails (e.g., EADDRINUSE) or signal during startup:
   │    ├── Uvicorn runs lifespan shutdown -> coordinator.shutdown()
   │    ├── Pre-release on_shutdown_settled cleans instance control.token & any discovery (under lock!)
   │    ├── server.json is absent, ownership released cleanly (unless runtime unsettled) -> exit 4 (no receipt)
   └── Bind succeeds (socket listening, actual bound port captured):
        ├── Read /proc/self/stat starttime (field 22) & /proc/sys/kernel/random/boot_id
        └── Atomically write ~/.local/state/atlasrag/server.json (0o600, O_NOFOLLOW)
            {url, host, port, pid, process_start_ticks, boot_id, server_instance_id, started_at}
```

#### `D11-02-AMENDMENT-01` — Instance-Scoped Post-Release Clean Receipt & Bounded Shutdown Timeline

- **Previous protocol & compound failure limitation:** The original WP-D design wrote `shutdown-<server_instance_id>.json` (`status="CLEAN_SHUTDOWN"`) inside `on_shutdown_settled()` *before* `RuntimeOwnershipLock.release()`. Because `shutdown-<server_instance_id>.json` existed on disk before `ownership.release()`, two compound failures could not be resolved fail-closed:
  1. `ownership.release()` failing *after* actually dropping the kernel `flock` (for instance `flock(LOCK_UN)` or `os.close(fd)` error after OS descriptor/lock release), where post-release state-directory deletion is forbidden (`S11-01`).
  2. `ownership.release()` failure followed by receipt-revocation (`unlink`) failure.
  In both cases `ServerLauncher` exited `4` while a schema-valid `"CLEAN_SHUTDOWN"` receipt remained on disk, causing `atlasrag stop` (which observes non-child exit via `pidfd` without POSIX exit code) to verify the unrevoked receipt and exit `0`.
- **Approved replacement protocol (`D11-02-AMENDMENT-01`):**
  1. All **shared** metadata cleanup (`server.json`, `control.token`), runtime task settlement, maintenance settlement, and `close_resources()` run strictly **under `RuntimeOwnershipLock`** inside `coordinator.shutdown()` (`on_shutdown_settled`) and **must never** emit `"CLEAN_SHUTDOWN"` before `ownership.release()`.
  2. `RuntimeOwnershipLock.release()` tracks actual OS descriptor/release state without inferring kernel lock ownership from `_shutdown_done == False`, `_fd is None` before close, raised exceptions alone, or lock-file existence, and never blindly retries `close(fd)` on a closed/reused descriptor number.
  3. Only after `coordinator.shutdown()`, verified `ownership.release()`, and all mandatory lifecycle checks succeed does `ServerLauncher` atomically publish this instance's **`shutdown-<server_instance_id>.json`** (`status="CLEAN_SHUTDOWN"`, integer `pid`) as the final commit marker before exiting `0`.
- **Narrow post-release write exception & preserved ownership guarantees:** Only atomic temp-file creation + `os.replace` for this instance's own UUID-keyed `shutdown-<server_instance_id>.json` is permitted after `ownership.release()`. The old launcher must never create, overwrite, or remove `server.json`, `control.token`, `runtime.lock`, or another instance's receipts, must never prune receipt history (`prune_shutdown_receipts` remains lock-protected at startup), must never delete/replace `state_dir`, and must guard delayed signal/shutdown callbacks with `_cleanup_closed` so they cannot mutate shared metadata after release. If receipt publication fails or the process exits after `ownership.release()` before publication completes, no valid `"CLEAN_SHUTDOWN"` receipt exists and `stop` fails closed with exit `4`.

```text
Shutdown Trigger (SIGINT / SIGTERM / POST /api/v1/control/shutdown)
  │
  ├─ T=0 (Prompt Admission Closure & Discovery Removal — under RuntimeOwnershipLock):
  │   ├── await coordinator.gate.begin_shutdown()  (all new /query, /query/stream, /control/sync|recover -> 503)
  │   ├── Instance-owned unlink of server.json (only if _cleanup_closed is False and server_instance_id matches)
  │   └── Set uvicorn.Server.should_exit = True
  │
  ├─ T=0 .. T_http (Bounded Uvicorn Connection Drain):
  │   ├── Uvicorn stops accepting connections & drains active HTTP/SSE tasks up to:
  │   │     timeout_graceful_shutdown = runtime_cfg.maintenance.shutdown_timeout_seconds
  │   └── At expiry, Uvicorn cancels remaining ASGI tasks -> QuerySupervisor cancels query_task
  │       and awaits task settlement before returning from ASGI handlers
  │
  ├─ T_http .. T_coord (Authoritative Runtime Settlement & Shared Metadata Cleanup — Sole Owner):
  │   └── FastAPI lifespan teardown invokes await coordinator.shutdown() (ONCE):
  │        ├── tasks.close() -> await tasks.wait_empty(timeout=shutdown_timeout_seconds)
  │        ├── If unsettled: await begin_guard_shutdown() -> tasks.cancel_all()
  │        │     -> await asyncio.wait(remaining, timeout=shutdown_timeout_seconds)
  │        ├── If still pending -> raises RuntimeShutdownFailedError:
  │        │     ├── on_shutdown_settled NOT called; lock RETAINED while alive; NO receipt; exit 4
  │        ├── close_resources() (if error -> raises, lock RETAINED, NO receipt, exit 4)
  │        ├── on_shutdown_settled() runs STILL HOLDING RuntimeOwnershipLock:
  │        │     ├── Verify service.operations.wait_active(...) (if unsettled -> raises, NO release, NO receipt, exit 4)
  │        │     ├── Instance-verified unlink of server.json (if still present)
  │        │     ├── Instance-verified unlink of control.token
  │        │     └── Mark _cleanup_closed = True (DO NOT write CLEAN_SHUTDOWN yet!)
  │        ├── ownership.release() (any pre-unlock, post-unlock, or ambiguous close failure -> exit 4, NO receipt!)
  │        └── Mark coordinator._shutdown_done = True
  │
  └─ Final Post-Release Instance Commit (only if all mandatory checks & release succeeded cleanly):
      ├── Atomically write shutdown-<server_instance_id>.json (write_secure_local_file, integer pid)
      │     {"server_instance_id": ..., "pid": <int>, "status": "CLEAN_SHUTDOWN", "completed_at": ...}
      ├── If receipt write fails -> NO valid receipt on disk -> atlasrag serve exits 4
      └── Otherwise -> atlasrag serve exits code 0
```

#### Unresolved Worker, Live Process State, and Clean-Shutdown Distinction

- **Five explicitly distinguished outcomes:**
  1. **Shutdown accepted:** `POST /control/shutdown` returned `202`, `gate.begin_shutdown()` closed admission.
  2. **Clean runtime shutdown (exit `0`):** Original `pidfd`/process exited **AND** `shutdown-<server_instance_id>.json` exists with matching `server_instance_id`, integer `pid`, and `status == "CLEAN_SHUTDOWN"`. A `CLEAN_SHUTDOWN` receipt attests that runtime settlement, resource cleanup, shared metadata cleanup, and `ownership.release()` succeeded (it does not independently report a non-child process's POSIX exit code via `pidfd`).
  3. **Runtime / release / receipt failure (exit `4`):** `RuntimeShutdownFailedError`, `close_resources()` failure, shared cleanup failure, `ownership.release()` failure (whether lock remained held, released prior to error, or descriptor state became ambiguous), or post-release receipt write failure -> **no** valid clean-shutdown receipt on disk, `ServerLauncher` exits `4`. When pre-release settlement/cleanup failed, `RuntimeOwnershipLock` stays held while the OS process persists.
  4. **Forced process termination (exit `4`):** `atlasrag stop --force` escalated to `SIGTERM`/`SIGKILL` via retained `pidfd` after timeout -> OS kernel reaps process and releases `flock` fd; `stop` exits `4` (never `0`).
  5. **Unverified process exit (exit `4`):** Original `pidfd`/PID disappears (crash, OOM kill, external `kill -9`, or exit after `release()` before receipt publication) **without** a valid `shutdown-<server_instance_id>.json` receipt (or `stop` lacked `pidfd` verification). `stop` exits `4` (never `0`).
- **Required E0-02 negative tests:** Must cover normal post-release receipt timing, resource/maintenance/metadata failures yielding no receipt, release failing before unlock, release failing after actual unlock, ambiguous unlock/close state, receipt publication failure post-release, exit between `release()` and receipt write, immediate new-server startup during the old receipt publication window without touching new-server metadata, delayed signal callbacks blocked from shared mutation, and `stop` returning `4` vs `0`.
- **Subsequent startup recovery:** After any failed shutdown, forced termination, or unverified exit, the next `atlasrag serve` acquires `RuntimeOwnershipLock` safely (clearing stale `server.json` & replacing `control.token` only after lock acquisition) and executes `StartupReconciliation` (`§80`).

---

### D11-03 — Versioned Minimal HTTP API Contract (`/api/v1/*`)

All 11 endpoints are necessary and non-redundant:

| Method & Path | Plane / Auth | Request Schema | Response Schema (2xx) | Status Codes & Error Codes |
|---|---|---|---|---|
| `POST /api/v1/sessions` | Knowledge (`Host` + `Origin` + `X-AtlasRAG-Client`) | `CreateSessionRequest` (`client_kind`: `"web" \| "cli"`, default `"web"`) | `SessionResponse` (`session_id`, `client_kind`, `created_at`, `expires_at`) | `201`, `400/422 INVALID_REQUEST`, `403 FORBIDDEN`, `413 PAYLOAD_TOO_LARGE`, `503 RESOURCE_EXHAUSTED` / `RUNTIME_SHUTTING_DOWN` |
| `DELETE /api/v1/sessions/{session_id}` | Knowledge (`Host` + `Origin` + `X-AtlasRAG-Client`) | Path `session_id` | `204 No Content` | `204`, `403 FORBIDDEN`, `404 SESSION_NOT_FOUND`, `503 RUNTIME_SHUTTING_DOWN` |
| `POST /api/v1/query` | Knowledge (`Host` + `Origin` + `X-AtlasRAG-Client`) | `QueryRequest` (`session_id`, `query`, `filters: QueryFilters \| None`) | `QueryResponse` (`trace_id`, `session_id`, `cache_status`, `elapsed_ms`, `response: FinalResponse`) | `200`, `400/422`, `403`, `404 SESSION_NOT_FOUND`, `413`, `500`, `502`, `503`, `504` |
| `POST /api/v1/query/stream` | Knowledge (`Host` + `Origin` + `X-AtlasRAG-Client`) | `QueryRequest` (`session_id`, `query`, `filters: QueryFilters \| None`) | `text/event-stream` (`accepted`, `progress`, `final`, `error`) | Pre-admission: `400/422`, `403`, `404`, `413`, `503` JSON. Post-admission (`200` SSE): terminal `error` event |
| `GET /api/v1/health/live` | Public/Host (`Host` check) | None | `LivenessResponse` (`status: "ALIVE" \| "SHUTTING_DOWN"`, `mode: RuntimeMode`) | `200`, `403 FORBIDDEN` |
| `GET /api/v1/health/ready` | Public/Host (`Host` check) | None | `ReadinessResponse` (`ready: bool`, `mode: RuntimeMode`) | `200` (ready), `503` (not ready), `403 FORBIDDEN` |
| `GET /api/v1/status` | Public/Host (`Host` check) | Query `refresh: bool = False` | `RuntimeStatusResponse` (`mode`, `ready`, `knowledge_verified`, `active_queries`, `dependencies`, `query_cache`, `pipeline_fingerprint`, `runtime_fingerprint`, `checked_at`) | `200`, `403 FORBIDDEN`, `503 RUNTIME_SHUTTING_DOWN` |
| `POST /api/v1/control/sync` | Control (`Bearer` + `Host` + `Origin`) | Empty / `{}` (`extra="forbid"`) | `OperationAcceptedResponse` (`operation_id`, `kind: "SYNC"`, `status: "RUNNING"`, `accepted_at`) | `202`, `401 UNAUTHORIZED`, `403 FORBIDDEN`, `409 MAINTENANCE_BUSY`, `503 RUNTIME_NOT_READY` / `RUNTIME_SHUTTING_DOWN` |
| `POST /api/v1/control/recover` | Control (`Bearer` + `Host` + `Origin`) | Empty / `{}` (`extra="forbid"`) | `OperationAcceptedResponse` (`operation_id`, `kind: "RECOVER"`, `status: "RUNNING"`, `accepted_at`) | `202`, `401 UNAUTHORIZED`, `403 FORBIDDEN`, `409 MAINTENANCE_BUSY`, `503 RUNTIME_NOT_READY` / `RUNTIME_SHUTTING_DOWN` |
| `GET /api/v1/control/operations/{operation_id}` | Control (`Bearer` + `Host`) | Path `operation_id` | `OperationStatusResponse` (`operation_id`, `kind`, `status`, `sync_outcome`, `serving_safety`, `runtime_mode`, `unsafe_reasons`, `blocked_code`, `summary`, `started_at`, `finished_at`, `error_code`, `message`) | `200`, `401 UNAUTHORIZED`, `403 FORBIDDEN`, `404 OPERATION_NOT_FOUND` |
| `POST /api/v1/control/shutdown` | Control (`Bearer` + `Host` + `Origin`) | Empty / `{}` (`extra="forbid"`) | `ShutdownAcceptedResponse` (`status: "SHUTTING_DOWN"`, `pid: int`, `server_instance_id: str`) | `202`, `401 UNAUTHORIZED`, `403 FORBIDDEN` |

#### Request & Response Bounds (No Truncation of `FinalResponse`)

- `QueryRequest` (`FrozenModel`, `extra="forbid"`): `session_id` (1..128 chars, validated against `SessionRegistry`), `query` (non-blank Unicode scalar, `<= 8192` chars), `filters: QueryFilters | None = None` (`file_name`, `source_type`, `sheet_name` — exact `atlasrag.domain.requests.QueryFilters`). Unsupported options (`top_k`, `web_enabled`, `model`, `milvus_expr`) fail `422 INVALID_REQUEST`.
- Max request body: `65_536` bytes (`64 KiB`) enforced before JSON parsing (`413 PAYLOAD_TOO_LARGE`).
- Max serialized response size: `ServerConfig.max_response_bytes = 4_194_304` (`4 MiB`). If a serialized `QueryResponse` exceeds `max_response_bytes`, return a mapped safe error (`500 INVARIANT_VIOLATION`) — **never** truncate `FinalResponse` into invalid JSON or broken citations.
- Default query execution timeout: `ServerConfig.query_timeout_seconds = 120.0` (configurable `1.0..600.0`).

---

### D11-04 — Minimal Process-Local Session Management (`OD11-03`: Mandatory Server-Issued Sessions)

- **Storage:** Process-local `SessionRegistry` (`dict[str, _SessionEntry]` under `asyncio.Lock`). Zero PostgreSQL session/conversation tables; zero history injection into Query Construction.
- **Identifier format:** `f"{client_kind}_{secrets.token_hex(16)}"` (`web_...` or `cli_...`; Stage 13 reserves `mcp_...`). Strictly satisfies `is_cache_key_component_safe()` and prevents cross-entrypoint collisions.
- **Mandatory server-issued session (`OD11-03`):** Both `POST /api/v1/query` and `POST /api/v1/query/stream` require an active server-issued `session_id` from `POST /api/v1/sessions` (unknown/expired/closed -> `404 SESSION_NOT_FOUND`).
- **Bounds & in-flight safety:** `max_sessions = 256` (`1..4096`), `session_ttl_seconds = 3600.0`, `max_in_flight_per_session = 4`. Idle expired sessions with `in_flight == 0` are pruned on `create_session()`; if full, returns `503 RESOURCE_EXHAUSTED`. Deleting or expiring a session while `in_flight > 0` sets `closed = True` (rejecting new requests `404`) and waits for `QuerySupervisor` settlement to decrement `in_flight` to `0` before purging the entry.
- **No Redis scan/delete on session close:** `DELETE /api/v1/sessions/{id}` never deletes Redis keys; `query_ttl_seconds` and `CacheFreshnessBarrier` remain authoritative.
- **Web & CLI retention:** Browser keeps `session_id` in JS memory / `sessionStorage`. `atlasrag ask` stores `{"session_id", "server_instance_id"}` in `~/.local/state/atlasrag/cli_session.json` (`0o600`, `O_NOFOLLOW`) so repeated `atlasrag ask "same question"` hits the Session Query Cache (`--new-session` rotates, `--session-id` overrides without overwriting, server restart auto-renews when `--session-id` is not explicitly pinned).

---

### D11-05 — Existing Redis Cache Integration & Cache Metadata Semantics

- Preserves exact key `atlasrag:query:{session_id}:{runtime_fingerprint}:{request_hash}`, raw `request_hash`, `CacheFreshnessBarrier`, Lua `QUERY_WRITE_FENCE_KEY` fencing, `ParentContextService` (`TEXT_PARENT` only, canonical `TABLE`), Redis soft degradation, and fresh `trace_id` per invocation (including hits).
- **Explicit `cache_status` definition (Lookup Outcome Only):**
  - `HIT`: eligible, validated cached `FinalResponse` was returned from Redis Query Cache (skipping `rag_core`).
  - `MISS`: a trusted Query Cache lookup was performed against Redis and no reusable entry was found. **`MISS` strictly reports lookup outcome and does NOT imply that a subsequent `cache_write` stored an entry** (e.g., `web_invoked=True`, `SAFE_FAILURE`, `DEGRADED_SUCCESS`, `NO_CITATIONS`, entry size overflow, or transport-ambiguous `CacheCommitOutcome.UNKNOWN` are preserved unchanged).
  - `BYPASSED`: lookup was intentionally bypassed (cache disabled, unsafe key component, time-sensitive query, untrusted PG marker, missing/mismatched write fence) or Redis was unavailable/errored.
  - `UNKNOWN`: reliable lookup facts were unavailable (e.g., plain fake retrieval callable in tests).
- Never infer `cache_status` from latency; never expose internal Redis keys, fence tokens, or DSNs.

---

### D11-06 — Minimal Safe SSE, Single Receive Consumer & Actual Send Timeout (`S11-DESIGN-F03`, Guardrails)

#### Authoritative Admission, Header Commit & Custom `SseStreamResponse`

- **Single authoritative ASGI `receive` consumer & actual ASGI `send()` timeout:**
  Starlette's built-in `StreamingResponse` spawns its own internal `listen_for_disconnect(receive)` task, which would collide with a route-level `receive()` monitor (violating ASGI's single-consumer rule) and does not wrap `send()` with a per-frame timeout. Therefore Stage 11 uses a minimal custom `SseStreamResponse(Response)` (in `src/atlasrag/server/app.py`, overriding `__call__(self, scope, receive, send)`) that:
  1. Starts **exactly one** `receive` disconnect monitor (`http.disconnect` -> `disconnect_event.set()`).
  2. Sends HTTP `200 text/event-stream` response headers only **after** `accepted_event` is already set.
  3. Wraps every actual `await send({"type": "http.response.body", "body": frame, "more_body": True})` call in `asyncio.wait_for(..., timeout=server_cfg.sse_send_timeout_seconds)` (default `5.0s`)—so an indefinitely stalled TCP socket times out at the actual ASGI `send()` boundary rather than hanging on backpressure, triggers `disconnect_event.set()` -> `query_task.cancel()` -> `QuerySupervisor` settlement, and cleans up!

```text
POST /api/v1/query/stream
  1. Validate Host/Origin/CSRF/body size/QueryRequest  -> fail: 403/413/422 JSON
  2. SessionRegistry.acquire(session_id)               -> fail: 404/503 JSON
  3. Transport semaphore acquire                       -> fail: 503 JSON
  4. Allocate isolated StreamChannel(progress_queue, terminal_event, accepted_event)
  5. Spawn single pre-admission DisconnectMonitor + query_task = create_task(
       coordinator.run_query(req, on_admitted=bind_and_accept)
     ) supervised by QuerySupervisor
  6. Await first_completed(accepted_event.wait(), query_task, disconnect_event.wait()):
     ├── Pre-admission failure (RuntimeNotReadyError, RuntimeShuttingDownError, probe error):
     │     -> settle & release resources -> return HTTP 503/504/500 JSON (NO 200 SSE headers!)
     ├── Client disconnected pre-admission:
     │     -> cancel query_task, await settlement, release resources -> return 499/empty
     └── accepted_event SET (QueryLease held, TaskKind.QUERY registered, trace_id bound):
           -> return SseStreamResponse(handing off the same single DisconnectMonitor!)
              ├─ Frame 1: event: accepted {"trace_id": ..., "session_id": ...}
              ├─ Frame 2..N: event: progress {...} & `: heartbeat` (via bounded-timeout send)
              └─ Terminal frame: event: final {QueryResponse} OR event: error {safe error}
```

#### Public Event Allowlist & Buffer Limits

| SSE `event:` | JSON `data:` Payload | Bounds & Rules |
|---|---|---|
| `accepted` | `{"trace_id": "<hex>", "session_id": "<id>"}` | Always first frame after `200` headers commit. |
| `progress` | `{"trace_id": "<hex>", "stage": "<stage>", "status": "<status>", "attempt": 1, "degraded": false}` | Projected strictly from allowlisted `ProductionObservation` fields; max `1_024` bytes (`max_progress_event_bytes`); zero `candidates`, `selected_identities`, chunk text, CoT, or prompts. |
| `final` | `QueryResponse` JSON (`trace_id`, `session_id`, `cache_status`, `elapsed_ms`, `response`) | Emitted only after `ApplicationCoordinator.run_query` returns reviewed/cached `FinalResponse`; never truncated. |
| `error` | `{"trace_id": "<hex_or_null>", "error_code": "<ErrorCode>", "message": "<safe_message>"}` | Emitted for post-admission runtime/timeout errors; zero stack traces or secrets. |

- **Buffer & slow-consumer rules:**
  - `progress_queue = asyncio.Queue(maxsize=64)`. Observer callback uses strictly non-blocking `put_nowait`, dropping oldest `progress` item on overflow so RAG execution never blocks.
  - Dedicated `terminal_event` slot guarantees terminal `final`/`error` retention inside the **application buffer**, **without** implying delivery over an already-disconnected or indefinitely stalled TCP socket.
  - Cache-hit SSE: emits `accepted` -> (optional `CACHE`/`HIT` if instrumented) -> `final` with `cache_status="HIT"`, zero fabricated retrieval/answer stage events.
  - Heartbeat `: heartbeat\n\n` every `sse_heartbeat_seconds = 15.0`.

---

### D11-07 — Explicit Client Disconnect & Query Supervision (`S11-DESIGN-F02`, Guardrails)

Uvicorn does not automatically cancel an `async def` HTTP route handler on TCP disconnect. Therefore **both** `POST /api/v1/query` and `POST /api/v1/query/stream` share `DisconnectMonitor` + `QuerySupervisor`:

1. **Single Authoritative `DisconnectMonitor` (`app.py` -> `DisconnectSignal` in `service.py`):**
   - For JSON `POST /api/v1/query`: after FastAPI parses the body, `app.py` runs one task reading `await request.receive()` until `{"type": "http.disconnect"}` and sets `disconnect_event.set()`.
   - For SSE `POST /api/v1/query/stream`: one shared `receive()` watcher runs pre-admission and continues inside `SseStreamResponse.__call__` (never running Starlette's `StreamingResponse.listen_for_disconnect`), plus ASGI `send()` timeout/error also triggers `disconnect_event.set()`.
2. **`QuerySupervisor` (`service.py`):**
   - Owns `query_task = asyncio.create_task(coordinator.run_query(user_turn, on_admitted=...))` inside bounded transport admission (`max_concurrent_queries = 16`, `max_concurrent_streams = 16`).
   - Races `query_task` against `disconnect_event.wait()` and `query_timeout_seconds` deadline, handling concurrent races across disconnect, request timeout, and coordinator shutdown cancellation.
   - Leaves server-shutdown drain vs cancellation to `InFlightTaskRegistry` (`coordinator.shutdown()`).
   - **Idempotent cancellation & repeated outer cancellation settlement:**
     ```python
     while not query_task.done():
         try:
             await asyncio.shield(query_task)
         except asyncio.CancelledError:
             outer_cancelled = True
             if not cancel_requested:
                 cancel_requested = True
                 query_task.cancel()
             continue
         except BaseException:
             break
     ```
   - **Cleanup strictly post-settlement:** In `finally:`, cancel and await `monitor_task` (suppressing `CancelledError`), unregister `InvocationEventBus` channel, decrement `SessionRegistry.in_flight`, and release transport concurrency semaphore **only after** `query_task.done()` is `True`.
   - **Unresolved workers:** If an underlying worker refuses cancellation during shutdown, `query_task` remains registered in `InFlightTaskRegistry`, `coordinator.shutdown()` raises `RuntimeShutdownFailedError`, `RuntimeOwnershipLock` stays held, and no clean-shutdown receipt is written.

---

### D11-08 — Maintenance Operation Ownership & Acceptance (`S11-DESIGN-F05`, Guardrails)

- **No duplicate task registry:** `InFlightTaskRegistry` (`TaskKind.MAINTENANCE`) remains the **only** runtime task registry. `MaintenanceOperationRegistry` is strictly a bounded HTTP control-plane ledger mapping `operation_id` (`op_<24-hex>`) to the active or recent terminal operation (`max_retained_operations = 32`, FIFO eviction).
- **Atomic reservation & exception-safe `MaintenanceLease` handoff before `202`:**
  1. Under `MaintenanceOperationRegistry._lock`, verify no active operation exists (`409 MAINTENANCE_BUSY`) and pre-check `coordinator.gate.mode` (`SERVING` for `sync`, `BLOCKED` for `recover`, else `503 RUNTIME_NOT_READY` / `RUNTIME_SHUTTING_DOWN` or `409 MAINTENANCE_BUSY`).
  2. Reserve `OperationRecord` atomically (`status="STARTING"`) and spawn server-owned `asyncio.Task` calling `coordinator.run_maintenance(on_draining=...)` or `coordinator.recover(on_draining=...)`.
  3. **Exception-safe `on_draining` in `MaintenanceCoordinator`:** If `on_draining()` fails or client/shutdown cancels between reservation and `DRAINING` handoff, `MaintenanceCoordinator` catches `BaseException` and calls `await lease.abort()` so `MaintenanceLease` is **never** stranded in `DRAINING`; reservation rolls back and HTTP returns `503`/`500` (never a bogus `202`).
  4. Once `on_draining` succeeds (`TaskKind.MAINTENANCE` registered + `RuntimeGate` in `DRAINING`), transition `OperationRecord` to `RUNNING` and return `202 Accepted`.
- **Disconnect resilience & terminal states:** HTTP/CLI client disconnect after `202` never cancels `op_task`. `OperationStatusResponse` reports **both** `sync_outcome` (`SyncOutcome`) and `serving_safety` (`ServingSafety`) plus `runtime_mode`, `unsafe_reasons`, and `blocked_code` — never inferring a safe runtime from `SUCCESS` or `PARTIAL_SUCCESS`.
- **Backup/Restore:** Deferred to Stage 14 (`§90–91`).

---

### D11-09 — Health, Status, and Complete Error Translation

- `/api/v1/health/live`: Event loop + `gate.mode` (`200` in `SERVING`, `BLOCKED`, `DRAINING`, `MAINTENANCE`, `SHUTTING_DOWN`). Zero DB/Milvus/Redis/SearXNG/DeepSeek calls.
- `/api/v1/health/ready`: `await coordinator.readiness()` (`200` iff `SERVING` and `hard_healthy`; else `503`). Soft outage (`redis`, `searxng`, `reranker`) preserves `200`.
- `/api/v1/status`:
  - Default `?refresh=false` uses `HealthRegistry.refresh_if_stale()` (`health_refresh_interval_seconds`).
  - Explicit `?refresh=true` shares `HealthRegistry._refresh_lock` (single-flight) and enforces a server-side manual refresh cooldown (`min_manual_refresh_interval_seconds = 1.0s`).
  - Stale snapshots cannot claim readiness (`lifecycle_generation` check) or active Query Cache (`query_cache` reads live `CacheFreshnessBarrier.state` + `redis` health).

#### Complete Error Translation Table

Reuses all 15 `ErrorCode` values (`src/atlasrag/domain/errors.py`) plus minimal non-duplicative transport codes without exposing stack traces, DSNs, secrets, or private paths:

| Error Code | Source | HTTP Status | Safe Public Message Policy |
|---|---|---|---|
| `RUNTIME_NOT_READY` | `RuntimeNotReadyError` | `503` | Fixed `safe_message` |
| `RUNTIME_SHUTTING_DOWN` | `RuntimeShuttingDownError` | `503` | Fixed `safe_message` |
| `MAINTENANCE_BUSY` | `MaintenanceBusyError` | `409` | Fixed `safe_message` |
| `MAINTENANCE_DRAIN_TIMEOUT` | `MaintenanceDrainTimeoutError` | `409` | Fixed `safe_message` (or operation status) |
| `RUNTIME_OWNERSHIP_CONFLICT` | `RuntimeOwnershipConflictError` | `409` | Startup exit `3`; fixed `safe_message` |
| `RUNTIME_BOOTSTRAP_FAILED` | `RuntimeBootstrapFailedError` | `500` | Startup exit `4`; never serves HTTP |
| `RUNTIME_SHUTDOWN_FAILED` | `RuntimeShutdownFailedError` | `500` | Shutdown exit `4`; lock retained, no clean receipt |
| `DEPENDENCY_TIMEOUT` | `DependencyTimeoutError` / query timeout | `504` | Fixed `safe_message` |
| `DEPENDENCY_UNAVAILABLE` | `DependencyUnavailableError` | `503` | Fixed `safe_message` |
| `INVALID_PROVIDER_RESPONSE` | `ProviderResponseError` | `502` | Fixed `safe_message` |
| `CANONICAL_DATA_INCONSISTENT` | `CanonicalDataError` | `500` | Fixed `safe_message` |
| `INVARIANT_VIOLATION` | `InvariantViolationError` / `max_response_bytes` | `500` | Fixed `safe_message` |
| `RESOURCE_EXHAUSTED` | `ResourceExhaustedError` / pool / session / backpressure | `503` | Fixed `safe_message` |
| `INVALID_CONFIGURATION` | `ConfigurationError` | `500` | Startup exit `2`; redacted |
| `SOURCE_MUTATED` | `SourceMutationError` | `409` | Fixed `safe_message` |
| `INVALID_REQUEST` *(transport)* | Pydantic `ValidationError` / malformed JSON | `422` (`400` malformed JSON) | Field location + safe validation summary |
| `PAYLOAD_TOO_LARGE` *(transport)* | Request body `> 64 KiB` | `413` | `"Request payload exceeds allowed size limit."` |
| `UNAUTHORIZED` *(transport)* | Missing/invalid Bearer token on `/control/*` | `401` | `"Valid local control authorization is required."` |
| `FORBIDDEN` *(transport)* | Invalid `Host`, cross-origin `Origin`, missing `X-AtlasRAG-Client` | `403` | `"Request rejected by local transport security policy."` |
| `SESSION_NOT_FOUND` *(transport)* | Unknown/expired/closed `session_id` | `404` | `"Session was not found or has expired."` |
| `OPERATION_NOT_FOUND` *(transport)* | Unknown/evicted `operation_id` | `404` | `"Maintenance operation was not found."` |
| `REQUEST_CANCELLED` *(transport)* | Disconnect / cancellation mid-stream | `499` / SSE `error` | `"Request was cancelled before completion."` |
| `INTERNAL_ERROR` *(transport)* | Unhandled unexpected exception | `500` | `"An internal server error occurred."` |

---

### D11-10 — Local Transport Security

- **Local single-user trust assumption:** AtlasRAG v1 runs for a single local OS user on loopback (`127.0.0.1`). Same-UID local processes share access to `0o600` user state files; cross-origin browser pages and network peers are untrusted.
- **Server protections:**
  1. `ServerConfig.host` enforces loopback-only (`127.0.0.1`, `::1`, `localhost`).
  2. Strict `Host` allowlist (`127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>`) prevents DNS rebinding (`403 FORBIDDEN`).
  3. No wildcard CORS (`CORSMiddleware` omitted); `POST`/`DELETE`/`PUT`/`PATCH` with non-matching `Origin`/`Referer` fail `403 FORBIDDEN`.
  4. `X-AtlasRAG-Client` (`"web"` | `"cli"`) is explicitly a **request-classification and browser simple-form CSRF guard**, **not** an authentication credential.
  5. Independent privileged Control Plane authorization: `/api/v1/control/*` requires `Authorization: Bearer <token>` verified via `hmac.compare_digest` against `~/.local/state/atlasrag/control.token` (`0o600`, written and cleaned up strictly under `RuntimeOwnershipLock`). Never exposed to Knowledge Plane or Stage 12 static Web assets.
  6. `64 KiB` (`65_536` bytes) request body limit (`413 PAYLOAD_TOO_LARGE`).
- **CLI client protections:**
  1. Validate target `--server-url` / `ATLASRAG_SERVER_URL` / `server.json` URL is strict `http://` (or `https://`) loopback (`127.0.0.1`, `localhost`, `::1` only, no userinfo) **before** reading or attaching `control.token`. Refuse sending `control.token` to any non-loopback target.
  2. Configure `httpx.Client` / `AsyncClient` with `follow_redirects=False` and `trust_env=False` (preventing credential leakage via redirects or `HTTP_PROXY`/`HTTPS_PROXY` env vars).
  3. Read `control.token`, `server.json`, `shutdown-*.json`, and `cli_session.json` safely with `os.O_NOFOLLOW`, `stat.S_ISREG`, `st_uid == os.getuid()`, and `(st_mode & 0o077) == 0`.

---

### D11-11 — Operational CLI, Pre-Shutdown Identity Capture & Verified Process Termination

Implemented with `argparse` + `httpx` in `src/atlasrag/cli/main.py` and `src/atlasrag/server/launcher.py`:

#### Complete CLI Command, Argument, Timeout & Discovery Contract

| Command | Arguments | HTTP Timeout Policy | Auth / Session / Output |
|---|---|---|---|
| `atlasrag serve` | `--host 127.0.0.1`, `--port 8000`, `--config PATH`, `--server-config PATH`, `--json` | N/A (foreground server) | Acquires `RuntimeOwnershipLock`; writes `control.token` post-lock, `server.json` post-bind, `shutdown-<id>.json` pre-release. |
| `atlasrag ask <query>` | `--session-id ID`, `--new-session`, `--file-name`, `--source-type`, `--sheet-name`, `--stream`, `--timeout 120.0`, `--json`, `--server-url` | `3.0s` connect; `120.0s` (or `--timeout`) read/stream | Reuses `cli_session.json` (`0o600`) or `--session-id`; `X-AtlasRAG-Client: cli`; prints answer + citations + `[cache=..., trace=..., elapsed=...ms]` or `--json`. |
| `atlasrag health` | `--live` \| `--ready` (default `--ready`), `--json`, `--server-url` | `3.0s` connect; `10.0s` read | Knowledge/Host; exit `0` if live/ready, `1` if `503`/unhealthy, `3` unreachable. |
| `atlasrag status` | `--refresh`, `--json`, `--server-url` | `3.0s` connect; `10.0s` read | Knowledge/Host; safe status table or JSON; exit `0`, `1`, `3`. |
| `atlasrag sync` | `--wait` / `--no-wait` (default `--wait`), `--poll-interval 1.0`, `--json`, `--server-url` | `3.0s` connect; `10.0s` per request | Control (`control.token` on validated loopback); verifies instance/pidfd/flock/discovery/token stability before `POST` & every poll `GET` (never switches instances), validates `operation_id`/`kind`, exits `0` only on terminal `SAFE_TO_SERVE`. |
| `atlasrag recover` | `--wait` / `--no-wait` (default `--wait`), `--poll-interval 1.0`, `--json`, `--server-url` | `3.0s` connect; `10.0s` per request | Control (`control.token`); same pre-send & per-poll instance/pidfd/flock/discovery/token stability check and `operation_id`/`kind` check; exits `0` only on terminal `SAFE_TO_SERVE`. |
| `atlasrag stop` | `--timeout <seconds>`, `--force`, `--json`, `--server-url` | `3.0s` connect; `10.0s` HTTP trigger | Control (`control.token`); pre-shutdown identity, stability & `pidfd` capture (no rediscovery after shutdown starts), post-release `CLEAN_SHUTDOWN` receipt verification, verified `pidfd` escalation. |

- **Discovery precedence:** `--server-url` > `ATLASRAG_SERVER_URL` > `~/.local/state/atlasrag/server.json` (`url`) > `http://127.0.0.1:8000`.
- **Thin-client rule:** Only `serve` imports/runs `ApplicationComposition`.

#### Exact `atlasrag stop` & Pre-Shutdown `pidfd` Verification Sequence

1. **Before sending `POST /api/v1/control/shutdown`:**
   - Read `server.json` safely (`O_NOFOLLOW`, regular file, UID match, `0o600`) and validate loopback target URL (`follow_redirects=False`, `trust_env=False`).
   - Capture immutable `TargetServerIdentity(server_instance_id, pid, process_start_ticks, boot_id, url)`.
   - Verify `boot_id == /proc/sys/kernel/random/boot_id`.
   - Open `pidfd = os.pidfd_open(pid, 0)`, then **re-verify** `/proc/{pid}/stat` `starttime == process_start_ticks` and `uid == os.getuid()` **after** opening `pidfd`, and verify `runtime.lock` is actively `flock`-held.
   - If `--force` is requested and `server.json` is missing/unsafe, `pidfd` is unsupported, or process-start verification fails -> **refuse `--force` operation immediately** (exit `4`) without signalling any process!
   - Retain this verified `pidfd` handle and `TargetServerIdentity` in memory for the **entire** `stop` command. **Never re-read `server.json` after sending the shutdown request.** (Before initial `POST`, also confirm discovery, `control.token`, `starttime`, and `flock` did not change.)
2. **Send `POST /api/v1/control/shutdown`** (carrying `control.token` captured before shutdown; ignore subsequent `server.json` removal or replacement by a new server).
3. **Wait for retained `pidfd` exit** up to `--timeout` (`2 * shutdown_timeout_seconds + 5.0` default — note this default is an observation timeout, not an upper bound for every sequential shutdown path; long-running tests specify explicit timeouts):
   - When `pidfd` becomes readable (original process exited): check for `shutdown-<target.server_instance_id>.json` (`O_NOFOLLOW`, UID, `0o600`) with matching `server_instance_id`, integer `pid`, and `status == "CLEAN_SHUTDOWN"` (which attests runtime settlement, cleanup, shared metadata cleanup, and `ownership.release()`, not the non-child process's unavailable POSIX exit code):
     - **Receipt valid:** unlink consumed receipt, exit `0` (**Clean Runtime Shutdown**).
     - **Receipt missing/invalid:** exit `4` (**Unverified Process Exit** — never exit `0`!).
   - If `pidfd` is still alive when `--timeout` expires:
     - Without `--force`: exit `1` (shutdown timed out; server still alive).
     - With `--force`: send `SIGTERM` via `signal.pidfd_send_signal(pidfd, signal.SIGTERM)`, wait bounded grace on `pidfd`, then `SIGKILL` via `signal.pidfd_send_signal(pidfd, signal.SIGKILL)` if still alive -> exit `4` (**Forced Process Termination** — never exit `0`!).
   - Close `pidfd` in `finally`.

#### Unified Exit Codes

- `0`: Success / verified clean graceful shutdown
- `1`: Operational/remote error (`4xx`/`5xx`, `sync`/`recover` returned `FAILED` or `UNSAFE_TO_SERVE`, `stop` timed out without `--force`)
- `2`: CLI argument error or `ConfigurationError`
- `3`: `RuntimeOwnershipConflictError` (`serve`) or server unreachable (`ConnectionError` in client commands)
- `4`: Server hard bootstrap failure, socket-bind failure, `RuntimeShutdownFailedError`, unverified process exit, forced `SIGKILL` termination, or refused unverified `--force` escalation
- `130` / `143`: Thin CLI client interrupted by `SIGINT` / `SIGTERM` (`serve` handles `SIGINT`/`SIGTERM` gracefully, exiting `0` on clean shutdown and `4` on `RuntimeShutdownFailedError`)

---

### D11-12 — Stage 12 Web Demo Handoff & Real-Pipeline Acceptance Boundary

- **Frontend direction:** Lightweight same-origin static `HTML/CSS/JS` + Fetch + safe minimal Markdown + POST SSE served from `app.py` (`GET /`); maintenance remains CLI-only (`control.token` never exposed to browser).
- **Safe Markdown rendering rule (preserved explicitly):** Stage 12 must escape raw HTML (`<`, `>`, `&`, `"`, `'`) in answer text and citations, restrict clickable links strictly to `http:` / `https:` URLs (blocking `javascript:`, `data:`, and inline `<script>` / event-handler injection), and treat all retrieved content and Web titles/URLs as untrusted data.
- **Stage 12 Real-Pipeline Acceptance Boundary (`ADR-002`):**
  Stage 12 Web Demo acceptance **must include a bounded real-model, real-knowledge-base end-to-end demonstration** (fake-only transport mocks are insufficient to claim real RAG usability):
  - Real Qwen embedding, real selected BGE reranker, real answer generation, reviewed `FinalResponse`, resolved Local/Web citations, existing Redis cache behavior (same-session exact-request cache hit + cross-session isolation), and basic runtime/resource observations.
  - Exact real API budgets, bounded KB fixtures, and execution approval belong to Stage 12 planning (not executed in Stage 11).

---

## 5. Deployment, Deferrals & Owner Decisions

- **Resource & Environment:** Preserves WSL2 native Python 3.11 + Docker Compose (`postgres:17.8-alpine`, `redis:8.2.10-alpine`, `milvusdb/milvus:v2.6.20` + `etcd` + `minio`, `searxng`); estimated `< 50 MiB` server RSS delta; full RTX 3070 8GB VRAM/OOM validation remains Stage 14.
- **Stage 13 MCP Deferrals:** `search_domain_docs`, `search_external_evidence`, `atlasrag_ask`/`search`/`list_sources`/`status`, MCP SDK/FastMCP, domain metadata/ACLs, Evidence Mode, Evidence Cache, Domain Epoch, SQL/doc conflict detection, cloud embeddings/rerankers, 16GB multi-domain budgets.
- **Stage 14 Preserved Obligations:** Coordinated backup/restore (`§90–91`), full RTX 3070 VRAM/OOM validation, MinerU subprocess settlement, Web+MCP concurrency/isolation, transferred Stage 9 `S9-17`/`S9-21` performance, Final Part IV Acceptance (`§94`).

### Approved Owner Decisions

| Decision ID | Chosen Option | Summary |
|---|---|---|
| `OD11-01` | **B (OWNER_APPROVED)** | Minimal backward-compatible runtime extensions (`QueryCacheStatus` on `QueryResponseEnvelope`, `on_admitted` & exception-safe `on_draining` hooks, `on_shutdown_settled` pre-release shared-cleanup hook, `InvocationObserverMux` wiring, `HealthRegistry.query_cache`, `ApplicationCoordinator.status(refresh=...)`). |
| `OD11-02` | **B (OWNER_APPROVED)** | `GET /api/v1/status` defaults to freshness-bounded `refresh_if_stale()`; `?refresh=true` enforces single-flight lock + server cooldown. |
| `OD11-03` | **B (OWNER_APPROVED)** | Mandatory server-issued sessions via `POST /api/v1/sessions`; unknown/expired sessions fail `404 SESSION_NOT_FOUND`. |
| `D11-02-AMENDMENT-01` | **OWNER_APPROVED** | Instance-Scoped Post-Release Clean Receipt: pre-release `on_shutdown_settled` performs maintenance verification and shared metadata cleanup (`server.json`, `control.token`) strictly under `RuntimeOwnershipLock`; atomic `shutdown-<server_instance_id>.json` (`CLEAN_SHUTDOWN`) is published only after confirmed `ownership.release()` and all mandatory lifecycle checks succeed. |
