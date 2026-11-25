# Stage 10-A Offline Verification Ledger

Status: **PARTIAL**. One hundred twenty-three deterministic Stage 10 tests
pass. A-R1, A-R2 and A-R3 are COMPLETE offline; A-R4, A-R5 and A-R6 remain
incomplete, and no offline PASS is claimed for missing rows. Real isolated
infrastructure remains Stage 10-B; independent review remains Stage 10-C.

## A-R1 Production composition — COMPLETE (offline, fake technical boundaries)

| Scenario | Test | Result |
|---|---|---|
| Default builder assembles Part I/II services | `test_default_builder_constructs_existing_part_i_and_part_ii_services` | PASS |
| Default production entry exists | `test_default_production_composition_is_available` | PASS |
| Full default startup reaches SERVING | `test_default_production_startup_and_shutdown_with_fake_boundaries` | PASS |
| One pool/Milvus/Redis/Qwen/reranker | same (constructor counters) | PASS |
| GuardedMinerU uses shared guard | same (ParserRouter capture) | PASS |
| Ownership acquired/released once | same | PASS |
| Bootstrap failure rollback + ownership release | `test_bootstrap_failure_rolls_back_and_releases_ownership` | PASS |
| CPU reranker degradation, no CUDA fallback | `test_cpu_config_degrades_reranker_without_cuda_fallback` | PASS |
| Cleanup failure attempts all resources | `test_composition_cleanup_attempts_all_resources_and_is_repeat_safe` | PASS |
| Close failure retains ownership, no false success | `test_shutdown_close_failure_retains_ownership_and_is_not_success` | PASS |
| Repeated cleanup safe | cleanup regression | PASS |

## A-R2 Runtime concurrency, health lifecycle and shutdown — COMPLETE (offline)

All A-R2 tests use the real `RuntimeGate`, `InFlightTaskRegistry`,
`HealthRegistry`, `MaintenanceCoordinator` and `ApplicationCoordinator` with
fake technical providers and business callbacks (`tests/stage10/runtime_harness.py`).

### AR2-01 — Admission versus maintenance

| Ordering / invariant | Test | Result |
|---|---|---|
| Query admitted first: DRAINING closes admission, mutation runs only after lease settlement (`active_queries == 0`, retrieval settled) | `test_query_admission_wins_and_mutation_waits_for_query_settlement` | PASS |
| Maintenance first: query rejected before retrieval, no lease acquired | `test_maintenance_admission_wins_and_query_never_reaches_retrieval` | PASS |

### AR2-02 — Admission versus shutdown

| Ordering / invariant | Test | Result |
|---|---|---|
| Shutdown before admission: new query/maintenance rejected, no retrieval, no lease | `test_shutdown_before_admission_rejects_new_work_without_retrieval` | PASS |
| Shutdown while pre-admission health refresh waits: registry tracked, close blocked until probe settles, query rejected | `test_shutdown_waits_for_tracked_pre_admission_health_probe` | PASS |
| Shutdown after admission before retrieval: admitted work settles before close, no cancellation in grace window | `test_shutdown_after_admission_allows_query_to_settle_before_close` | PASS |
| Gate admission vs shutdown atomic outcomes in both scheduling orders | `test_gate_admission_and_shutdown_offer_atomic_outcomes[True/False]` | PASS |
| Active query accounting does not leak; shutdown terminal | shutdown/lease regressions below | PASS |

### AR2-03 — Task registration and lease accounting

