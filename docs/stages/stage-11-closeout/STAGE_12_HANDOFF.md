# Stage 12 Handoff — Web Demo and Existing RAG Acceptance

- **Predecessor Stage:** Stage 11 — Local Application Server (`COMPLETE / READY_FOR_PR_REVIEW` on `stage/11-local-application-server` at `0789687`)
- **Accepted `main` Baseline Prior to Stage 11 PR:** `9cd2344f2076d60ee53568eb35aaa625474dc645`
- **Next Planned Stage:** Stage 12 — Web Demo and Existing RAG Acceptance (`NOT_STARTED`)
- **Stage 13 MCP Server Status:** **`DEFERRED`** (do not start MCP tools/ACLs/Evidence Cache in Stage 12)
- **Authorization Notice:** This handoff records Stage 11 interfaces and Stage 12 acceptance boundaries per `ADR-002` and does **not** authorize Stage 12 implementation.

---

## 1. Completed Stage 11 Server Contracts Consumed by Stage 12

1. **Loopback Server Process & Lifecycle ([`launcher.py`](../../../src/atlasrag/server/launcher.py), [`main.py`](../../../src/atlasrag/cli/main.py)):**
   - One long-lived `atlasrag serve` Uvicorn process (`workers=1`, `reload=False`, `127.0.0.1:<port>`) holds the sole `ApplicationComposition`, `ApplicationCoordinator`, and per-UID `RuntimeOwnershipLock`.
   - `server.json` (`0o600`, UID-owned) advertises the running loopback URL; `shutdown-<server_instance_id>.json` (`CLEAN_SHUTDOWN`) is committed atomically post-release per `D11-02-AMENDMENT-01`.
2. **Versioned Knowledge API (`/api/v1/*`, [`app.py`](../../../src/atlasrag/server/app.py), [`schemas.py`](../../../src/atlasrag/server/schemas.py)):**
   - `POST /api/v1/sessions` (`client_kind: "web"`): issues bounded `web_<hex>` session IDs (`201` `SessionResponse`).
   - `DELETE /api/v1/sessions/{session_id}`: deletes session (`204`; marks `closed` without cancelling/corrupting in-flight queries and without touching Redis).
   - `POST /api/v1/query` & `POST /api/v1/query/stream`: require valid issued `session_id` (`404 SESSION_NOT_FOUND` otherwise) and `X-AtlasRAG-Client: web` + validated loopback `Host` & `Origin`; enforce `64 KiB` request body limit and verbatim `FinalResponse` preservation.
   - `POST /api/v1/query/stream` emits `accepted` (post-admission, pre-retrieval) -> allowlisted `progress` stages (`<= 1_024` bytes) -> terminal `final` or `error` + `: heartbeat`; returns `503` JSON pre-admission when unready (`BLOCKED`/`MAINTENANCE`/`SHUTTING_DOWN`).
   - `GET /api/v1/health/live`, `GET /api/v1/health/ready`, `GET /api/v1/status` (`?refresh=true` single-flight & cooldown-bounded): public loopback observability endpoints exposing mode, dependency health, and `query_cache` state without leaking credentials, prompts, or internal keys.

---

## 2. Constraints Stage 12 Must Preserve

- **Same-Origin Static UI (`GET /`) & Safe Browser Rendering (`D11-12`):**
  - Serve static HTML/CSS/JS same-origin from `app.py` (`GET /`), communicating over Fetch + `POST /api/v1/query/stream` (`ReadableStream` SSE parser since browser `EventSource` only sends `GET`).
  - Escape raw HTML (`<`, `>`, `&`, `"`, `'`) in answers, citations, and titles; allow only `http:` / `https:` links (`javascript:` / `data:` / inline scripts forbidden).
- **Maintenance Remains CLI-Only:**
  - `control.token` and `/api/v1/control/{sync,recover,shutdown,operations/*}` must **never** be exposed to browser JavaScript or Web Demo UI controls.
- **Zero Semantic Mutation:**
  - Do **not** weaken existing Part I/II retrieval, indexing, citation, `QueryCache`, or `ParentContextService` contracts.

---

## 3. Mandatory Bounded Real-Model, Real-KB Acceptance in Stage 12 (`ADR-002`)

Per [`ADR-002`](../../architecture/ADR-002-part-iv-web-demo-and-mcp-milestone-refinement.md) and [`D11-12`](../stage-11-local-application-server.md), Stage 12 acceptance **cannot** rely on fake-model transport tests alone and must include a bounded real-model, real-knowledge-base end-to-end demonstration verifying:
- Real Qwen embedding + real selected BGE reranker + real answer generation over real PostgreSQL, Milvus, and Redis.
- Real Local (`[L*]`) and Web (`[W*]`) citation rendering and `MISS -> HIT` + cross-session isolation on real Redis Query Cache.
- Unchanged preservation of deferred Stage 13 (MCP, domain metadata/ACLs, Evidence Cache, Domain Epochs) and Stage 14 (backup/restore, full RTX 3070 VRAM/OOM stress, MinerU cancellation settlement, transferred Stage 9 performance/ablation gates).
