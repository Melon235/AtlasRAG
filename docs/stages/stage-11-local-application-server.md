# Stage 11 Detailed Design — Local Application Server

- **Status:** `READY_FOR_OWNER_REVIEW` (Design & Implementation Planning Only — **Implementation `NOT_STARTED`**)
- **Baseline (`origin/main`):** `9cd2344f2076d60ee53568eb35aaa625474dc645` (`PR #11` & `PR #12` **MERGED**)
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
- Safe stage/status SSE streaming (`accepted`, `progress`, `final`, `error`) projected from `ProductionObservation` without exposing raw observations, unreviewed tokens, CoT, or private evidence.
- Process-local bounded `MaintenanceOperationRegistry` for authorized asynchronous `sync` and `recover` operations.
- Local Transport Authorization (loopback bind, Host/Origin validation, browser CSRF protection, `0o600` Bearer control token separating Knowledge Plane from Control Plane, request size bounds).
- Thin `argparse` + `httpx` operational CLI (`serve`, `ask`, `health`, `status`, `sync`, `recover`, `stop`) and OS signal / launcher termination boundaries.

### Explicit Out-of-Scope Boundaries (Deferred)

- Web Demo HTML/CSS/JS frontend implementation (Stage 12).
- Domain-aware MCP server (`search_domain_docs`, `search_external_evidence`, FastMCP/MCP SDK, zero-trust security metadata/ACLs, Evidence Mode, Evidence Cache, Domain Epoch) (Stage 13).
- Coordinated backup and restore (`§90–91`), real CUDA Qwen/BGE weight validation, MinerU SDK subprocess settlement, RTX 3070 dual-entrypoint VRAM/OOM validation, and transferred Stage 9 performance runner (`S9-17`, `S9-21`) (Stage 14).
- Multi-tenant RBAC, OAuth, persistent DB conversation history, Celery/Redis task queues, WebSockets, or reverse proxies.

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
|  - Lifespan: ApplicationComposition.start() <-> shutdown()        |
+-------------------------------+-----------------------------------+
                                |
                                v
+-------------------------------------------------------------------+
| ApplicationServerService (Framework-Independent Adapter Boundary) |
|  - SessionRegistry (process-local, bounded, in-flight safe)       |
|  - MaintenanceOperationRegistry (process-local, bounded)          |
|  - InvocationEventBus (ProductionObserver -> safe SSE projection) |
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
|   +-- InFlightTaskRegistry (QUERY / MAINTENANCE / HEALTH)         |
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
| [`src/atlasrag/runtime/coordinator.py`](../../src/atlasrag/runtime/coordinator.py) | `ApplicationCoordinator`: `run_query(request: UserTurnRequest) -> QueryResponseEnvelope`, `run_maintenance() -> SyncResult`, `recover() -> SyncResult`, `health() -> HealthSnapshot`, `readiness() -> bool`, `shutdown() -> None`. Uses `_begin_operation(TaskKind)` + `observation_invocation(invocation)`. | All HTTP/SSE queries, health/status checks, maintenance, recovery, and shutdown flow through this single coordinator. |
| [`src/atlasrag/runtime/ownership.py`](../../src/atlasrag/runtime/ownership.py) | `RuntimeOwnershipLock`: fixed `~/.local/state/atlasrag/runtime.lock` (`0o700` dir, `0o600` regular non-symlink file, `O_NOFOLLOW`, `flock(LOCK_EX | LOCK_NB)`), writes `pid=<pid>\n`. | Guarantees single runtime per OS user. Never overridden or bypassed by CLI/server. |
| [`src/atlasrag/runtime/gate.py`](../../src/atlasrag/runtime/gate.py) | `RuntimeGate`: atomic mode (`STARTING`, `SERVING`, `DRAINING`, `MAINTENANCE`, `BLOCKED`, `SHUTTING_DOWN`), `lifecycle_generation`, `active_queries`, `acquire_query() -> QueryLease`, `begin_maintenance()`, `begin_recovery()`, `begin_shutdown()`. | Enforces atomic serving vs maintenance mutual exclusion; HTTP routes never duplicate lock/mode state. |
| [`src/atlasrag/runtime/tasks.py`](../../src/atlasrag/runtime/tasks.py) | `InFlightTaskRegistry`: tracks actual `asyncio.Task` objects by `TaskKind` (`QUERY`, `MAINTENANCE`, `HEALTH`, `STARTUP`, `CACHE`), supports `close()`, `wait_empty(timeout)`, `cancel_all()`. | HTTP/SSE handlers must keep coordinator operations awaited inside registered tasks until settlement. |
| [`src/atlasrag/runtime/health.py`](../../src/atlasrag/runtime/health.py) | `HealthRegistry`: single-flight `_refresh_lock`, bounded `probe_timeout`, `refresh()`, `refresh_if_stale()`, `readiness()`. Hard dependencies: `postgres`, `milvus`, `embedding` + `_knowledge_verified`. Soft dependencies: `redis`, `searxng`, `reranker`. **Note:** `query_cache="UNKNOWN"` is currently hardcoded at `health.py:125`. | Used for `/health/ready` (`readiness()`) and `/status`. Requires minimal `query_cache` status projection extension. |
| [`src/atlasrag/runtime/cache_freshness.py`](../../src/atlasrag/runtime/cache_freshness.py) | `CacheFreshnessBarrier`: `state` (`TRUSTED` / `BYPASSED`), `active_write_fence`, `enforce()`, `recover_if_due()`. | Source of truth for whether Query Cache is currently `TRUSTED` or `BYPASSED`. |
| [`src/atlasrag/runtime/config.py`](../../src/atlasrag/runtime/config.py) & [`src/atlasrag/config/models.py`](../../src/atlasrag/config/models.py) | `load_runtime_config()` loads `ATLASRAG_CONFIG` + `ATLASRAG_CONFIG_JSON` into `RuntimeConfig` (`FrozenModel`) with `_redacted()` on `ValidationError`. Pool contract: `postgres_pool_max_size >= 2 * max_document_concurrency + 2` (default `16`), `postgres_pool_min_size <= postgres_pool_max_size` (default `2`). | Preserved intact; server transport settings live in a separate `ServerConfig` (`FrozenModel`) excluded from both `pipeline_fingerprint` and `runtime_fingerprint`. |
| [`src/atlasrag/domain/requests.py`](../../src/atlasrag/domain/requests.py) | `UserTurnRequest(session_id: StrictStr, query: StrictStr, filters: QueryFilters | None = None)`; `QueryFilters(file_name, source_type, sheet_name)`. Both `FrozenModel` (`extra="forbid"`). | Exact query input contract. No unsupported `top_k`, `web_enabled`, model override, or Milvus filter fields permitted. |
| [`src/atlasrag/domain/results.py`](../../src/atlasrag/domain/results.py) | `FinalResponse(answer_text: StrictStr, citations: tuple[FinalCitation, ...])`; `FinalCitation = FinalLocalCitation | FinalWebCitation`. | Returned unmodified inside `QueryResponse.response`. |
| [`src/atlasrag/domain/observation.py`](../../src/atlasrag/domain/observation.py) | `ObservationStage` (`DENSE`, `BM25`, `FUSION`, `RERANKER`, `EVIDENCE`, `SUFFICIENCY`, `ROUTING`, `ANSWER`), `ProductionObservation`, `ProductionObserver`, `observation_invocation(invocation_id)`. | **Gap found:** `build_default_business` in `production.py` currently leaves `observer=NULL_OBSERVER` on `LocalEvidenceServices` and `RagCoreServices`, and neither `retrieval_graph` nor `web_evidence` emits observations. |
| [`src/atlasrag/services/cache/session.py`](../../src/atlasrag/services/cache/session.py) & [`src/atlasrag/graphs/retrieval/graph.py`](../../src/atlasrag/graphs/retrieval/graph.py) | `SessionQueryCache`: `lookup(request) -> FinalResponse | None`, `prepare(...)`, `commit(...) -> CacheCommitOutcome` (`STORED`, `BYPASSED`, `UNKNOWN`). `RetrievalRunner.run(request) -> FinalResponse`. | **Gap found:** Neither `SessionQueryCache.lookup` nor `RetrievalRunner.run` currently exposes `HIT` vs `MISS` vs `BYPASSED` to `ApplicationCoordinator`. |
| [`src/atlasrag/domain/errors.py`](../../src/atlasrag/domain/errors.py) | `ErrorCode` (`15` codes) and `AtlasRAGError` hierarchy (`safe_message` without secrets/stack traces). | All 15 codes mapped 1:1 to HTTP responses without duplicating meanings. |