| Invariant | Test | Result |
|---|---|---|
| Preserved registration-failure regression | `test_registration_failure_releases_query_lease` | PASS |
| Registration failure after lease acquisition releases the lease, no retrieval | `test_registration_failure_after_lease_acquisition_releases_lease` | PASS |
| Operation registration closed by shutdown maps to `RuntimeShuttingDownError` | `test_shutdown_before_admission_rejects_new_work_without_retrieval` | PASS |
| Cancellation while waiting for admission cleans tracking, no lease | `test_cancellation_during_pre_admission_health_refresh_cleans_up` | PASS |
| Cancellation after lease acquisition releases exactly once | `test_cancellation_after_lease_acquisition_releases_exactly_once` | PASS |
| Repeated cancellation during cleanup does not double-release | `test_repeated_cancellation_during_cleanup_does_not_double_release` | PASS |
| Health tracking is distinct from query lease accounting | `test_health_tracking_is_distinct_from_query_lease_accounting` | PASS |
| Duplicate lease release is idempotent | `test_gate_admission_and_exactly_once_release` | PASS |

### AR2-04 — Public health/readiness ownership

| Invariant | Test | Result |
|---|---|---|
| Public health probe tracked; resource close waits for settlement | `test_public_health_request_is_tracked_until_probe_settles` | PASS |
| Public readiness probe tracked; resource close waits for settlement | `test_public_readiness_request_is_tracked_until_probe_settles` | PASS |
| Pre-admission query health refresh tracked as `HEALTH` | `test_shutdown_waits_for_tracked_pre_admission_health_probe`, `test_health_tracking_is_distinct_from_query_lease_accounting` | PASS |
| Coordinator-owned post-maintenance refresh tracked as `MAINTENANCE` | `test_maintenance_health_refresh_is_tracked_until_settlement` | PASS |

### AR2-05 — Bounded health refresh

| Invariant | Test | Result |
|---|---|---|
| Fresh snapshot reused; expired snapshot refreshes (fake monotonic clock) | `test_fresh_snapshot_is_reused_and_expired_snapshot_refreshes` | PASS |
| Concurrent refreshes bounded/serialized, one probe batch at a time | `test_concurrent_health_refreshes_are_bounded_and_serialized` | PASS |
| Hard-probe failure closes admission; recovery reopens it, no per-query full verification | `test_probe_failure_closes_admission_then_recovers` | PASS |
| Soft dependency (Redis) failure alone does not block readiness | `test_soft_dependency_failure_does_not_block_readiness` | PASS |

### AR2-06 — Stale health-probe lifecycle race

| Invariant | Test | Result |
|---|---|---|
| Snapshot records the lifecycle generation sampled at probe start | `test_health_snapshot_records_lifecycle_generation` | PASS |
| Delayed probe spanning a transition is not current | `test_stale_probe_spanning_lifecycle_transition_is_not_current` | PASS |
| Gate ignores admission decisions from an older lifecycle epoch (DRAINING, abort-to-SERVING, SHUTTING_DOWN) | `test_gate_ignores_admission_from_stale_lifecycle_generation` | PASS |
| Stale probe cannot reopen admission after maintenance completed (SERVING epoch change) | `test_stale_health_probe_cannot_reopen_admission_after_maintenance` | PASS |
| Stale probe cannot override a BLOCKED transition | `test_stale_probe_cannot_override_blocked_lifecycle` | PASS |
| Readiness uses current mode, not the snapshot's sampled mode | `test_readiness_uses_current_runtime_mode_not_stale_snapshot` | PASS |
| Dependency health cannot certify unverified knowledge as safe | `test_unknown_knowledge_is_not_certified_by_dependency_health` | PASS |
| Authoritative UNSAFE maintenance outcome is not overridden by healthy probes | `test_unsafe_maintenance_outcome_is_not_overridden_by_healthy_probes` | PASS |

### AR2-07 — Graceful shutdown

| Invariant | Test | Result |
|---|---|---|
| Admitted query finishes without cancellation; lease/task settle before close; ownership releases last | `test_graceful_shutdown_drains_admitted_query_without_cancellation` | PASS |
| Shutdown coordinator is not registered/canceled as ordinary work | same (registry contains only the query task) | PASS |
| Independent queries run concurrently, not globally serialized | `test_independent_queries_are_not_globally_serialized` | PASS |
| A-R1 cleanup order and ownership invariants preserved | `test_composition_cleanup_attempts_all_resources_and_is_repeat_safe`, `test_shutdown_close_failure_retains_ownership_and_is_not_success` | PASS |

