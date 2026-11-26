# Stage 10 Review Summary

Reviewed HEAD: `622434e8de4c65c4b919e34b2c5bfb1481f998aa`

| Review | Independent status | Self-review findings | Acceptance status |
|---|---|---|---|
| R10-01 Lifecycle/concurrency | INTERRUPTED / NO EVIDENCE | F-01, F-02 fixed; G-01..G-03 open | PARTIAL |
| R10-02 Cache/knowledge | INTERRUPTED / NO EVIDENCE | F-03, F-04 fixed; G-04..G-06 open | PARTIAL |
| R10-03 Architecture/ownership | UNAVAILABLE | G-07, G-08 open | PARTIAL |
| R10-04 Security/release | UNAVAILABLE | G-09, G-10 open | PARTIAL |

## Confirmed fixes

- Coordinator no longer double-acquires the ownership lock when composition already owns it.
- Maintenance freshness failure now blocks instead of leaving a lease in MAINTENANCE.
- Cache commit now classifies Redis write failure instead of raising it out of settlement.
- Parent cache now compares stored/domain chunk types correctly.

## Blockers

- No completed independent review evidence.
- No isolated real PostgreSQL/Milvus/Redis integration suite.
- Incomplete production business-graph composition.
- MinerU shared-guard boundary not implemented.
- Several mandatory concurrency/cache/startup tests remain missing.

Stage 10 remains **PARTIAL / UNSATISFIED**. No merge is authorized.


---

## Stage 10-A update (self-review evidence only)

F-01 diagnosis corrected: ownership was reacquired by coordinator because the
composition had already acquired it; the original review text's claim that
composition acquired twice was imprecise. F-01 regression now covers the
pre-owned coordinator path. F-02/F-03/F-04 regressions added. G-04..G-09 remain
open; G-10 remains Stage 10-C.

---

## Stage 10-A A-R2 update (deterministic evidence only)

R10-01 gaps G-01..G-03 now have deterministic offline evidence:

- G-01 shutdown settlement: `test_cancellation_settlement_closes_resources_after_worker_settles`,
  `test_canceled_query_settlement_is_awaited_before_resource_close`, and the
  preserved `test_shutdown_unsettled_worker_retains_resources_and_raises`.
- G-02 admission/registration race: `test_gate_admission_and_shutdown_offer_atomic_outcomes`,
  `test_shutdown_waits_for_tracked_pre_admission_health_probe`,
  `test_registration_failure_after_lease_acquisition_releases_lease`.
- G-03 stale health probe: `test_stale_health_probe_cannot_reopen_admission_after_maintenance`,
  `test_gate_ignores_admission_from_stale_lifecycle_generation`,
  `test_health_snapshot_records_lifecycle_generation`.

Six A-R2 defects (A-R2-F01..F06) were confirmed and fixed; regressions were
executed against the reverted pre-fix code path. Details are in the Stage 10-A
verification ledger and completion document.

Independent review remains UNAVAILABLE. No review status is upgraded and
Stage 10-C is not started.

---

## Stage 10-A A-R3 update (deterministic evidence only)

R10-01 gap G-04 (interrupted deletion) now has deterministic offline evidence:

- Interrupted before Milvus deletion: `test_interruption_before_milvus_deletion_preserves_both_stores`.
- Milvus failure/partial/absence verification: `test_milvus_delete_failure_keeps_canonical_state_and_blocks_serving`,
  `test_remaining_milvus_projection_is_unsafe_and_not_serving`,
  `test_failed_absence_verification_keeps_postgres_and_blocks_serving`.
- Interrupted before/inside PostgreSQL commit: `test_interruption_before_postgres_transaction_keeps_canonical_row`,
  `test_interruption_inside_postgres_transaction_rolls_back`,
  `test_postgres_commit_failure_rolls_back_deletion_and_marker`.
- Successful commit and restart: `test_successful_deletion_commits_marker_with_canonical_state`,
  `test_interruption_after_deletion_commit_restarts_to_serving`.
- Failure chain and restart safety: `test_failed_required_deletion_chain_blocks_runtime_before_restart`,
  `test_restart_with_mismatched_projection_is_unsafe_with_empty_summaries`.
