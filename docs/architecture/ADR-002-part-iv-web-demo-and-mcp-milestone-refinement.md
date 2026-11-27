# ADR-002: Part IV Owner-Approved Scope Refinement — Lightweight Web Demo & MCP Milestone Separation

- **Status:** Owner-Approved Scope Refinement (`READY_FOR_OWNER_REVIEW` alongside Stage 11 Detailed Design)
- **Baseline (`origin/main`):** `9cd2344f2076d60ee53568eb35aaa625474dc645` (`PR #11` & `PR #12` **MERGED**)
- **Amends / Refines:** [`ADR-001-part-iv-web-mcp-architecture.md`](ADR-001-part-iv-web-mcp-architecture.md)
- **Primary Authority Referenced:** [`AtlasRAG_Complete_Final_Architecture_Design_Manual.md`](AtlasRAG_Complete_Final_Architecture_Design_Manual.md)

---

## 1. Context & Clarified Product Direction

Following completion and acceptance of Stage 10 (`Application Runtime Foundation`, `PR #11` merged at `833d1150`) and acceptance of `ADR-001` (`PR #12` merged at `d13776a1`), the owner clarified the long-term product direction and two-milestone delivery order for Part IV:

1. **Primary Long-Term Product Purpose:** AtlasRAG's primary long-term role is to serve as a **domain-aware RAG MCP retrieval module** for a future multi-domain data platform.
2. **Role of the Web Application:** The Web application is a **lightweight demonstration** of the existing complete Part I/II RAG pipeline, not a full conversational product or comprehensive knowledge-management platform.
3. **Delivery Milestone Separation:**
   - **Milestone 1 (Stages 11–12 — Existing RAG Acceptance):** Build the minimal, reliable Local Application Server (`Stage 11`) wrapping the accepted Stage 10 runtime and a lightweight same-origin Web Demo (`Stage 12`) to demonstrate and accept the existing complete RAG pipeline.
   - **Milestone 2 (Stages 13–14 — Domain-Aware MCP & Final Part IV Acceptance):** Separately design and implement the domain-aware RAG MCP retrieval service (`Stage 13`) and complete final deployment, backup/restore, real-model/GPU validation, and transferred Stage 9 performance obligations (`Stage 14`).

---

## 2. Owner-Approved Scope Refinement to ADR-001

Without modifying or weakening any frozen Part I, Part II, Part III, or Stage 10 contract, the Stage 11–14 roadmap and Web/CLI scope are refined as follows:

| Stage | ADR-001 Label | Refined Label | Refined Scope Summary |
|---|---|---|---|
| `10` | Application Runtime Foundation | Application Runtime Foundation | **COMPLETE / ACCEPTED / MERGED** (`833d1150`) — unchanged |
| `11` | Shared Application Server | **Local Application Server** | Minimal, reliable single-runtime-owning FastAPI + Uvicorn server (`atlasrag serve`), versioned `/api/v1/*` HTTP/SSE contract, process-local session & operation registries, local transport authorization, thin `argparse` + `httpx` operational CLI, and OS signal/launcher lifecycle. Preserves a framework-independent service boundary for Stage 13 MCP extension without implementing MCP now |
| `12` | Web Application | **Web Demo and Existing RAG Acceptance** | Lightweight same-origin Web Demo using static HTML, CSS, JavaScript, Fetch API, safe minimal Markdown rendering, and minimal SSE support. No React/Vite/Tailwind/shadcn/Node.js toolchain, no persistent conversation history, no user account system, and no Web knowledge-management console; operational CLI remains sufficient for maintenance |
| `13` | MCP Server | **Domain-Aware RAG MCP Server** | Separately designed and implemented inbound domain-aware MCP retrieval service (`search_domain_docs`, `search_external_evidence`, domain-aware metadata, zero-trust security-level authorization, Milvus/PG pre-filtering, canonical evidence validation, deterministic evidence fingerprints, permission-aware Evidence Cache, domain epoch freshness, 16GB single-machine constraints) |
| `14` | Deployment and Final Validation | **Deployment and Final Integration** | Coordinated backup/restore (`§90–91`), Windows Host + WSL2 Ubuntu + RTX 3070 8GB real Qwen/BGE CUDA & MinerU subprocess validation, dual-entrypoint Web Demo + MCP concurrency/isolation tests, transferred Stage 9 `S9-17` / `S9-21` performance verification, and **Final Part IV Acceptance (`§94`)** |