### 3.2 Required Minimal Stage 10 Interface Extensions

To satisfy D11-05, D11-06, and D11-09 truthfully without inferring status from latency or fabricating events, Stage 11 requires four minimal, backward-compatible runtime extensions:

1. **Truthful Query Cache Status & Elapsed Timing on `QueryResponseEnvelope` (`coordinator.py`, `session.py`, `retrieval/graph.py`, `application/retrieval.py`):**
   - Add `QueryCacheStatus(StrEnum)` (`HIT`, `MISS`, `BYPASSED`, `UNKNOWN`).
   - Add `SessionQueryCache.lookup_with_status(request) -> tuple[FinalResponse | None, QueryCacheStatus]` (returning `HIT`, `MISS`, or `BYPASSED` based on actual policy, freshness trust, write-fence check, and Redis outcome; preserving `lookup()` as a thin delegating wrapper).
   - Record `execution_facts["cache_status"]` in `build_retrieval_graph` and expose `RetrievalRunner.run_with_facts(request) -> RetrievalExecution(final_response, cache_status)` (or report via an optional context-local execution-facts sink) while preserving `RetrievalRunner.run(request) -> FinalResponse` unchanged.
   - Extend `QueryResponseEnvelope` with backward-compatible defaults:
     ```python
     @dataclass(frozen=True)
     class QueryResponseEnvelope:
         trace_id: str
         response: FinalResponse
         cache_status: QueryCacheStatus = QueryCacheStatus.UNKNOWN
         elapsed_ms: float | None = None
     ```
   - `ApplicationCoordinator.run_query` measures monotonic elapsed time (`elapsed_ms`) and propagates actual `cache_status`, defaulting conservatively to `QueryCacheStatus.UNKNOWN` if an injected test `retrieval` callable does not report cache facts.
2. **Per-Invocation `ProductionObserver` Routing (`production.py`, `coordinator.py`, optional `observation.py`):**
   - Wire an invocation-scoped `ProductionObserver` multiplexer (`InvocationObserverMux` dispatching by `event.invocation_id` or `current_invocation_id()`) into `LocalEvidenceServices.observer` and `RagCoreServices.observer` inside `build_default_business`, and accept an optional `observer: ProductionObserver` on `CoordinatorDependencies` / `ApplicationComposition.start(..., observer=...)`.
   - Optionally add `ObservationStage.CACHE`, `ObservationStage.QUERY_CONSTRUCTION`, and `ObservationStage.WEB` emissions in `retrieval/graph.py` and `rag_core/subgraph.py` only where genuinely executed—never fabricating stages on cache hits or skipped branches.
3. **Truthful `query_cache` Health Projection & Cached Status Read (`health.py`, `coordinator.py`):**
   - Allow `HealthRegistry` to accept an optional `query_cache_status: Callable[[], str]` (wired in `ApplicationComposition.start` from `CacheFreshnessBarrier.state`, `config.cache.query_cache_enabled`, and latest Redis health -> `"ACTIVE"`, `"BYPASSED"`, `"DISABLED"`, or `"UNKNOWN"`), replacing the hardcoded `"UNKNOWN"` at `health.py:125`.
   - Add `ApplicationCoordinator.status(self, *, refresh: bool = False) -> HealthSnapshot` (using `refresh_if_stale()` when `refresh=False` and `refresh()` when `refresh=True`) under `TaskKind.HEALTH` so status polling stays bounded.
4. **Gate Shutdown Entrypoint (`coordinator.py` / `gate.py`):**
   - Expose `await coordinator.begin_shutdown()` (or reuse `await coordinator.gate.begin_shutdown()`) for immediate admission closure upon `SIGINT`/`SIGTERM`/`/control/shutdown` before Uvicorn drains active HTTP/SSE connections and calls `await coordinator.shutdown()`.

---

## 4. Design Decisions (`D11-01` through `D11-12`)

### D11-01 — Technology Stack and Package Structure

#### Recommended Dependencies & Lockfile Compatibility

Inspection of `pyproject.toml` and `uv.lock` confirms:

- Repository Python constraint: `requires-python = ">=3.11,<3.12"` (verified `.venv`: `Python 3.11.16`).
- Locked core libraries already in `uv.lock`: `pydantic==2.13.5`, `pydantic-core==2.46.5`, `httpx==0.28.1`, `anyio==4.14.2`, `langgraph==1.2.15`.
- Transitive/optional-group versions **already resolved in `uv.lock`** (via `parsing-mineru` group):
  - `fastapi==0.143.0` (depends on `pydantic`, `starlette==1.7.0`, `annotated-doc`, `opentelemetry-api`, `typing-extensions`, `typing-inspection`)
  - `uvicorn==0.54.0` (depends on `click`, `h11==0.16.0`)

**Recommended `[project.dependencies]` additions (for Stage 11 implementation only; unchanged now):**

| Package | Recommended Constraint | Locked `uv.lock` Version | Compatibility Rationale |
|---|---|---|---|
| `fastapi` | `>=0.143.0,<0.144.0` | `0.143.0` | Matches existing `uv.lock` resolution; full Pydantic v2 (`2.13.5`) and Starlette `1.7.0` / AnyIO `4.14.2` compatibility; avoids unexpected `mineru` group conflicts. |
| `uvicorn` | `>=0.54.0,<0.55.0` | `0.54.0` | Plain ASGI server without `[standard]` extras (`watchfiles` / `websockets`), aligning with `reload=False` and no WebSockets. |

And `[project.scripts]` in `pyproject.toml` will add:

```toml
[project.scripts]
atlasrag = "atlasrag.cli.main:main"
```

#### Minimal Package Structure