- Source reappearance/incomplete scans/stale plans: `test_source_reappearance_after_planning_blocks_deletion`,
  `test_incomplete_scan_blocks_before_any_deletion`,
  `test_changed_canonical_revision_after_planning_blocks_deletion`.
- Explicit recovery: `test_failed_explicit_recovery_keeps_blocked_and_admission_closed`,
  `test_successful_explicit_recovery_verifies_knowledge_and_opens_admission`.

Three A-R3 defects (A-R3-F01..F03) were confirmed and fixed; regressions were
executed against the reverted pre-fix code path. Details are in the Stage 10-A
verification ledger and completion document.

Independent review remains UNAVAILABLE. No review status is upgraded and
Stage 10-C is not started.

---

## Stage 10-A A-R4 update (deterministic evidence only)

R10-02 gap G-05 (ambiguous Redis/PostgreSQL outcomes) now has deterministic
offline evidence:

- Ambiguous writes: `test_submitted_write_outcome_matrix`,
  `test_dependency_error_without_execution_detail_is_unknown`,
  `test_ambiguous_write_is_never_deleted_by_cleanup`.
- Concurrent same-key: `test_concurrent_same_key_misses_both_publish_and_last_value_is_valid`,
  `test_canceled_same_key_request_does_not_delete_other_valid_entry`,
  `test_ambiguous_same_key_write_preserves_other_valid_entry`.
- Marker ambiguity and invalidation: `test_marker_commit_applied_with_lost_ack_is_conservatively_bypassed`,
  `test_confirmatory_read_failure_remains_conservative`,
  `test_confirmatory_true_marker_keeps_cache_bypassed`,
  `test_complete_invalidation_precedes_marker_clear_and_confirmation`.
- Redis outage/recovery: `test_redis_outage_bypasses_cache_and_recovery_removes_stale_entries`.
- Maintenance/cache exclusion and settlement: `test_maintenance_waits_for_delayed_write_and_invalidates_it`,
  `test_shutdown_stalled_write_fails_then_recovers_after_settlement`.
- Recovery throttling: `test_due_callers_waiting_behind_held_recovery_run_once`.

Two A-R4 defects (A-R4-F01, A-R4-F02) were confirmed and fixed; regressions
were executed against the reverted pre-fix code path. The F-03 regression is
retained with the corrected conservative outcome.

Independent review remains UNAVAILABLE. No review status is upgraded and
Stage 10-C is not started.

---

## Stage 10-A A-R5 update (deterministic evidence only)

R10-02 gap G-06 (Parent Cache) now has complete deterministic offline evidence
across `AR5-01`..`AR5-12` (`tests/stage10/test_ar5_parent_cache.py`,
`tests/stage10/test_ar5_lifecycle.py`, preserved
`tests/stage10/test_parent_cache.py`, and `tests/persistence/test_postgres_contracts.py`):

- Production `build_default_business` wiring, TTL, key isolation, and Query
  Cache independence (`test_ar5_01_production_builder_wires_parent_cache_with_ttl_and_key_isolation`).
- Valid hit authoritative provenance, header-only metadata validation without
  reading canonical `chunks.content`, and partial-header fallback
  (`test_valid_text_parent_cache_hit_reconstructs_authoritative_provenance`,
  `test_ar5_02_valid_hit_works_with_single_header_reader_and_partial_header_fallback`).
- Miss/canonical fill, Redis GET/SET failures, malformed payloads, mismatched
  identities, cancellation propagation (`test_ar5_03_*`, `test_ar5_04_*`).
- Non-resurrection for missing, stale, unpublished, or wrong-lineage contexts
  and full metadata validation equivalence (`test_ar5_05_*`, `test_ar5_06_*`).
- Exact content/whitespace-only handling, mixed `TEXT_PARENT`/`TABLE` ordering,
  duplicate rejection, canonical `TABLE`, disabled cache, UoW/Query Lease
  lifetimes, maintenance exclusion, and R1→R2/deletion lifecycle (`test_ar5_07_*`
  through `test_ar5_12_*`).

Five A-R5 defects (`A-R5-F01`..`A-R5-F05`) were confirmed against `6ffab31` and
fixed with passing regressions. `AR4-T01` (Redis transport settlement) remains
OPEN for A-R6 deterministic closure and Stage 10-B real isolated verification.

Independent review remains UNAVAILABLE. No review status is upgraded and
Stage 10-C is not started.
