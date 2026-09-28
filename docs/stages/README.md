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

- **Stage:** Stage 1 — Contracts & Shared Kernel
- **Status:** Stage 1 COMPLETE
- **Report:**
  [stage-01-contracts-shared-kernel.md](stage-01-contracts-shared-kernel.md)
- **Next:** Stage 2 — Infrastructure & Persistence — NOT STARTED

Stage 1 is limited to typed contracts, shared validation primitives,
declarative graph State schemas, configuration schemas, fingerprints, request
hashing, and trace contracts. Its local gates, normal branch publication, and
remote implementation CI are green. Stage 2 providers, infrastructure,
persistence, and runtime graph execution have not started.