```text
src/atlasrag/
├── server/
│   ├── __init__.py
│   ├── config.py        # ServerConfig (FrozenModel) & env/JSON loader (excluded from fingerprints)
│   ├── schemas.py       # Pydantic v2 FrozenModel HTTP/SSE/Operation/Error DTOs
│   ├── errors.py        # ErrorCode + transport error -> (HTTP status, code, safe_message)
│   ├── auth.py          # Host allowlist, Origin/CSRF check, 0o600 Bearer control token
│   ├── sessions.py      # Bounded process-local SessionRegistry (in-flight safe)
│   ├── events.py        # InvocationEventBus + ProductionObserver -> strict SSE allowlist
│   ├── operations.py    # Bounded process-local MaintenanceOperationRegistry
│   ├── service.py       # Framework-independent ApplicationServerService (no FastAPI imports)
│   ├── app.py           # Thin FastAPI app factory, lifespan, middleware, /api/v1/* routes
│   └── launcher.py      # Uvicorn programmatic server runner, signals, server.json, exit codes
└── cli/
    ├── __init__.py
    └── main.py          # argparse + httpx thin CLI (serve, ask, health, status, sync, recover, stop)
```

**Boundary enforcement:** Extend `scripts/check_import_boundaries.py` during Stage 11 implementation so `src/atlasrag/server/service.py`, `sessions.py`, `events.py`, `operations.py`, `schemas.py`, `errors.py`, and `src/atlasrag/cli/main.py` must not import `fastapi`, `starlette`, or `uvicorn` (only `server/app.py`, `server/auth.py` if ASGI middleware, and `server/launcher.py` touch FastAPI/Uvicorn). `src/atlasrag/graphs/`, `services/`, `repositories/`, `providers/`, and `runtime/` never import `atlasrag.server` or `atlasrag.cli`.

---

### D11-02 — Single Runtime Ownership and Server Lifecycle

`atlasrag serve` is the **sole production runtime-owning entrypoint**:

- Uvicorn runs programmatically via `uvicorn.Server(uvicorn.Config(app, host=server_cfg.host, port=server_cfg.port, workers=1, reload=False, access_log=..., timeout_graceful_shutdown=server_cfg.uvicorn_graceful_shutdown_seconds))`.
- `workers` is strictly `1`; `reload` is strictly `False`.
- `ApplicationComposition` count = `1`; `ApplicationCoordinator` count = `1`.

#### Exact Lifecycle Ordering

```text
1. CLI / Config Load
   ├── load_runtime_config() + load_server_config()
   └── Invalid config -> exit code 2 (before touching lock or socket)

2. Server State & Control Token Prep (inside launcher, after config valid)
   ├── Ensure ~/.local/state/atlasrag (0o700, owned by uid, non-symlink)
   └── Write fresh control token atomically to control.token (0o600, O_NOFOLLOW)

3. Uvicorn Startup -> FastAPI Lifespan Startup
   ├── ApplicationComposition(runtime_config).start(observer=event_bus)
   │    ├── Acquire RuntimeOwnershipLock (~/.local/state/atlasrag/runtime.lock)
   │    │    └── RuntimeOwnershipConflictError -> abort startup -> exit code 3
   │    │        (existing server's lock and resources remain untouched)
   │    ├── Open PG pool, HTTP clients, Redis, Milvus, Qwen (+ optional BGE)
   │    ├── StartupReconciliation -> StartupSourceSafety -> GlobalKnowledgeVerification
   │    │    ├── KNOWLEDGE_DIRECTORY_UNAVAILABLE / hard dependency failure
   │    │    │    -> RuntimeBootstrapFailedError -> close_resources -> release lock (unless cleanup failed) -> exit code 4
   │    │    └── Initialized but unsafe KB -> RuntimeMode.BLOCKED (server stays alive!)
   │    └── Safe KB -> CacheFreshnessBarrier.enforce() -> RuntimeMode.SERVING
   └── Write server.json (url, pid, started_at, server_instance_id) atomically (0o600)

4. Serving / Blocked Operation
   ├── SERVING: /health/live=200, /health/ready=200, queries admitted, sync admitted
   └── BLOCKED: /health/live=200, /health/ready=503, queries=503, sync=503, recover=202

5. Shutdown Initiation (SIGINT / SIGTERM / POST /api/v1/control/shutdown)
   ├── Immediately close new admission: await coordinator.gate.begin_shutdown()
   │    (subsequent /query, /query/stream, /control/sync, /control/recover -> 503 RUNTIME_SHUTTING_DOWN)
   ├── Set uvicorn.Server.should_exit = True
   ├── Uvicorn stops accepting new connections and waits up to timeout_graceful_shutdown
   │    (configured >= 2 * maintenance.shutdown_timeout_seconds + 5.0)
   └── FastAPI Lifespan Shutdown:
        ├── await coordinator.shutdown()
        │    ├── tasks.close() -> wait_empty(shutdown_timeout_seconds)
        │    ├── if unsettled: begin_guard_shutdown() -> tasks.cancel_all() -> wait(shutdown_timeout_seconds)
        │    ├── if still pending -> raises RuntimeShutdownFailedError (lock RETAINED!)
        │    ├── close_resources() (if error -> raises, lock RETAINED!)
        │    └── ownership.release()
        ├── Remove server.json (best-effort) & remove control.token on clean shutdown
        └── If RuntimeShutdownFailedError or close error -> mark shutdown_failed=True -> process exits code 4
            Else -> process exits code 0
```

**Preserved invariant:** Hard bootstrap failure exits non-zero (`4`) rather than pretending to be `BLOCKED`. Recoverable `BLOCKED` keeps the server process live (`200 /health/live`, `503 /health/ready`) so an operator can inspect `/status` and run `atlasrag recover`.

---

### D11-03 — Versioned Minimal HTTP API Contract (`/api/v1/*`)

All 11 candidate endpoints are necessary and non-redundant; none are dropped, and no extra endpoints are added:

