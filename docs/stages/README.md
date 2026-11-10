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

- **Stage:** Stage 4 — Part I: Index & Publish
- **Status:** Stage 4 COMPLETE — pull request #5 open and unmerged
- **Report:**
  [stage-04-part-i-index-publish.md](stage-04-part-i-index-publish.md)
- **Next:** Stage 5 — Part I: Sync / Recovery / Maintenance — NOT STARTED

Stage 4 implements lossless representation construction, pinned local Qwen
embeddings, bounded Milvus candidate indexing, independent exact-set
verification with invocation-bound proof, atomic PostgreSQL publication through
`READY`, and an eight-node LangGraph document build subgraph with a whole-build
advisory lease. Its final local offline, Stage 2 service, Stage 3
PostgreSQL/Docling, real MinerU, Stage 4 PostgreSQL/Milvus, and real Qwen CUDA
gates are green. The branch is published normally, all push- and PR-triggered
remote gates are green, and pull request #5 remains deliberately unmerged. No
directory sync, reconciliation, runtime maintenance, Redis cache invalidation
execution, Part II, or benchmark behavior has started.