### AR2-08 — Shutdown timeout and cancellation

| Invariant | Test | Result |
|---|---|---|
| Cancellation-settled worker allows close/release (no false failure) | `test_cancellation_settlement_closes_resources_after_worker_settles` | PASS |
| Canceled query settlement is awaited before resource close; lease released | `test_canceled_query_settlement_is_awaited_before_resource_close` | PASS |
| Queued guarded work cannot begin after shutdown | `test_queued_guard_work_cannot_begin_after_shutdown` | PASS |
| Unsettled worker raises, retains resources/ownership, stays SHUTTING_DOWN | `test_shutdown_unsettled_worker_retains_resources_and_raises`, `test_unsettled_worker_failure_preserves_recovery_boundary` | PASS |

### AR2-09 — Maintenance, recovery and shutdown interactions

| Invariant | Test | Result |
|---|---|---|
| Exclusive maintenance ownership; second request rejected | `test_maintenance_ownership_is_exclusive` | PASS |
| Drain timeout preserves the query and restores SERVING admission | `test_maintenance_drain_timeout_preserves_query_and_restores_admission` | PASS |
| Cancellation before maintenance entry restores SERVING origin/admission | `test_maintenance_cancellation_before_entry_restores_serving` | PASS |
| Recovery cancellation after entry remains BLOCKED | `test_recovery_cancellation_after_maintenance_entry_remains_blocked` | PASS |
| Post-mutation exception blocks; never resumes SERVING directly | `test_maintenance_exception_after_mutation_blocks_and_never_serves` | PASS |
| Shutdown during DRAINING/MAINTENANCE is terminal; late completion cannot overwrite | `test_shutdown_during_drain_keeps_shutting_down_terminal`, `test_shutdown_during_maintenance_keeps_shutting_down_terminal` | PASS |
| Authoritative UNSAFE result not overridden | `test_unsafe_maintenance_outcome_is_not_overridden_by_healthy_probes` | PASS |

### AR2-10 — Complete query lifecycle and cache-hit admission

| Invariant | Test | Result |
|---|---|---|
| Cache hit while SERVING traverses admission and holds the Query Lease | `test_cache_hit_while_serving_traverses_application_admission` | PASS |
| Cache hit rejected in DRAINING, MAINTENANCE and SHUTTING_DOWN before any work | `test_cache_hit_attempt_is_rejected_outside_serving[DRAINING/MAINTENANCE/SHUTTING_DOWN]` | PASS |
| Shutdown waits for admitted cache-hit finalization | `test_shutdown_waits_for_admitted_cache_hit_finalization` | PASS |

### AR2-11 — Repeated and concurrent shutdown

| Invariant | Test | Result |
|---|---|---|
| Repeated successful shutdown closes/releases once and stays terminal | `test_repeated_successful_shutdown_closes_once_and_stays_terminal` | PASS |
| Concurrent shutdown calls close resources once | `test_concurrent_shutdown_calls_close_resources_once` | PASS |
| Failed cleanup is not falsely successful; later success only after real close | `test_failed_cleanup_is_not_falsely_successful_on_repeat` | PASS |
| Unsettled-worker failure retains the recovery boundary; later invocation can settle and close | `test_unsettled_worker_failure_preserves_recovery_boundary` | PASS |

### A-R2 confirmed defects and regressions

Each regression below was executed against the reverted pre-A-R2 code path; the
pre-fix result is the observed failure, and the listed fix is the minimal
compatible correction. No independent review evidence is claimed.

