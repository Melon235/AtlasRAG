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
- **Status:** Stage 10-A (`A-R1`–`A-R6` offline), Stage 10-B (`12/12` isolated real PostgreSQL/Milvus/Redis integration), Stage 10-C1 (`FAIL`), and Stage 10-C2 remediation COMPLETE; Stage 10-C3 (independent re-review) `READY / PENDING_INDEPENDENT_REVIEW`; Stage 10 overall PARTIAL / acceptance UNSATISFIED
- **Report & Matrices:**
  - [stage-10-part-iv-application-runtime.md](stage-10-part-iv-application-runtime.md)
  - [stage-10a-completion.md](stage-10a-completion.md)
  - [stage-10a-verification-ledger.md](stage-10a-verification-ledger.md)
  - [stage-10b-completion.md](stage-10b-completion.md)
  - [stage-10-acceptance.md](stage-10-acceptance.md)
  - [stage-10-review/STAGE_10_C2_REMEDIATION.md](stage-10-review/STAGE_10_C2_REMEDIATION.md)
  - [stage-10-review/STAGE_10_C3_HANDOFF.md](stage-10-review/STAGE_10_C3_HANDOFF.md)
- **Next:** Stage 10-C3 independent re-review ([stage-10-review/REVIEW_SUMMARY.md](stage-10-review/REVIEW_SUMMARY.md)) — no Stage 11 started

Stage 9 remains closed under its amended acceptance scope (`12/12` closeout raw
artifacts preserved and verified). Within Stage 10, offline application runtime
verification (`348` Stage 10 tests, `3656` repository offline tests) and real
isolated PostgreSQL/Milvus/Redis integration (`12` Stage 10-B integration tests)
both pass after Stage 10-C2 remediation. Stage 10-C3 independent re-review and
real GPU/system hardware acceptance remain pending before Stage 10 acceptance can
pass.
