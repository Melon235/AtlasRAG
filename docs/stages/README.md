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

## Current stage

- **Stage:** Stage 8 — Part II: RagCore / Cache
- **Status:** Stage 8 IMPLEMENTATION COMPLETE — live end-to-end acceptance pending
- **Report:**
  [stage-08-part-ii-rag-core-cache.md](stage-08-part-ii-rag-core-cache.md)
- **Next:** Stage 9 — Part III: Benchmark — NOT STARTED

Stage 7 implements SearXNG JSON search, SSRF-safe fetch/sanitize/budgets,
direct and batch-summary Web evidence strategies, structured DeepSeek
generation, identity compression, deterministic output review, bounded
regeneration, and authoritative citation resolution as two independent
subgraphs. Local transport and Stage 2–6 regression gates are green; all push/PR remote
gates are green; real SearXNG and DeepSeek generation/batch-summary acceptance
also pass, and PR #8 remains deliberately unmerged. No RagCore orchestration,
Query Cache, RuntimeGate, Stage 8, or benchmark behavior has started.