| ID | Defect | Regression(s) | Pre-fix result |
|---|---|---|---|
| A-R2-F01 | `wait_empty` checked the deadline before pruning settled tasks, so a worker that settled on cancellation was reported as an unsettled shutdown failure | `test_cancellation_settlement_closes_resources_after_worker_settles` | `RuntimeShutdownFailedError`; resources/ownership retained |
| A-R2-F02 | Public `health()`/`readiness()` probed dependencies without task registration, allowing resource close during an in-flight probe | `test_public_health_request_is_tracked_until_probe_settles`, `test_public_readiness_request_is_tracked_until_probe_settles` | probe absent from registry; shutdown closed resources while the probe waited |
| A-R2-F03 | Health results were not associated with runtime lifecycle state; stale probe results could reopen admission after a maintenance transition, and readiness used the snapshot's sampled mode | `test_stale_health_probe_cannot_reopen_admission_after_maintenance`, `test_gate_ignores_admission_from_stale_lifecycle_generation`, `test_readiness_uses_current_runtime_mode_not_stale_snapshot` | stale query admitted after maintenance; readiness True during DRAINING |
| A-R2-F04 | Aborting maintenance during DRAINING restored SERVING but left admission closed, so the runtime was SERVING yet rejected new queries | `test_maintenance_drain_timeout_preserves_query_and_restores_admission`, `test_maintenance_cancellation_before_entry_restores_serving` | `RuntimeNotReadyError` after abort |
| A-R2-F05 | Concurrent shutdown calls could both pass `_shutdown_done` and close resources twice | `test_concurrent_shutdown_calls_close_resources_once` | close callback invoked twice |
| A-R2-F06 | Operations attempted after shutdown failed registration with generic `RuntimeError` instead of the stable `RuntimeShuttingDownError` | `test_shutdown_before_admission_rejects_new_work_without_retrieval` | `RuntimeError("task registry is closed")` |

## A-R3 Interrupted deletion and startup safety — COMPLETE (offline)

All A-R3 tests use the real `StartupReconciliationService`,
`StartupSourceSafetyService`, `GlobalKnowledgeVerificationService`,
`MissingSourceDeletionService`, `SyncServices`/`IndexSyncApplication`,
`MaintenanceCoordinator` and `ApplicationCoordinator` over the Stage 3
transaction-aware PostgreSQL fake (committed/working state, commit, rollback)
and an independently recorded Milvus projection fake
(`tests/stage10/ar3_support.py`). No final safety verdict is faked.

### AR3-01 — Source availability contract

File: `tests/stage10/test_ar3_source_safety.py`

| Scenario | Test | Result |
|---|---|---|
| Available unchanged source: SERVING, no sync, query admitted | `test_startup_serves_available_unchanged_source_without_sync` | PASS |
| Changed available source: last-good revision preserved, no rebuild | `test_changed_available_source_keeps_last_good_revision_without_rebuild` | PASS |
| Confirmed missing published source: BLOCKED before verification | `test_confirmed_missing_published_source_blocks_startup` | PASS |
| Unreadable published source: UNAVAILABLE finding, BLOCKED, no admission | `test_unreadable_published_source_blocks_startup` | PASS |
| Document without published current revision: valid startup, no sync | `test_document_without_current_revision_does_not_block_startup` | PASS |
| Empty internally consistent KB: SERVING, no sync | `test_empty_consistent_kb_serves` | PASS |

Existing direct service tests (`test_source_safety_blocks_missing_published_source`,
`test_source_safety_allows_available_changed_source`, `test_empty_consistent_kb_is_safe`,
`test_unreadable_published_source_is_unsafe`,
`test_inaccessible_root_is_a_hard_failure`) remain passing.

### AR3-02 — Root accessibility and source classification

File: `tests/stage10/test_ar3_source_safety.py`

