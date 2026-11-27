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

- **Accepted `main` Baseline:** `833d1150d3cc508971f2dc92b6d625718be2fe78` (`PR #11` **MERGED**)
- **Part IV Architecture Authority:** [AtlasRAG_Complete_Final_Architecture_Design_Manual.md](../architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md) & [ADR-001-part-iv-web-mcp-architecture.md](../architecture/ADR-001-part-iv-web-mcp-architecture.md) (`One RAG Engine, One Runtime Owner, Two Application Entrypoints`)
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

### Revised Stage 10–14 Allocation

| Stage | Title | Status | Scope Summary |
|---|---|---|---|
| `10` | Application Runtime Foundation | **COMPLETE / ACCEPTED / MERGED** | In-process `ApplicationComposition`, `ApplicationCoordinator`, ownership lock, gate, freshness barrier, caches (`PR #11` merged at `833d1150`) |
| `11` | Shared Application Server | `NOT_STARTED` | Long-lived loopback runtime-owning server, thin Web HTTP/SSE adapter, inbound MCP extension boundary, thin CLI/control boundary, health/readiness, OS signals & launcher termination |
| `12` | Web Application | `NOT_STARTED` | Human-facing browser QA app (sessions, multi-turn display, Markdown/code/tables/math, citations, authorized source view, SSE status streaming, filters, controlled maintenance) |
| `13` | MCP Server | `NOT_STARTED` | Inbound agent-facing MCP adapter (`atlasrag_ask` + proposed `atlasrag_search`, `atlasrag_list_sources`, `atlasrag_status`, read-only default, Answer Mode + detailed design of Evidence Mode) |
| `14` | Deployment and Final Validation | `NOT_STARTED` | Coordinated backup/restore, Windows/WSL2/RTX 3070 8GB validation, real Qwen/BGE CUDA & MinerU subprocess settlement, dual-entrypoint Web+MCP tests, transferred Stage 9 `S9-17`/`S9-21` performance |

Operational CLI remains a thin control/user client, never a third independent runtime. Stage 9 remains closed under its amended acceptance scope (`12/12` raw artifacts preserved and verified), and Stage 11 has **not** started.