| Method & Path | Plane / Auth | Request Schema | Response Schema (2xx) | Status Codes & Error Codes |
|---|---|---|---|---|
| `POST /api/v1/sessions` | Knowledge (Host + Origin + `X-AtlasRAG-Client` for browser) | `CreateSessionRequest` (`client_kind`: `"web" \| "cli"` optional, default `"web"`) | `SessionResponse` (`session_id`, `client_kind`, `created_at`, `expires_at`) | `201`, `400/422 INVALID_REQUEST`, `403 FORBIDDEN`, `413 PAYLOAD_TOO_LARGE`, `503 RESOURCE_EXHAUSTED` / `RUNTIME_SHUTTING_DOWN` |
| `DELETE /api/v1/sessions/{session_id}` | Knowledge (Host + Origin + `X-AtlasRAG-Client`) | Path `session_id` | `204 No Content` | `204`, `403 FORBIDDEN`, `404 SESSION_NOT_FOUND`, `503 RUNTIME_SHUTTING_DOWN` |
| `POST /api/v1/query` | Knowledge (Host + Origin + `X-AtlasRAG-Client`) | `QueryRequest` (`session_id`, `query`, `filters: QueryFilters \| None`) | `QueryResponse` (`trace_id`, `session_id`, `cache_status`, `elapsed_ms`, `response: FinalResponse`) | `200`, `400/422 INVALID_REQUEST`, `403 FORBIDDEN`, `404 SESSION_NOT_FOUND`, `413 PAYLOAD_TOO_LARGE`, `500`, `502`, `503`, `504` |
| `POST /api/v1/query/stream` | Knowledge (Host + Origin + `X-AtlasRAG-Client`) | `QueryRequest` (`session_id`, `query`, `filters: QueryFilters \| None`) | `text/event-stream` (`accepted`, `progress`, `final`, `error`) | Pre-stream: `400/422`, `403`, `404`, `413`, `503`. Mid-stream: `200` + SSE `error` event |
| `GET /api/v1/health/live` | Public/Host (loopback Host check) | None | `LivenessResponse` (`status: "ALIVE"`, `mode: RuntimeMode`) | `200`, `403 FORBIDDEN` |
| `GET /api/v1/health/ready` | Public/Host (loopback Host check) | None | `ReadinessResponse` (`ready: bool`, `mode: RuntimeMode`) | `200` (ready), `503` (not ready), `403 FORBIDDEN` |
| `GET /api/v1/status` | Public/Host (`?refresh=false` default) | Query `refresh: bool = False` | `RuntimeStatusResponse` (`mode`, `ready`, `knowledge_verified`, `active_queries`, `dependencies`, `query_cache`, `pipeline_fingerprint`, `runtime_fingerprint`, `checked_at`) | `200`, `403 FORBIDDEN`, `503 RUNTIME_SHUTTING_DOWN` |
| `POST /api/v1/control/sync` | Control (`Bearer` token + Host + Origin) | Empty / `{}` (`extra="forbid"`) | `OperationAcceptedResponse` (`operation_id`, `kind: "SYNC"`, `status: "RUNNING"`, `accepted_at`) | `202`, `401 UNAUTHORIZED`, `403 FORBIDDEN`, `409 MAINTENANCE_BUSY`, `503 RUNTIME_NOT_READY` / `RUNTIME_SHUTTING_DOWN` |
| `POST /api/v1/control/recover` | Control (`Bearer` token + Host + Origin) | Empty / `{}` (`extra="forbid"`) | `OperationAcceptedResponse` (`operation_id`, `kind: "RECOVER"`, `status: "RUNNING"`, `accepted_at`) | `202`, `401 UNAUTHORIZED`, `403 FORBIDDEN`, `409 MAINTENANCE_BUSY`, `503 RUNTIME_NOT_READY` / `RUNTIME_SHUTTING_DOWN` |
| `GET /api/v1/control/operations/{operation_id}` | Control (`Bearer` token + Host) | Path `operation_id` | `OperationStatusResponse` (`operation_id`, `kind`, `status`, `started_at`, `finished_at`, `result`, `error_code`, `message`) | `200`, `401 UNAUTHORIZED`, `403 FORBIDDEN`, `404 OPERATION_NOT_FOUND` |
| `POST /api/v1/control/shutdown` | Control (`Bearer` token + Host + Origin) | Empty / `{}` (`extra="forbid"`) | `ShutdownAcceptedResponse` (`status: "SHUTTING_DOWN"`, `pid: int`) | `202`, `401 UNAUTHORIZED`, `403 FORBIDDEN` |

#### Transport Request / Response Rules

- **Strict `UserTurnRequest` preservation:** `QueryRequest` uses `FrozenModel` (`extra="forbid"`) with exact fields:
  - `session_id: StrictStr` (1..128 chars, validated against `SessionRegistry`)
  - `query: StrictStr` (non-blank Unicode scalar text, max `8192` chars)
  - `filters: QueryFilters | None = None` (`file_name`, `source_type`, `sheet_name` — exact `atlasrag.domain.requests.QueryFilters`)
  - Any unsupported field (`top_k`, `web_enabled`, `model`, `milvus_expr`, `history`) fails validation immediately (`422 INVALID_REQUEST`).
- **Response envelope:** `QueryResponse` wraps `FinalResponse` cleanly without altering `FinalResponse` or `FinalCitation` (`FinalLocalCitation` / `FinalWebCitation`).
- **Size & timeout limits:**
  - Max request body: `65_536` bytes (`64 KiB`).
  - Max JSON response size: `FinalResponse` text + citations naturally bounded by generation/review limits; server imposes no truncated mid-JSON cutoffs and protects SSE queues with bounded sizes.
  - Default request execution timeout: `ServerConfig.query_timeout_seconds` (default `120.0s`, configurable `1.0..600.0s`), triggering asyncio task cancellation and settlement on expiry.

#### Example Payloads

```json
// POST /api/v1/query Request
{
  "session_id": "web_7f8c9a12b4e04d318a6c5e2f9d0b1a44",
  "query": "What is the maximum PostgreSQL pool size formula?",
  "filters": {
    "source_type": "MD"
  }
}

// POST /api/v1/query 200 Response
{
  "trace_id": "d2f4b8c19e7045a18c2e6f10b4d8a391",
  "session_id": "web_7f8c9a12b4e04d318a6c5e2f9d0b1a44",
  "cache_status": "MISS",
  "elapsed_ms": 843.25,
  "response": {
    "answer_text": "The maximum pool size must be at least 2 * max_document_concurrency + 2 [L1].",
    "citations": [
      {
        "citation_id": "L1",
        "source_kind": "LOCAL",
        "file_name": "stage-10.md",
        "relative_source_path": "docs/stages/stage-10.md",
        "evidence_type": "TEXT_PARENT",
        "source_anchor": {
          "page_number": null,
          "heading": "Pool Sizing",
          "sheet_name": null,
          "cell_range": null
        }
      }
    ]
  }
}

// Standard Error Envelope (4xx/5xx)
{
  "error_code": "RUNTIME_NOT_READY",
  "message": "The AtlasRAG runtime is not ready to accept work.",
  "trace_id": null
}
```

---

### D11-04 — Minimal Process-Local Session Management

Sessions exist strictly as **Query Cache isolation namespaces** (`atlasrag:query:{session_id}:{runtime_fingerprint}:{request_hash}`), never as persistent conversation transcripts:

- **Storage:** Process-local `SessionRegistry` (`dict[str, _SessionEntry]` protected by `asyncio.Lock`). No PostgreSQL tables, no history injection into Query Construction.
- **Identifier format:** Server generates `f"{client_kind}_{secrets.token_hex(16)}"` where `client_kind` is `"web"` or `"cli"` (future Stage 13 uses `"mcp"`).
  - Strictly satisfies `is_cache_key_component_safe()` (ASCII lowercase alphanumeric + `_`, no `:`, no whitespace, no control chars).
  - Namespacing (`web_...`, `cli_...`, future `mcp_...`) guarantees zero accidental cache sharing across entrypoints.
- **Bounds & expiration:**
  - `max_sessions = 256` (configurable `1..4096`), `session_ttl_seconds = 3600.0` (idle TTL updated on query admission), `max_in_flight_per_session = 4`.
  - On `create_session()`: prune expired sessions with `in_flight == 0` first; if still at `max_sessions`, reject with `503 RESOURCE_EXHAUSTED`.
- **In-flight safety during deletion or TTL expiration:**
  - Each `SessionRecord` tracks `in_flight: int` and `closed: bool`.
  - Admission checks `record exists and not record.closed and not expired(now)` and increments `in_flight += 1` inside the registry lock.
  - If `DELETE /api/v1/sessions/{session_id}` is called or TTL expires while `in_flight > 0`: `record.closed = True` (rejects new queries with `404 SESSION_NOT_FOUND`), the active query runs/settles undisturbed, and `release_query(session_id)` removes the record when `in_flight` hits `0`.
