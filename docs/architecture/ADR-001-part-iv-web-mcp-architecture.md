# ADR-001: Part IV Architecture Amendment — Web Application and MCP Server

- **Status:** Owner-Approved Part IV Direction / Documentation Candidate (`READY_FOR_OWNER_APPROVAL` on `docs/part-iv-web-mcp-architecture`)
- **Baseline (`origin/main`):** `833d1150d3cc508971f2dc92b6d625718be2fe78` (`PR #11` merged)
- **Stage 10 Reviewed Implementation / Closeout:** `be5a287601ea988fe3b79dd4fed2c7aaafc4a7a8` / `ad96d23fbe8a4539352fd70a83aac713a89d63b8`
- **Primary Authority Updated:** [`AtlasRAG_Complete_Final_Architecture_Design_Manual.md`](AtlasRAG_Complete_Final_Architecture_Design_Manual.md)

---

## 1. Context & Owner-Approved Direction

Stage 10 (`Application Runtime Foundation`) is complete, independently verified (`23/23` requirements satisfied, `6/6` blocking findings closed, `10/10` historical gaps closed, `AR4-T01 = OFFLINE_AND_REAL_INFRA_VERIFIED`, `12/12` Stage 9 raw artifact integrity), and merged into `main` (`833d1150d3cc508971f2dc92b6d625718be2fe78`).

To deliver both human-facing and agent-facing knowledge access cleanly on top of the completed runtime foundation, the owner approved revising Part IV around one invariant:

> **One RAG Engine, One Runtime Owner, Two Application Entrypoints**

1. **AtlasRAG Web** — a human-facing browser knowledge-base QA application.
2. **AtlasRAG MCP Server** — an agent-facing inbound MCP knowledge and RAG adapter (explicitly distinct from the existing outbound SearXNG MCP/Web provider integration).

This amendment preserves all Part I, Part II, and Part III contracts as well as the completed Stage 10 in-process runtime architecture.

---

## 2. Conceptual Topology

```text
AtlasRAG Web                    MCP Clients
      |                              |
Web HTTP API / SSE                MCP Adapter
      |                              |
      +------ Shared Application ----+
                    Server
                      |
             ApplicationCoordinator
                      |
              Single Application
                   Runtime
                      |
          Part I / Part II Services
                      |
      PostgreSQL / Milvus / Redis /
        Qwen / BGE / SearXNG
```

Both entrypoints share one `ApplicationComposition`, one `ApplicationCoordinator`, one `RuntimeOwnershipLock`, one `RuntimeGate`, one `InFlightTaskRegistry`, one PostgreSQL pool, one `MilvusIndexProvider`, one `RedisCacheProvider`, one set of loaded GPU models, one `ResourceGuard`, one canonical knowledge base, and one maintenance/recovery lifecycle. Independent Web and MCP business runtimes are explicitly prohibited.

---

## 3. Previous vs Revised Part IV Stage Allocation

| Stage | Previous Allocation | Revised Allocation |
|---|---|---|
| `10` | Application Runtime Foundation | Application Runtime Foundation |
| `11` | Local Server and Operational CLI | Shared Application Server |
| `12` | Backup and Restore | Web Application |
| `13` | Web UI and Safe Streaming | MCP Server |
| `14` | Final Integration and Performance | Deployment and Final Validation |

- **Stage 10 (Application Runtime Foundation — `COMPLETE / ACCEPTED / MERGED` at `833d1150d3cc508971f2dc92b6d625718be2fe78`):** Preserved completely without changing historical Stage 10 acceptance requirements (`S10-01`–`S10-20`) or independent review records.
- **Stage 11 (Shared Application Server — `NOT_STARTED`):** Long-lived runtime-owning local server (`atlasrag serve` conceptual entrypoint, with Web/MCP adapters never instantiating `ApplicationComposition`), thin Web HTTP/SSE adapter boundary, inbound MCP protocol adapter extension boundary, thin operational CLI/control interface, request validation, error translation, session identity ownership, cancellation/disconnect handling, health/liveness/readiness endpoints, Local Transport Authorization (Host/Origin/DNS-rebinding, explicit local control auth, browser state-changing protections), and OS signal/launcher termination boundary. Exact CLI flags, routes, and schemas belong to Stage 11 design.
- **Stage 12 (Web Application — `NOT_STARTED`):** Browser-based human QA application (sessions, multi-turn display, Markdown/code/table/math rendering, Local/Web citation resolution and presentation, authorized source inspection, SSE stage/status streaming, reviewed `FinalResponse` publication, query options/metadata filters, safe cache/trace summaries, runtime health/readiness view, controlled maintenance triggers).
- **Stage 13 (MCP Server — `NOT_STARTED`):** Inbound agent-facing MCP adapter (`atlasrag_ask` primary full-RAG tool preserving `FinalResponse` / citation resolution plus proposed read-only `atlasrag_search`, `atlasrag_list_sources`, `atlasrag_status`), Answer Mode reuse, and detailed design of proposed Evidence Mode with its dedicated Evidence Publication Boundary and `RuntimeGate` enforcement.
- **Stage 14 (Deployment and Final Validation — `NOT_STARTED`):** Backup and Restore remain mandatory Stage 14 scope (never removed), alongside Windows Host + WSL2 Ubuntu + native CUDA/RTX 3070 8GB local deployment, real Qwen/BGE CUDA weight verification, MinerU SDK subprocess settlement verification, Web + MCP combined-entrypoint concurrency/isolation/maintenance-exclusion validation, and transferred Stage 9 performance verification (`S9-17`, `S9-21`).

---

## 4. Affected Architecture Manual Sections