| Scenario | Test | Result |
|---|---|---|
| Confirmed missing file | `test_confirmed_missing_source_is_classified_missing` | PASS |
| PermissionError is not confirmed absence | `test_permission_denied_source_is_unavailable_not_missing` | PASS |
| I/O error is unreadable, not missing | `test_io_error_source_is_unavailable` | PASS |
| Unavailable parent component | `test_unavailable_parent_directory_is_unavailable` | PASS |
| Invalid/unsafe source identity | `test_invalid_source_identity_is_unavailable` | PASS |
| Unreadable source cannot authorize deletion | `test_deletion_refuses_unreadable_source` | PASS |
| Unavailable root cannot authorize deletion | `test_deletion_refuses_unavailable_knowledge_root` | PASS |
| Invalid identity cannot authorize deletion | `test_deletion_refuses_invalid_source_identity` | PASS |
| Missing root: hard bootstrap failure | `test_missing_knowledge_root_is_a_bootstrap_failure` | PASS |
| Non-directory root: hard bootstrap failure | `test_non_directory_knowledge_root_is_a_bootstrap_failure` | PASS |
| Unreadable root: hard bootstrap failure | `test_unreadable_knowledge_root_is_a_bootstrap_failure` | PASS |
| BLOCKED reconciliation stays initialized-BLOCKED | `test_blocked_reconciliation_is_initialized_blocked_not_bootstrap_failure` | PASS |

### AR3-03 — Interruption before Milvus deletion

File: `tests/stage10/test_ar3_interrupted_deletion.py`

| Invariant | Test | Result |
|---|---|---|
| Cancelled at the pre-Milvus probe: both stores still match; fresh startup sees missing source, stays BLOCKED, runs no sync and deletes nothing; empty summaries remain SAFE but source safety blocks | `test_interruption_before_milvus_deletion_preserves_both_stores` | PASS |

### AR3-04 — Milvus failure and partial deletion

File: `tests/stage10/test_ar3_interrupted_deletion.py`

| Invariant | Test | Result |
|---|---|---|
| Milvus delete failure before removal: PG commit not attempted, `MILVUS_DELETE_FAILED`, UNSAFE, leases settle, restart BLOCKED | `test_milvus_delete_failure_keeps_canonical_state_and_blocks_serving` | PASS |
| Remaining projection: `VISIBILITY_REMAINS`, UNSAFE, PG retained, restart BLOCKED; empty verification summary is not proof of deletion | `test_remaining_milvus_projection_is_unsafe_and_not_serving` | PASS |
| Failed absence verification: PG retained, `MILVUS_DELETE_FAILED`, UNSAFE MISSING_RECORDS on restart | `test_failed_absence_verification_keeps_postgres_and_blocks_serving` | PASS |

### AR3-05 — Interruption before PostgreSQL commit

File: `tests/stage10/test_ar3_interrupted_deletion.py`

| Invariant | Test | Result |
|---|---|---|
| A. Cancelled before the PostgreSQL transaction: Milvus absent+verified, PG row remains, marker false; restart BLOCKED, no republish, verification MISSING_RECORDS | `test_interruption_before_postgres_transaction_keeps_canonical_row` | PASS |
| B. Interruption inside the PG transaction: rollback discards staged deletion, Milvus absent, PG row+marker intact | `test_interruption_inside_postgres_transaction_rolls_back` | PASS |
| B. Commit failure: `POSTGRES_DELETE_FAILED`, rollback, restart BLOCKED and verification UNSAFE | `test_postgres_commit_failure_rolls_back_deletion_and_marker` | PASS |

### AR3-06 — Successful deletion commit and restart

File: `tests/stage10/test_ar3_interrupted_deletion.py`

| Invariant | Test | Result |
|---|---|---|
| Milvus absence scan precedes canonical deletion; document/elements/chunks removed; marker true; lease released; PG and marker committed together (rollback case in AR3-05B) | `test_successful_deletion_commits_marker_with_canonical_state` | PASS |
| Interrupted after commit at final verification: durable deletion+marker survive; fresh restart SERVING on empty KB; startup verification precedes freshness, freshness sees marker true, then admission | `test_interruption_after_deletion_commit_restarts_to_serving` | PASS |

### AR3-07 — Required deletion failure followed by restart

File: `tests/stage10/test_ar3_interrupted_deletion.py`