- **No Redis scan/delete on session close:** `DELETE /api/v1/sessions/{id}` never runs Redis `SCAN`/`DEL`; existing `query_ttl_seconds` and `CacheFreshnessBarrier` remain authoritative.
- **Web Demo retention:** Browser stores `session_id` in JS memory / `sessionStorage`; "New Session" calls `DELETE /api/v1/sessions/{old}` (best-effort) + `POST /api/v1/sessions`.
- **CLI retention (for same-session cache-hit demonstration):**
  - `atlasrag ask` persists `{"session_id": "...", "server_instance_id": "..."}` in `~/.local/state/atlasrag/cli_session.json` (`0o600`).
  - Repeated `atlasrag ask "same query"` reuses that `session_id`, demonstrating a real same-session `cache_status="HIT"` without DB history.
  - `--new-session` forces `POST /api/v1/sessions` and updates `cli_session.json`; `--session-id <id>` uses the supplied ID directly without overwriting `cli_session.json`.
  - If server restarted (`server_instance_id` changed or `404 SESSION_NOT_FOUND`), `atlasrag ask` (without explicit `--session-id`) transparently allocates a fresh CLI session and notes it on `stderr`; explicit `--session-id` fails with `404 SESSION_NOT_FOUND`.
- **Server restart behavior:** Process-local registry starts empty; old `session_id`s return `404 SESSION_NOT_FOUND`. Because new IDs contain 128-bit cryptographic randomness, a restarted server can never collide with prior Redis cache namespaces.

---

### D11-05 — Existing Redis Cache Integration

Stage 11 reuses Stage 10's cache stack verbatim:

- Key format preserved: `atlasrag:query:{session_id}:{runtime_fingerprint}:{request_hash}` (`providers/cache/keys.py`).
- Raw request hashing preserved: `request_hash(UserTurnRequest)` hashes raw `query` + normalized `filters` before Query Construction (`application/hashing.py`).
- Freshness & fencing preserved: `CacheFreshnessBarrier` (`query_cache_invalidation_required` PostgreSQL marker + atomic Lua `QUERY_WRITE_FENCE_KEY`) and `SessionQueryCache.prepare`/`commit` (`require_write_fence=True`) remain unchanged.
- `ParentContextService` (`TEXT_PARENT` only, canonical `TABLE`) and Redis soft-dependency bypass remain unchanged.
- Fresh `trace_id = uuid4().hex` generated on every `ApplicationCoordinator.run_query` invocation, including cache hits.
- **No secondary HTTP answer cache, no Evidence Cache, no Domain Epoch.**
- **Truthful transport metadata:** `cache_status` (`HIT` / `MISS` / `BYPASSED` / `UNKNOWN`) comes strictly from `SessionQueryCache.lookup_with_status` via `QueryResponseEnvelope`, never inferred from `elapsed_ms`. Internal keys, fences, and Redis DSNs are never exposed.

---

### D11-06 — Minimal Safe SSE (`POST /api/v1/query/stream`)

- Uses `POST /api/v1/query/stream` (for Fetch-based SSE consumption via `ReadableStream` in Stage 12 Web Demo and `httpx` streaming in `atlasrag ask --stream`). No WebSockets, no brokers, no replay.
- Response headers: `Content-Type: text/event-stream; charset=utf-8`, `Cache-Control: no-cache, no-store`, `X-Accel-Buffering: no`.

#### Strict Public Event Allowlist

| SSE `event:` | JSON `data:` Payload | Emission Condition |
|---|---|---|
| `accepted` | `{"trace_id": "<hex>", "session_id": "<id>"}` | Immediately after `QueryRequest` validation, session validation, `RuntimeGate.acquire_query()`, and `TaskKind.QUERY` registration succeed. |
| `progress` | `{"trace_id": "<hex>", "stage": "<stage>", "status": "<status>", "attempt": 1, "degraded": false}` | Projected strictly from `ProductionObservation` via `InvocationEventBus` allowlist (`DENSE`, `BM25`, `FUSION`, `RERANKER`, `EVIDENCE`, `SUFFICIENCY`, `ROUTING`, `ANSWER`, plus optional `CACHE`/`QUERY_CONSTRUCTION`/`WEB` if instrumented). Never contains `candidates`, `selected_identities`, chunk text, or raw `ProductionObservation`. |
| `final` | Full `QueryResponse` JSON (`trace_id`, `session_id`, `cache_status`, `elapsed_ms`, `response`) | Emitted **only** after `ApplicationCoordinator.run_query` returns the completed reviewed (or cached) `FinalResponse`. Terminal event. |
| `error` | `{"trace_id": "<hex_or_null>", "error_code": "<ErrorCode>", "message": "<safe_message>"}` | Emitted if execution fails or times out **after** SSE HTTP `200` headers were sent. Terminal event. |

#### Streaming Correctness, Buffering & Slow Consumers

- **Per-invocation stream channel:** Registered in `InvocationEventBus` under `invocation_id` (passed into `observation_invocation(invocation)` or bound on a per-request child observer).
- **Bounded buffer + non-blocking delivery:**
  - `progress_queue = asyncio.Queue(maxsize=64)` (configurable `16..512`).
  - `terminal_ready = asyncio.Event()`, `terminal_event: SseEvent | None = None`.
  - `deliver_observation` runs synchronously inside graph nodes (`observer.observe(event)`): it projects `ProductionObservation` to a safe `progress` event and calls `put_nowait`. If `progress_queue` is full (slow consumer), it drops the **oldest** queued `progress` event (`get_nowait()` + `put_nowait()`) so `deliver_observation` NEVER blocks or fails the RAG pipeline!
  - Terminal `final` or `error` is stored in `terminal_event` and wakes `terminal_ready`—guaranteeing a slow consumer **never** loses the terminal `final`/`error` event!
- **Heartbeats:** When neither `progress_queue` nor `terminal_ready` signals within `sse_heartbeat_seconds` (default `15.0s`), emit `: heartbeat\n\n` SSE comment so connections remain alive through slow LLM/GPU steps.
- **Cache-hit behavior:** Emits `accepted` -> (optional `progress` `CACHE/HIT`) -> `final` with `cache_status="HIT"`. Never fabricates `DENSE`/`BM25`/`RERANKER`/`ANSWER` events on a cache hit.
- **Client disconnect:** Generator detects disconnect via `await request.is_disconnected()` / `asyncio.CancelledError` / `GeneratorExit`, immediately cancels `query_task`, **awaits `query_task` settlement** (shielded against outer cancellation), unregisters the `InvocationEventBus` sink, and decrements session `in_flight` in `finally`.
- **Forbidden leaks:** Never streams unreviewed LLM tokens, Chain-of-Thought, system prompts, retrieved evidence text, candidate lists/scores, raw `Trace`/`SpanRecord` objects, or stack traces.

---

### D11-07 — Query Lifecycle, Cancellation & Transport Backpressure

- **Single execution path:** Both `POST /api/v1/query` and `POST /api/v1/query/stream` invoke `ApplicationServerService.execute_query(...)`, which executes `coordinator.run_query(UserTurnRequest(...))` inside an owned `asyncio.Task`.
- **Bounded transport backpressure:** `ApplicationServerService` holds `query_concurrency_semaphore = asyncio.BoundedSemaphore(server_cfg.max_concurrent_queries)` (default `16`) with immediate or short bounded acquire (`server_cfg.admission_wait_seconds`, default `0.0` or `0.25s`). If saturated, rejects with `503 RESOURCE_EXHAUSTED` before piling up unbounded HTTP tasks. `RuntimeGate` remains the sole authoritative serving/maintenance admission lock.

#### Cancellation Matrix