### Explicit Web Demo Simplifications (Milestone 1)

- **No persistent conversation history** in PostgreSQL or browser backend storage.
- **No complex frontend framework or Node.js build pipeline** required (`static HTML/CSS/JS + Fetch + minimal safe Markdown + SSE`).
- **No user account system** or multi-user login UI.
- **No comprehensive Web knowledge-management console** (operational CLI `atlasrag sync` / `atlasrag recover` is sufficient for maintenance).
- **Zero RAG semantic changes:** `UserTurnRequest`, `QueryFilters`, `FinalResponse`, citations, `SessionQueryCache` key/freshness/fencing, and `ParentContextService` (`TEXT_PARENT` only, canonical `TABLE`) remain strictly unchanged.

---

## 3. Milestone Distinction: Stage 12 RAG Demo Acceptance vs. Final Part IV Acceptance

To prevent premature acceptance claims, Part IV formally separates two distinct acceptance gates:

1. **Stage 12 Web Demo & Existing RAG Acceptance Milestone:**
   - Demonstrates end-to-end execution of the existing Part I/II RAG pipeline through the Stage 11 Local Application Server and Stage 12 same-origin Web Demo: question input, safe stage/status SSE events, reviewed `FinalResponse`, resolved Local/Web citations, truthful `cache_status`, `elapsed_ms`, `trace_id`, health/readiness display, new session creation, same-session exact-request cache hit, and cross-session cache isolation.
   - Does **not** by itself claim completion of Stage 13 MCP capabilities or Stage 14 deployment/hardware/backup obligations.
2. **Final Part IV Acceptance (`§94` — Stage 14 Milestone):**
   - Preserves **all** previously frozen Part IV obligations, explicitly tracking deferred items to their owning stages:
     - **Deferred to Stage 13:** Inbound MCP protocol/SDK (`atlasrag_ask` / `search_domain_docs` / `search_external_evidence`), domain-aware metadata/ACLs, Evidence Mode, Evidence Publication Boundary, Evidence Cache, Domain Epoch, Web+MCP session isolation.
     - **Deferred to Stage 14:** Coordinated backup and restore (`§90–91`), real Qwen/BGE CUDA weight loading and RTX 3070 8GB VRAM/OOM dual-session validation, MinerU SDK subprocess settlement verification, Web + MCP combined-entrypoint concurrency/maintenance-exclusion validation, and transferred Stage 9 performance requirements (`S9-17`, `S9-21`).

---

## 4. Preserved Invariants

All frozen Stage 1–10 and Architecture Manual (`§1–§102`) invariants remain binding:

- One `ApplicationComposition`, one `ApplicationCoordinator`, one `RuntimeOwnershipLock`, one `RuntimeGate`, one `InFlightTaskRegistry`, one `ResourceGuard`, one PostgreSQL pool, one `MilvusIndexProvider`, one `RedisCacheProvider`, and one set of loaded GPU models.
- Exact Session Query Cache key `atlasrag:query:{session_id}:{runtime_fingerprint}:{request_hash}`, raw request hashing before Query Construction, `CacheFreshnessBarrier`, Lua write fencing, and fresh `trace_id` per invocation.
- No unreviewed token streaming, no persisted Chain-of-Thought, no secrets in logs/responses/fingerprints, and no `benchmarks/` or `tests/` imports in production code.
