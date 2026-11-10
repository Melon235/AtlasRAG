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

- **Stage:** Stage 5 — Part I: Sync / Recovery / Maintenance
- **Status:** Stage 5 IN PROGRESS — publication and final remote gates pending
- **Report:**
  [stage-05-part-i-sync-recovery-maintenance.md](stage-05-part-i-sync-recovery-maintenance.md)
- **Next:** Stage 6 — Part II: Local Evidence — NOT STARTED

Stage 5 implements complete Knowledge Directory scanning, immutable sync
planning, bounded whole-directory build execution, durable PARSED/CHUNKED
recovery, startup reconciliation, exact-set current-index repair, Milvus-first
missing-source deletion, and global published-knowledge verification with
separate SyncOutcome/ServingSafety verdicts. Its real PostgreSQL/Milvus
integration gates are green; remote publication is pending. No RuntimeGate,
runtime lifecycle, Redis invalidation execution, Part II, or benchmark behavior
has started.