| Invariant | Test | Result |
|---|---|---|
| Real chain: deletion failure → `DeletionSummary.failed` → global verification → UNSAFE → BLOCKED and admission closed; match-state restart stays BLOCKED by source safety while projections match | `test_failed_required_deletion_chain_blocks_runtime_before_restart` | PASS |
| Mismatched restart: empty startup deletion summary cannot erase independently observed MISSING_RECORDS | `test_restart_with_mismatched_projection_is_unsafe_with_empty_summaries` | PASS |

### AR3-08 — Source reappearance and incomplete scans

File: `tests/stage10/test_ar3_startup_recovery.py`

| Invariant | Test | Result |
|---|---|---|
| Source reappears after planning/before deletion: `SOURCE_REAPPEARED`, no deletion, UNSAFE, durable state intact; restart is SERVING because the source is available | `test_source_reappearance_after_planning_blocks_deletion` | PASS |
| Incomplete scan: `SCAN_PERMISSION_DENIED`, no build/delete side effects, runtime BLOCKED | `test_incomplete_scan_blocks_before_any_deletion` | PASS |
| Stale canonical revision after planning: `STATE_CHANGED`, no deletion, UNSAFE, restart BLOCKED | `test_changed_canonical_revision_after_planning_blocks_deletion` | PASS |
| Changed revision revalidation (existing) | `tests/stage5/test_deletion.py::test_changed_current_revision_is_not_deleted` | PASS |
| Reappeared source revalidation (existing) | `tests/stage5/test_deletion.py::test_reappeared_source_blocks_deletion` | PASS |

### AR3-09 — Authoritative startup safety

| Condition | Test | Result |
|---|---|---|
| Reconciliation BLOCKED with matching projections | `test_blocked_reconciliation_is_initialized_blocked_not_bootstrap_failure` | PASS |
| Source safety fails despite matching projections | `test_confirmed_missing_published_source_blocks_startup`, `test_interruption_before_milvus_deletion_preserves_both_stores` | PASS |
| Global verification fails despite available sources | `test_global_verification_failure_is_not_overridden_by_source_safety` | PASS |
| All safety conditions pass | `test_startup_serves_available_unchanged_source_without_sync` | PASS |
| Empty internally consistent KB | `test_empty_consistent_kb_serves` | PASS |
| Modified source with valid last-good revision | `test_changed_available_source_keeps_last_good_revision_without_rebuild` | PASS |
| Orphan cleanup proven before serving | `test_orphan_projection_is_removed_before_serving` | PASS |
| Attempted-but-unproven cleanup blocks serving | `test_unremoved_orphan_projection_blocks_serving` | PASS |
| Hard bootstrap failures stay distinct from initialized BLOCKED | `test_missing_knowledge_root_is_a_bootstrap_failure`, `test_non_directory_knowledge_root_is_a_bootstrap_failure`, `test_unreadable_knowledge_root_is_a_bootstrap_failure` | PASS |

### AR3-10 — Explicit BLOCKED recovery

File: `tests/stage10/test_ar3_startup_recovery.py`

| Invariant | Test | Result |
|---|---|---|
| Failed recovery: still BLOCKED, knowledge unverified, readiness false, no admission; durable deletion remains undone | `test_failed_explicit_recovery_keeps_blocked_and_admission_closed` | PASS |
| Successful recovery: required deletion completed, freshness handled (marker true), knowledge verified, readiness true, query admission reopened; no direct BLOCKED→SERVING shortcut | `test_successful_explicit_recovery_verifies_knowledge_and_opens_admission` | PASS |
| Restart after interrupted deletion never runs automatic sync; source safety keeps admission closed | `test_restart_after_interrupted_deletion_never_runs_automatic_sync` | PASS |
| Frozen six-mode vocabulary | `test_runtime_mode_vocabulary_has_no_blocked_to_serving_shortcut` | PASS |

### A-R3 confirmed defects and regressions

Each regression was executed against the reverted pre-A-R3 code path; the
pre-fix result is the observed failure. No independent review is claimed.

