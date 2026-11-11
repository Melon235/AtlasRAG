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

- **Stage:** Stage 6 — Part II: Local Evidence
- **Status:** Stage 6 IN PROGRESS — publication and final remote gates pending
- **Report:**
  [stage-06-part-ii-local-evidence.md](stage-06-part-ii-local-evidence.md)
- **Next:** Stage 7 — Part II: Web Evidence & Answer — NOT STARTED

Stage 6 implements the shared published retrieval scope, Dense and BM25
retrieval, deterministic RRF, pinned BGE reranking with degradation, unique
evidence selection, canonical PostgreSQL Parent/Table expansion, deterministic
sufficiency, and the six-node `local_evidence_subgraph`. Its real
PostgreSQL/Milvus and real Qwen/BGE model gates are green; remote publication is
pending. No Query Construction, RagCore retry/routing, Web evidence, Answer
generation, Query Cache, RuntimeGate, Stage 7, or benchmark behavior has
started.
