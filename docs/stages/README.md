# Stage workflow

AtlasRAG is implemented as a sequence of explicitly scoped stages. Each stage:

1. starts from the repository's default branch on a dedicated `stage/*` branch;
2. follows the Architecture Manual and changes only its declared scope;
3. adds tests and an evidence-bearing stage report;
4. runs the stable repository verification gate;
5. is committed and published without force-pushing; and
6. is reviewed independently before any merge into the default branch.

Local implementation does not make a stage complete. Completion additionally
requires its report, commit, successful GitHub publication, and—when available—a
green remote CI result.

## Current stage & Part IV roadmap

- **Accepted `main` Baseline:** `9cd2344f2076d60ee53568eb35aaa625474dc645` (`PR #11` & `PR #12` **MERGED**)
- **Part IV Architecture Authority:** [AtlasRAG_Complete_Final_Architecture_Design_Manual.md](../architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md), [ADR-001-part-iv-web-mcp-architecture.md](../architecture/ADR-001-part-iv-web-mcp-architecture.md), and [ADR-002-part-iv-web-demo-and-mcp-milestone-refinement.md](../architecture/ADR-002-part-iv-web-demo-and-mcp-milestone-refinement.md) (`One RAG Engine, One Runtime Owner`; Milestone 1: Stages 11–12 Web Demo RAG Acceptance; Milestone 2: Stages 13–14 Domain-Aware RAG MCP Server & Final Part IV Integration)
- **Stage 10 Closeout & Acceptance Records:**
  - [stage-10-part-iv-application-runtime.md](stage-10-part-iv-application-runtime.md)
  - [stage-10a-completion.md](stage-10a-completion.md)
  - [stage-10a-verification-ledger.md](stage-10a-verification-ledger.md)
  - [stage-10b-completion.md](stage-10b-completion.md)
  - [stage-10-acceptance.md](stage-10-acceptance.md)
  - [stage-10-review/STAGE_10_C3_R1_REVIEW.md](stage-10-review/STAGE_10_C3_R1_REVIEW.md)
  - [stage-10-review/REVIEW_SUMMARY.md](stage-10-review/REVIEW_SUMMARY.md)
  - [stage-10-closeout/CLOSEOUT_REPORT.md](stage-10-closeout/CLOSEOUT_REPORT.md)
  - [stage-10-closeout/ACCEPTANCE_AUDIT.md](stage-10-closeout/ACCEPTANCE_AUDIT.md)
  - [stage-10-closeout/STAGE_11_HANDOFF.md](stage-10-closeout/STAGE_11_HANDOFF.md)
- **Stage 11 Design & Planning Records (`READY_FOR_OWNER_REVIEW` — implementation `NOT_STARTED`):**
  - [stage-11-local-application-server.md](stage-11-local-application-server.md)
  - [stage-11-acceptance.md](stage-11-acceptance.md)
  - [2026-11-27-stage-11-local-application-server.md](../superpowers/plans/2026-11-27-stage-11-local-application-server.md)

### Revised Stage 10–14 Allocation (Refined via ADR-002)

| Stage | Title | Status | Scope Summary |
|---|---|---|---|
| `10` | Application Runtime Foundation | **COMPLETE / ACCEPTED / MERGED** | In-process `ApplicationComposition`, `ApplicationCoordinator`, ownership lock, gate, freshness barrier, caches (`PR #11` merged at `833d1150`) |
| `11` | Local Application Server | `DESIGN_READY / IMPLEMENTATION_NOT_STARTED` | Long-lived loopback runtime-owning FastAPI + Uvicorn server (`atlasrag serve`), `/api/v1/*` HTTP/SSE contract, process-local sessions & operations, local transport auth, thin operational CLI, OS signals & launcher termination, framework-independent service boundary |
| `12` | Web Demo and Existing RAG Acceptance | `NOT_STARTED` | Lightweight same-origin Web Demo (static HTML/CSS/JS, Fetch, safe Markdown, minimal SSE) demonstrating & accepting the existing complete RAG pipeline; maintenance remains CLI-only |
| `13` | Domain-Aware RAG MCP Server | `DEFERRED` | Separately designed domain-aware inbound MCP retrieval module (`search_domain_docs`, `search_external_evidence`, zero-trust ACLs/metadata pre-filtering, canonical evidence validation, permission-aware Evidence Cache, domain epochs) |
| `14` | Deployment and Final Integration | `NOT_STARTED` | Coordinated backup/restore, Windows/WSL2/RTX 3070 8GB validation, real Qwen/BGE CUDA & MinerU subprocess settlement, dual-entrypoint Web+MCP tests, transferred Stage 9 `S9-17`/`S9-21` performance, Final Part IV Acceptance (`§94`) |

Operational CLI remains a thin control/user client, never a third independent runtime. Stage 9 remains closed under its amended acceptance scope (`12/12` raw artifacts preserved and verified), and Stage 11 has **not** started.