| ID | Defect | Regression(s) | Pre-fix result |
|---|---|---|---|
| A-R3-F01 | Failed existence checks authorized destructive deletion: `Path.exists()` collapsed PermissionError/I/O failure/invalid paths to "missing", and deletion revalidation had no root/path validation | `test_permission_denied_source_is_unavailable_not_missing`, `test_io_error_source_is_unavailable`, `test_invalid_source_identity_is_unavailable`, `test_deletion_refuses_unreadable_source`, `test_deletion_refuses_unavailable_knowledge_root`, `test_deletion_refuses_invalid_source_identity` | MISSING/AVAILABLE classification and `DELETED` canonical+index records for an unreadable source |
| A-R3-F02 | Inaccessible or non-directory knowledge roots became initialized-BLOCKED (or SERVING for an unreadable empty root) instead of the approved hard bootstrap failure; `StartupSourceSafetyService` only resolved the root | `test_missing_knowledge_root_is_a_bootstrap_failure`, `test_non_directory_knowledge_root_is_a_bootstrap_failure`, `test_unreadable_knowledge_root_is_a_bootstrap_failure` | `RuntimeBootstrapFailedError` not raised |
| A-R3-F03 | Authoritative maintenance verdicts were not propagated into `HealthRegistry`, so an initially-BLOCKED runtime could complete successful explicit recovery to SERVING but remain unready and reject all queries; an UNSAFE verdict left knowledge marked verified | `test_successful_explicit_recovery_verifies_knowledge_and_opens_admission`, `test_failed_explicit_recovery_keeps_blocked_and_admission_closed` | readiness false after successful recovery; query admission closed |

The A-R2 test `test_unsafe_maintenance_outcome_is_not_overridden_by_healthy_probes`
was corrected to assert the authoritative semantics: dependency probes remain
HEALTHY, but `knowledge_verified` and `hard_healthy` are false after UNSAFE.

## A-R4 Query Cache and freshness

| Scenario | Test | Result |
|---|---|---|
| Classified Redis dependency failure | `test_cache_commit_classifies_redis_failure_without_raising` | PASS |
| Applied write, bad acknowledgement | `test_cache_commit_applied_write_with_bad_ack_is_unknown` | PASS |
| Cancellation during commit settlement | `test_cache_commit_cancellation_settles_write_and_propagates` | PASS |
| Marker true invalidates before clear | `test_marker_true_invalidates_before_clear` | PASS |
| Partial invalidation keeps marker | `test_partial_invalidation_keeps_marker_true` | PASS |
| Canonical marker corruption propagates | `test_canonical_marker_error_propagates` | PASS |
| Concurrent recovery serialized | `test_concurrent_recovery_is_serialized` | PASS |
| PG marker-clear failure/ambiguous commit | — | MISSING_TEST |
| Cache-hit through query lease | `test_cache_hit_while_serving_traverses_application_admission` (A-R2 boundary evidence only; cache protocol unchanged) | PASS (admission) / A-R4 matrix pending |
| Concurrent same-key/cross-session | — | MISSING_TEST |

## A-R5 Parent Cache

| Scenario | Test | Result |
|---|---|---|
| Valid TEXT_PARENT hit, authoritative provenance | `test_valid_text_parent_cache_hit_reconstructs_authoritative_provenance` | PASS |
| Miss/fill, Redis unavailable, malformed, stale | — | MISSING_TEST |
| Disabled cache, TABLE canonical | — | MISSING_TEST |

## A-R6 Final verification

| Check | Result |
|---|---|
| `make test-stage10` | PASS — 123 tests |
| `pytest` full | PASS — 3417 passed, 1 skipped, 108 deselected |
| ruff / mypy / architecture / format / diff | PASS |
| Real isolated infrastructure | NOT_EXECUTED (Stage 10-B) |
| Independent review | NOT_EXECUTED (Stage 10-C) |

A-R6 remains PENDING: the offline suites pass, but A-R4/A-R5 matrices are still
incomplete and Stage 10-B/10-C evidence is absent.
