# Stage 10 Acceptance Matrix

Status: **PARTIAL**. Implemented and offline-verified requirements are marked
SATISFIED; unavailable infrastructure/GPU evidence is marked PARTIAL.

| ID | Status | Evidence / limitation |
|---|---|---|
| S10-01 | SATISFIED | `RuntimeOperationalConfig` defaults/ranges; config/fingerprint tests |
| S10-02 | SATISFIED | `RuntimeOwnershipLock`; second-acquirer and idempotent tests |
| S10-03 | SATISFIED (offline) | Default production startup with fake boundaries, shared identities, MinerU guard; real infra Stage 10-B |
| S10-04 | PARTIAL | Formula `2C+2`, default 16; unit/type validation; no isolated contention load test |
| S10-05 | SATISFIED (offline) | Atomic query admission/release; admission vs maintenance and admission vs shutdown interleavings; duplicate-release idempotence |
| S10-05a | SATISFIED (offline) | `InFlightTaskRegistry`; registration failure, cancellation before/after admission, repeated cancellation, settlement before resource close |
| S10-06 | SATISFIED (offline) | Session Query Cache identity, eligibility, prepare/commit, ambiguity, same-key concurrency, maintenance invalidation and full miss/store/hit lifecycle; real Redis integration remains Stage 10-B |
| S10-06a | SATISFIED (offline) | Prepare/commit boundary, cancellation before/during/after submit, repeated cancellation settlement, ambiguous submitted write classified conservatively |
| S10-07 | SATISFIED (offline) | Drain/timeout/origin-aware abort; drain-timeout and pre-entry cancellation restore SERVING admission while preserving the query |
| S10-08 | SATISFIED (offline) | Maintenance consumes `SyncResult.serving_safety`; real deletion failure → UNSAFE → closed admission chain over fake PostgreSQL/Milvus; no isolated Stage 5 integration |
| S10-09 | PARTIAL (offline) | Actual startup orchestration over the transaction-aware fake: reconciliation BLOCKED, source-safety and verification independence, hard-bootstrap vs initialized-BLOCKED; real startup integration remains Stage 10-B |
| S10-09a | SATISFIED (offline) | Source safety matrix plus interrupted deletion before/inside/after Milvus and PostgreSQL commit, failed required deletion and restart; real infrastructure remains Stage 10-B |
| S10-10 | SATISFIED (offline) | Hard bootstrap failure vs initialized BLOCKED, unsafe knowledge, empty consistent KB and no-current-revision startup verified offline; real infrastructure remains Stage 10-B |
| S10-11 | SATISFIED (offline) | Recovery abort returns BLOCKED; failed recovery stays blocked; successful recovery re-verifies knowledge, restores readiness and reopens admission |
| S10-12 | SATISFIED (offline) | Barrier ordering, marker ambiguity/rollback/confirm-read failures, Redis outage/recovery, namespace invalidation and throttled recovery over fake PG/Redis |
| S10-13 | SATISFIED (offline) | Frozen keys, raw request hashing, session isolation, runtime fingerprint and policy eligibility preserved and re-verified |
| S10-14 | SATISFIED (offline) | Complete A-R5 Parent Context Cache matrix (`AR5-01`..`AR5-12`): canonical metadata equivalence, authoritative provenance, miss/fill, Redis faults/malformed payloads, stale/deleted isolation, mixed `TEXT_PARENT`/`TABLE` ordering, exact content/whitespace handling, UoW lifetime, and lifecycle tests; real PostgreSQL/Redis integration remains Stage 10-B |
| S10-15 | PARTIAL (offline) | Guarded providers/guard; MinerU boundary wired; queued guard work cannot begin after shutdown; real GPU/MinerU SDK evidence deferred to later deployment stages |
| S10-16 | PARTIAL | Composition initializes one model; real GPU model initialization deferred to later deployment stages |
| S10-17 | PARTIAL (offline) | Health/readiness tracked and settled; lifecycle-generation association rejects stale probes; bounded single-flight probes; no isolated real health-probe integration |
| S10-18 | PARTIAL (offline) | Coordinator drain/cancel/close; cancellation settlement, bounded timeout, concurrent/repeated shutdown; OS signal and launcher termination deferred to later deployment stages |
| S10-19 | PARTIAL | 3547 passed offline (1 skipped, 108 deselected); isolated real-infrastructure suite not executed |
| S10-20 | SATISFIED | Architecture checks, scope tests updated, Stage 9 artifacts preserved (12/12 SHA-256/size verified) |

A-R1, A-R2, A-R3, A-R4 (deterministic matrix) and A-R5 are complete and passing
offline; `AR4-T01` remains OPEN. Stage 10-A remains **PARTIAL** pending A-R6
final verification and reconciliation, and Stage 10 overall remains PARTIAL
pending Stage 10-B isolated PostgreSQL/Milvus/Redis integration and Stage 10-C
independent review. Acceptance remains UNSATISFIED.
