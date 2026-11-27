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
- **Status:** Stage 10 `ACCEPTED / PENDING_MERGE` (`Stage 10-A` COMPLETE, `Stage 10-B` COMPLETE, `Stage 10-C` COMPLETE / PASS via Stage 10-C3-R1 on `be5a2876`, `Stage 10-C4` COMPLETE; `23/23` requirements SATISFIED, `6/6` blocking findings CLOSED, `10/10` gaps CLOSED within approved scope; `AR4-T01` `OFFLINE_AND_REAL_INFRA_VERIFIED`; `PR #11` READY — `OWNER_AUTHORIZATION_REQUIRED`)
- **Report, Closeout & Matrices:**
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
- **Next:** Await owner authorization to merge `PR #11`; next planned stage is Stage 11 — Local Server and Operational CLI (`NOT_STARTED`; see [stage-10-closeout/STAGE_11_HANDOFF.md](stage-10-closeout/STAGE_11_HANDOFF.md))

Stage 9 remains closed under its amended acceptance scope (`12/12` closeout raw
artifacts preserved and verified). Within Stage 10, offline application runtime
verification (`352` Stage 10 tests, `3660` repository offline tests), real
isolated PostgreSQL/Milvus/Redis integration (`12` Stage 10-B integration tests),
and final independent re-review (`Stage 10-C3-R1` PASS on `be5a2876`) are
complete. Explicit Stage 11 and real GPU/system hardware execution items remain
deferred beyond Stage 10 (`NOT_STARTED`).