| Scenario | Behavior |
|---|---|
| HTTP client disconnect (`POST /query`) | Uvicorn cancels ASGI handler; service cancels `query_task` and **awaits settlement** before exiting handler so `QueryLease`, `ResourceGuard`, and `InFlightTaskRegistry` settle cleanly. |
| SSE client disconnect (`POST /query/stream`) | SSE generator exits loop, cancels `query_task`, **awaits settlement** (`try: await asyncio.shield(query_task) ...`), cleans up bus & session refcount. |
| Request timeout (`query_timeout_seconds`) | Service cancels `query_task`, awaits settlement, returns `504 DEPENDENCY_TIMEOUT` (or SSE `error` `DEPENDENCY_TIMEOUT`). |
| Cancel during embedding / reranking | `CancelledError` propagates out of `GuardedEmbeddingProvider` / `GuardedRerankerProvider`, releasing `ResourceGuard` slot in `finally`, then `QueryLease`, then `InFlightTaskRegistry`. Never force-kills threads/subprocesses untracked. |
| Cancel during DeepSeek generation | HTTPX request cancelled, `rag_core` exits before `cache_write`, `SessionQueryCache.prepare`/`commit` never called -> partial result never cached. |
| Cancel during Redis `SessionQueryCache.commit` | Preserved Stage 10 behavior (`session.py:196-198`): `_settle_write` shields the Redis `SET`/Lua `EVAL` task until transport settlement (`STORED`/`BYPASSED`/`UNKNOWN`), then re-raises `CancelledError` so `QueryLease` and `InFlightTaskRegistry` settle accurately. Completed eligible cache writes may survive subsequent client disconnect. |
| Server shutdown (`SHUTTING_DOWN`) | `gate.begin_shutdown()` rejects new work immediately; `coordinator.shutdown()` awaits `InFlightTaskRegistry` drain up to `shutdown_timeout_seconds`, then cancels remaining tasks and awaits another `shutdown_timeout_seconds` before closing resources or raising `RuntimeShutdownFailedError`. |

---

### D11-08 — Maintenance and Recovery (`sync` / `recover`)

- Exposed exclusively via Control Plane endpoints (`POST /api/v1/control/sync`, `POST /api/v1/control/recover`, `GET /api/v1/control/operations/{operation_id}`).
- Both delegate strictly to `coordinator.run_maintenance()` and `coordinator.recover()`. Never invoke `index_graph` directly.
- **Process-local `MaintenanceOperationRegistry`:**
  - Enforces single active operation at a time. Pre-checks runtime mode (`sync` requires `SERVING`, `recover` requires `BLOCKED`, returning `503 RUNTIME_NOT_READY` otherwise; `SHUTTING_DOWN` returns `503 RUNTIME_SHUTTING_DOWN`; active/draining operation returns `409 MAINTENANCE_BUSY`).
  - Spawns a server-owned `asyncio.Task` registered in `InFlightTaskRegistry` (`TaskKind.MAINTENANCE`) and returns `202 Accepted` with `operation_id` (`op_<24-hex>`).
  - **Client disconnect resilience:** Dropping the CLI/HTTP connection after `202` does **not** cancel the server-owned maintenance task; `atlasrag sync --wait` polls `GET /api/v1/control/operations/{operation_id}` until terminal state (`SUCCEEDED`, `PARTIAL_SUCCESS`, `FAILED`).
  - **Terminal retention:** Retains up to `32` recent completed operations in memory (FIFO eviction) so polling works cleanly without unbounded memory growth.
  - **`DRAINING` timeout / abort:** Preserved via `MaintenanceCoordinator` (`SERVING` restores `SERVING`; `BLOCKED` restores `BLOCKED`), recording `FAILED` + `MAINTENANCE_DRAIN_TIMEOUT` in the operation record.
  - **Shutdown during maintenance:** `InFlightTaskRegistry` drains/cancels the maintenance task under Stage 10 settlement rules; interrupted Part I work recovers via `StartupReconciliation` on next boot.
  - **Backup/Restore:** Explicitly **not** implemented in Stage 11 (preserved for Stage 14).

---

### D11-09 — Health, Status, and Stable Error Translation

#### Distinct Health Surfaces

| Endpoint | Underlying Call | Behavior in `SERVING` | Behavior in `BLOCKED` | Behavior in `DRAINING`/`MAINTENANCE` | Behavior in `SHUTTING_DOWN` |
|---|---|---|---|---|---|
| `GET /api/v1/health/live` | Event loop + `gate.mode` | `200` (`ALIVE`) | `200` (`ALIVE` — `BLOCKED` is NOT dead) | `200` (`ALIVE`) | `200` (`SHUTTING_DOWN`) |
| `GET /api/v1/health/ready` | `coordinator.readiness()` | `200` if `hard_healthy`, else `503` | `503` (`ready: false`) | `503` (`ready: false`) | `503` (`ready: false`) |
| `GET /api/v1/status` | `coordinator.status(refresh=...)` | `200` full safe status | `200` (`mode: BLOCKED`, `ready: false`, `knowledge_verified: false`) | `200` (`ready: false`) | `503 RUNTIME_SHUTTING_DOWN` |

- Liveness never triggers DB, Milvus, Redis, SearXNG, or DeepSeek calls.
- Readiness uses bounded `HealthRegistry.readiness()` (`refresh_if_stale`, no global DB verification, no DeepSeek calls).
- Soft degradation (`redis`, `searxng`, `reranker` = `UNAVAILABLE`/`DEGRADED`) leaves `hard_healthy=True` and `/health/ready=200` when `postgres`, `milvus`, `embedding`, and `knowledge_verified` are healthy.

#### Complete Error Translation Table

Reuses all 15 `ErrorCode` values from [`src/atlasrag/domain/errors.py`](../../src/atlasrag/domain/errors.py) plus minimal non-duplicative transport codes:

| Error Code | Source | HTTP Status | Public Safe Message Policy |
|---|---|---|---|
| `RUNTIME_NOT_READY` | `RuntimeNotReadyError` | `503` | Fixed `safe_message` |
| `RUNTIME_SHUTTING_DOWN` | `RuntimeShuttingDownError` | `503` | Fixed `safe_message` |
| `MAINTENANCE_BUSY` | `MaintenanceBusyError` | `409` | Fixed `safe_message` |
| `MAINTENANCE_DRAIN_TIMEOUT` | `MaintenanceDrainTimeoutError` | `409` | Fixed `safe_message` (or via operation status) |
| `RUNTIME_OWNERSHIP_CONFLICT` | `RuntimeOwnershipConflictError` | `409` | Startup exit `3`; fixed `safe_message` |
| `RUNTIME_BOOTSTRAP_FAILED` | `RuntimeBootstrapFailedError` | `500` | Startup exit `4`; never serves HTTP |
| `RUNTIME_SHUTDOWN_FAILED` | `RuntimeShutdownFailedError` | `500` | Shutdown exit `4`; lock retained |
| `DEPENDENCY_TIMEOUT` | `DependencyTimeoutError` / query timeout | `504` | Fixed `safe_message` |
| `DEPENDENCY_UNAVAILABLE` | `DependencyUnavailableError` | `503` | Fixed `safe_message` |
| `INVALID_PROVIDER_RESPONSE` | `ProviderResponseError` | `502` | Fixed `safe_message` |
| `CANONICAL_DATA_INCONSISTENT` | `CanonicalDataError` | `500` | Fixed `safe_message` |
| `INVARIANT_VIOLATION` | `InvariantViolationError` | `500` | Fixed `safe_message` |
| `RESOURCE_EXHAUSTED` | `ResourceExhaustedError` / pool/session/backpressure | `503` | Fixed `safe_message` |
| `INVALID_CONFIGURATION` | `ConfigurationError` | `500` | Startup exit `2`; redacted |
| `SOURCE_MUTATED` | `SourceMutationError` | `409` | Fixed `safe_message` |
| `INVALID_REQUEST` *(transport)* | Pydantic `ValidationError` / malformed JSON | `422` (`400` malformed JSON) | Field location + safe validation summary only |
| `PAYLOAD_TOO_LARGE` *(transport)* | Request body `> max_request_bytes` | `413` | `"Request payload exceeds allowed size limit."` |
| `UNAUTHORIZED` *(transport)* | Missing/invalid Bearer token on `/control/*` | `401` | `"Valid local control authorization is required."` |
| `FORBIDDEN` *(transport)* | Invalid `Host`, cross-origin `Origin`, missing CSRF header | `403` | `"Request rejected by local transport security policy."` |
| `SESSION_NOT_FOUND` *(transport)* | Unknown/expired/closed `session_id` | `404` | `"Session was not found or has expired."` |
| `OPERATION_NOT_FOUND` *(transport)* | Unknown/evicted `operation_id` | `404` | `"Maintenance operation was not found."` |
| `REQUEST_CANCELLED` *(transport)* | Client disconnect / task cancelled mid-stream | `499` / SSE `error` | `"Request was cancelled before completion."` |
| `INTERNAL_ERROR` *(transport)* | Unhandled unexpected exception | `500` | `"An internal server error occurred."` (never raw exception/stacktrace/DSN/path) |

---

### D11-10 — Local Transport Security

Loopback binding alone (`127.0.0.1`) is insufficient because browsers on arbitrary websites can attempt DNS rebinding or cross-origin form/fetch attacks against localhost. Stage 11 enforces defense in depth:

1. **Loopback-only binding:** `ServerConfig.host` validates via `ipaddress` to allow only loopback addresses (`127.0.0.1`, `::1`) or `"localhost"`; non-loopback bind addresses (`0.0.0.0`, LAN IPs) fail configuration validation (`ConfigurationError`).
2. **Host header validation (DNS rebinding defense):** Every request's `Host` header must match the configured exact loopback allowlist (`127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>`). Missing or mismatched `Host` -> `403 FORBIDDEN`.
3. **Same-Origin & CSRF protection (no wildcard CORS):**
   - No `CORSMiddleware` with `*`; cross-origin requests are denied by omission of `Access-Control-Allow-Origin` and actively rejected on state-changing routes.
   - Any `POST`/`DELETE`/`PUT`/`PATCH` request carrying an `Origin` header (or `Referer` when `Origin` is absent) whose scheme+host+port is not an exact match for the server's loopback origin is rejected with `403 FORBIDDEN`.
   - Browser-facing state-changing endpoints (`POST /sessions`, `DELETE /sessions/{id}`, `POST /query`, `POST /query/stream`) require `X-AtlasRAG-Client` (`"web"` or `"cli"`) so simple HTML `<form>` submissions cannot bypass CORS preflight.
4. **Privileged Control Plane Bearer Token:**
   - On `atlasrag serve` boot, generate `secrets.token_hex(32)` and atomically write to `~/.local/state/atlasrag/control.token` (`0o700` parent dir, `0o600` regular file, `O_NOFOLLOW`, UID check).
   - Every `/api/v1/control/*` route requires `Authorization: Bearer <token>` verified via `hmac.compare_digest`.
   - Knowledge Plane endpoints (`/sessions`, `/query`, `/query/stream`, `/health/*`, `/status`) and Stage 12 static Web Demo assets **never** receive, store, or expose `control.token`.
5. **Request size bounds:** HTTP body-size guard enforces `<= 65_536` bytes (`64 KiB`) before JSON parsing (`413 PAYLOAD_TOO_LARGE`), using both `Content-Length` check and streaming byte counter when chunked.

---

### D11-11 — Operational CLI and Launcher Lifecycle

Implemented with `argparse` + `httpx` in `src/atlasrag/cli/main.py` and `src/atlasrag/server/launcher.py`:

| Command | Arguments | Auth / Session | Output & Exit Codes |
|---|---|---|---|
| `atlasrag serve` | `--host 127.0.0.1`, `--port 8000`, `--config PATH`, `--server-config PATH`, `--json` | Acquires `RuntimeOwnershipLock`; creates `control.token` & `server.json` | Foreground process; `0` clean exit, `2` config error, `3` lock conflict, `4` bootstrap/shutdown failure |
| `atlasrag ask <query>` | `--session-id ID`, `--new-session`, `--file-name`, `--source-type`, `--sheet-name`, `--stream`, `--timeout`, `--json`, `--server-url` | Uses `cli_session.json` (`0o600`) or `--session-id`; sends `X-AtlasRAG-Client: cli` | Human-readable answer + citations + `[cache=HIT\|MISS, trace=..., elapsed=...ms]` or `--json`; `0`, `1`, `2`, `3` (unreachable), `130`/`143` |
| `atlasrag health` | `--live` \| `--ready` (default `--ready`), `--json`, `--server-url` | Knowledge/Host | Exit `0` when healthy/ready, `1` when `503`/unhealthy, `3` unreachable |
| `atlasrag status` | `--refresh`, `--json`, `--server-url` | Knowledge/Host | Safe status table or JSON; exit `0`, `1`, `3` unreachable |
| `atlasrag sync` | `--wait` (default `True`, `--no-wait` optional), `--poll-interval 1.0`, `--json`, `--server-url` | Reads `control.token` (`0o600`) | Polls `/operations/{id}` when `--wait`; disconnects safely if Ctrl+C (warns server continues sync); `0` on `SUCCEEDED`/`PARTIAL_SUCCESS` (prints warnings), `1` on `FAILED`/auth/busy, `3` unreachable |
| `atlasrag recover` | `--wait` (default `True`, `--no-wait` optional), `--poll-interval 1.0`, `--json`, `--server-url` | Reads `control.token` (`0o600`) | Same as `sync` against `/control/recover`; `0` only if recovery returned `SERVING` (`SAFE_TO_SERVE`), `1` otherwise |
| `atlasrag stop` | `--timeout <seconds>`, `--force`, `--json`, `--server-url` | Reads `control.token` (`0o600`) & `server.json` | Sends `POST /control/shutdown`, waits for verified server PID exit; if `--force` and server hangs past timeout, escalates `SIGTERM` -> wait -> `SIGKILL` strictly on verified `server.json` PID (`lock` inode/PID check) and exits `4`; exits `0` on clean PID exit |

#### CLI Timeout & Discovery Policy

- Discovery order: `--server-url` > `ATLASRAG_SERVER_URL` > `~/.local/state/atlasrag/server.json` (`url`) > `http://127.0.0.1:8000`.
- HTTPX timeouts: `3.0s` connect; `10.0s` read for `health`/`status`/`sessions`/`control`; `120.0s` (or `--timeout`) read for `ask` (`timeout=None` per-chunk read or generous read timeout aligned with SSE heartbeats on `--stream`).
- Thin-client rule: No subcommand other than `atlasrag serve` ever imports or instantiates `ApplicationComposition`.