Reconciled in [`AtlasRAG_Complete_Final_Architecture_Design_Manual.md`](AtlasRAG_Complete_Final_Architecture_Design_Manual.md) with existing section numbers `§1`–`§102` preserved:

- **Document Header & `§73–76`:** Updated Part IV topology and ownership rules for the Shared Application Server serving Web and inbound MCP over one `ApplicationComposition` / `ApplicationCoordinator`; preserved `atlasrag serve` conceptual startup entrypoint (adapters never instantiate `ApplicationComposition`), preserved the `langgraph dev` development-only constraint (`§75`), and fixed PG pool sizing erratum wording.
- **Added Subsections `§76.1–76.5`:** Defined Shared Application Server including Stage 11 Local Transport Authorization constraints (`§76.1`), AtlasRAG Web (`§76.2`), Inbound AtlasRAG MCP Server (`§76.3`), Answer Mode vs proposed Evidence Mode & its Stage 13 Evidence Publication Boundary (`§76.4`), and Proposed Technology Direction (`§76.5`).
- **`§84–85`:** Preserved `atlasrag:query:{session_id}:{runtime_fingerprint}:{request_hash}`, PG freshness marker, Lua write fencing, and `TEXT_PARENT` Parent Context Cache; mandated disjoint Web/MCP application-session identities and excluded Evidence Mode outputs from the `FinalResponse` Query Cache.
- **`§88–92` & `§94`:** Reconciled `QueryResponseEnvelope` vs Evidence Mode boundary (`§88`), Web SSE + MCP safe progress notifications (`§89`), Stage 14 allocation for coordinated backup/restore (`§90–91`), private-trace non-exposure rules (`§92`), and restored/extended `§94` Local/Web citation resolution acceptance across both Answer Mode entrypoints alongside proposed Evidence Mode provenance.
- **`§99–102`:** Reconciled v1 out-of-scope boundaries (including read-only MCP default, no automatic transcript injection into RAG semantics, no multi-tenant RBAC/LAN hosting), Part IV status (`§100`), deferred design items (`§101`), and Final Architecture Statement (`§102`).

---

## 5. Preserved Frozen Contracts & Documentation Erratum

- **Frozen Part I–III & Stage 10 Contracts:** Domain models (`UserTurnRequest`, `FinalResponse`, citations), Part II RAG/Review/Citation pipeline, `RuntimeMode` / `RuntimeGate` / `InFlightTaskRegistry` / `ResourceGuard` / `HealthRegistry` / `RuntimeOwnershipLock`, exact Query Cache key (`atlasrag:query:{session_id}:{runtime_fingerprint}:{request_hash}`) with raw request hashing before Query Construction, `CacheFreshnessBarrier`, `ParentContextService` (`TEXT_PARENT` only, canonical `TABLE`), and unique `trace_id` per invocation (including cache hits).
- **Documentation Erratum Resolved:** Earlier Stage 10 handoff/architecture notes misstated `2 * max_document_concurrency + 2` as `postgres_pool_min_size` ("Pool minimum"). The actual validated contract in `src/atlasrag/config/models.py` (`RuntimeConfig._validate_operational`) is:
  - `postgres_pool_max_size >= 2 * max_document_concurrency + 2`
  - `postgres_pool_min_size <= postgres_pool_max_size`
  - Preserved validated defaults: `postgres_pool_min_size = 2`, `postgres_pool_max_size = 16`.

---

## 6. Proposed Technology Direction (Not Yet Pinned)

| Component | Proposed Technology |
|---|---|
| Application Server | FastAPI + Uvicorn |
| Web Frontend | React + TypeScript |
| Frontend Build | Vite |
| UI Styling | Tailwind CSS + shadcn/ui |
| Web Streaming | SSE |
| MCP | Official Python MCP SDK / FastMCP |
| MCP Primary Transport | Streamable HTTP |
| MCP Compatibility | Optional stdio thin adapter (to existing server/runtime owner only) |
| Application Runtime | Existing Python / LangGraph implementation |

No dependencies or lockfiles (`pyproject.toml`, `uv.lock`) are modified by this amendment. The existing `langgraph dev` development/local-dev boundary remains intact and is not invalidated by the proposed FastAPI + Uvicorn server direction.

---

## 7. Decisions Explicitly Deferred to Stage 11–14 Detailed Designs

- **Stage 11:** Exact `atlasrag serve`/CLI arguments and launcher/PID/OS signal lifecycle, HTTP routes/payloads, Local Transport Authorization rules (beyond loopback binding: Host/Origin validation, DNS rebinding protection, explicit local caller authorization for privileged control operations, separation of knowledge vs control authority, and browser protections for state-changing requests), disconnect cancellation propagation, and Web/CLI session/control transport schemas.
- **Stage 12:** Frontend UI architecture, React/shadcn component layout, Web multi-turn session storage/persistence model, source-viewer formatting, and maintenance UX.
- **Stage 13:** MCP protocol-to-application `session_id` mapping, exact schemas for `atlasrag_ask` / `atlasrag_search` / `atlasrag_list_sources` / `atlasrag_status`, and Evidence Mode + dedicated Evidence Publication Boundary specification (authorized source access, canonical evidence/provenance validation, bounded result sizes, safe serialization, private trace/internal exclusion, untrusted retrieved-content handling, service reuse, stop point, Local/Web behavior, Query Construction rules, cancellation/degradation, and cache policy without bypassing `RuntimeGate` or using the `FinalResponse` Session Query Cache).
- **Stage 14:** Backup/restore implementation mechanics, real Qwen/BGE CUDA + MinerU subprocess RTX 3070 8GB validation, Web+MCP concurrency/isolation/maintenance-exclusion integration tests, and Stage 9 `S9-17` / `S9-21` performance verification.
