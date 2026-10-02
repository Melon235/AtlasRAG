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

- **Stage:** Stage 2 — Infrastructure & Persistence
- **Status:** Stage 2 IMPLEMENTATION VERIFIED — PUBLICATION PENDING
- **Report:**
  [stage-02-infrastructure-persistence.md](stage-02-infrastructure-persistence.md)
- **Next:** Stage 3 — Part I: Parse & Canonical Build — NOT STARTED

Stage 2 is limited to local infrastructure, canonical persistence records and
repositories, technical cache/index providers, readiness/migration tooling,
and deterministic integration verification. Its offline and real-service
local gates are green; normal branch publication and both remote workflows are
pending. No parser, chunk builder, embedding pipeline, business graph,
RuntimeGate, benchmark, or other Stage 3+ behavior has started.