---

### D11-12 — Stage 12 Web Demo Handoff

Stage 11 prepares the exact backend capabilities required by Stage 12 without building frontend assets now:

- **Serving model:** Stage 12 will mount static `HTML/CSS/JS` assets same-origin at `GET /` (via `StaticFiles` / explicit static route in `server/app.py`) — zero React, Vite, Tailwind, shadcn/ui, or Node.js toolchain.
- **Capabilities enabled by Stage 11 APIs:**
  - Question input + optional `QueryFilters` (`file_name`, `source_type`, `sheet_name`) -> `POST /api/v1/query/stream` (Fetch `ReadableStream` SSE) or `POST /api/v1/query`.
  - Safe stage/status display from `accepted` / `progress` SSE events.
  - Reviewed `FinalResponse` rendering + resolved `FinalLocalCitation` (`L1..`) / `FinalWebCitation` (`W1..`) cards.
  - Truthful `cache_status`, `elapsed_ms`, and `trace_id` badge display.
  - Live/Ready/Status panel via `GET /api/v1/health/ready` and `GET /api/v1/status`.
  - Session creation / "New Session" isolation via `POST /api/v1/sessions` and `DELETE /api/v1/sessions/{id}`, demonstrating same-session exact-request cache hits and cross-session misses.
- **Stage 12 XSS / Untrusted Content Rule:** Any Markdown renderer used in Stage 12 must escape raw HTML (`<`, `>`, `&`, `"`, `'`) and restrict link schemes to `http:` / `https:` (never rendering raw LLM HTML or `<script>`/`javascript:` URLs).
- **Maintenance:** Remains CLI-only (`atlasrag sync`, `atlasrag recover`), keeping `control.token` completely off the browser.

---

## 5. Deployment & Resource Constraints (WSL2 + RTX 3070 8GB + 16GB RAM)

- **Reference topology preserved:** Native Python 3.11 + CUDA inside WSL2 Ubuntu (`atlasrag serve`) talking over loopback to Docker Compose (`postgres:17.8-alpine`, `redis:8.2.10-alpine`, `milvusdb/milvus:v2.6.20` + `etcd` + `minio`, `searxng`).
- **Stage 11 runtime memory/resource delta (estimated, not yet measured on live GPU):**
  - FastAPI + Uvicorn (`workers=1`, pure asyncio event loop) + `SessionRegistry` (`<= 256` entries) + `MaintenanceOperationRegistry` (`<= 32` entries) + SSE queues (`<= 64` small dicts per active stream): estimated `< 50 MiB` additional RSS above existing Stage 10 runtime.
  - Zero duplicate Qwen embedding, BGE reranker, PostgreSQL pool, Milvus client, or Redis client instances.
  - Zero additional Docker containers or infrastructure services.
- **Unvalidated hardware claim guard:** Because real CUDA model loading and RTX 3070 8GB / 16GB RAM execution have not yet been benchmarked together with the server process, Stage 11 does **not** claim full-stack 8GB VRAM / 16GB RAM budget compliance without actual Stage 14 hardware validation (`§94`).

---

## 6. Explicit Stage 13 MCP Deferrals & Preserved Stage 14 Obligations

### Deferred to Stage 13 (Domain-Aware RAG MCP Server — NOT Implemented in Stage 11)

- MCP SDK / FastMCP integration, MCP Streamable HTTP / stdio endpoints, and MCP protocol-to-application session mapping (`mcp_<id>`).
- `search_domain_docs`, `search_external_evidence`, `atlasrag_ask`, `atlasrag_search`, `atlasrag_list_sources`, `atlasrag_status`.
- Domain-aware metadata schemas, PostgreSQL/Milvus domain/security-level migrations, and pre-filtering.
- Zero-trust authorization context and document/security-level ACLs.
- Evidence Mode orchestration, canonical evidence validation, structured `EvidenceEnvelope`, deterministic evidence fingerprints, permission-aware Evidence Cache, domain-level epoch/freshness semantics, SQL/document conflict detection, deterministic downstream verification, cloud embedding/reranking, and 16GB multi-domain resource budgeting.
- Stage 11 preserves a clean boundary (`ApplicationServerService` independent of FastAPI) so Stage 13 can attach an MCP adapter without refactoring Stage 11 HTTP routes or introducing speculative abstractions now.

### Preserved for Stage 14 (Deployment and Final Validation)

- Coordinated backup and restore (`§90–91`).
- Real CUDA Qwen embedding & BGE reranker initialization and RTX 3070 8GB VRAM/OOM measurement.
- Real MinerU SDK subprocess execution and cancellation settlement verification.
- Combined Web Demo + MCP concurrency, session isolation, and maintenance-exclusion integration tests.
- Transferred Stage 9 performance runner and telemetry (`S9-17`, `S9-21`) and **Final Part IV Acceptance (`§94`)**.

---

## 7. Owner Decision Register

| Decision ID | Problem | Available Alternatives | Recommended Choice | Trade-offs | Affected Contracts | Implementation Impact |
|---|---|---|---|---|---|---|
| `OD11-01` | Stage 10 `QueryResponseEnvelope`, `HealthRegistry.query_cache`, and `build_default_business` `observer` do not expose truthful `cache_status`, live query-cache readiness, or per-invocation `ProductionObservation` events. | (A) Keep Stage 10 untouched and infer/omit `cache_status` and progress events. (B) Add minimal backward-compatible Stage 10 extensions (`QueryCacheStatus` on `QueryResponseEnvelope`, `lookup_with_status`, `query_cache` callback in `HealthRegistry`, and `InvocationObserverMux` wiring in `production.py`). | **B** | Adds ~80 LOC across 4 Stage 10 files with default parameters preserving 100% existing Stage 10 test compatibility; eliminates any latency-guessing or fabricated SSE events. | `QueryResponseEnvelope`, `SessionQueryCache`, `RetrievalRunner`, `HealthRegistry`, `build_default_business` | Required in Work Packages A & B/C before truthfulness acceptance criteria can pass. |
| `OD11-02` | `GET /api/v1/status` can either trigger live probes on every request or return the bounded cached `HealthSnapshot` (`refresh_if_stale`). | (A) Always call `coordinator.health()` (active probes every request). (B) Default `refresh=False` (`refresh_if_stale()` via `health_refresh_interval_seconds=10s`) with `?refresh=true` calling `coordinator.health()`. | **B** | Prevents probe storms if Web Demo polls `/status` frequently while giving `atlasrag status --refresh` immediate active probing. | `ApplicationCoordinator.status(refresh: bool = False)` | One small method on `ApplicationCoordinator` + query param on `/api/v1/status`. |
| `OD11-03` | Should `POST /api/v1/query` require a prior `POST /api/v1/sessions` session ID or auto-create ad-hoc sessions? | (A) Auto-create sessions on unknown `session_id`. (B) Require server-issued `session_id` from `POST /api/v1/sessions`. | **B** | Requires one extra setup call (`POST /sessions`) for raw curl users (handled automatically by CLI and Web Demo), but enforces `is_cache_key_component_safe()`, `max_sessions` bounds, and Web/CLI/MCP isolation. | `SessionRegistry`, `POST /api/v1/query`, `POST /api/v1/query/stream` | Enforced in `ApplicationServerService` and `SessionRegistry`. |
