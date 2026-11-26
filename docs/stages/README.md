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

- **Stage:** Stage 10 — Part IV: Application Runtime Foundation (`stage/10-part-iv-application-runtime`, `PR #11` OPEN/UNMERGED)
- **Status:** Stage 10-A (`A-R1`–`A-R6`) COMPLETE offline; Stage 10-B (isolated PostgreSQL/Milvus/Redis integration) and Stage 10-C (independent review) NOT_STARTED; Stage 10 overall PARTIAL / acceptance UNSATISFIED
- **Report & Matrices:**
  - [stage-10-part-iv-application-runtime.md](stage-10-part-iv-application-runtime.md)
  - [stage-10a-completion.md](stage-10a-completion.md)
  - [stage-10a-verification-ledger.md](stage-10a-verification-ledger.md)
  - [stage-10-acceptance.md](stage-10-acceptance.md)
- **Next:** [stage-10b-handoff.md](stage-10b-handoff.md) — isolated real PostgreSQL/Milvus/Redis verification (no Stage 11 started)

Stage 9 remains closed under its amended acceptance scope (`12/12` closeout raw
artifacts preserved and verified). Within Stage 10-A, all offline application
runtime work packages (`A-R1`–`A-R6`) pass offline (`304` Stage 10 tests, `3611`
repository tests passing offline). Live isolated PostgreSQL/Milvus/Redis
integration (Stage 10-B), independent review (Stage 10-C), and real GPU/system
acceptance remain pending before Stage 10 acceptance can pass.
