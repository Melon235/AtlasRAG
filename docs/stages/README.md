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

- **Stage:** Stage 3 — Part I: Parse & Canonical Build
- **Status:** Stage 3 COMPLETE — pull request #4 open and unmerged
- **Report:**
  [stage-03-part-i-parse-canonical-build.md](stage-03-part-i-parse-canonical-build.md)
- **Next:** Stage 4 — Part I: Index & Publish — NOT STARTED

Stage 3 implements secure source identity, deterministic parser normalization,
canonical elements and Parent/Child/Table chunks, typed persistence mapping,
and atomic candidate boundaries through `CHUNKED`. Its final local offline,
Stage 2 service, Stage 3 PostgreSQL/Docling, and real MinerU gates are green.
The branch is published normally, all push- and PR-triggered remote gates are
green, and pull request #4 remains deliberately unmerged. No embedding, Milvus
write, publication, build graph, directory sync, Part II, or benchmark behavior
has started.
