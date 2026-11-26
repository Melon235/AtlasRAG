# Stage 10-A Offline Verification Ledger

Status: **Stage 10-A COMPLETE offline (`304` Stage 10 tests pass across
`A-R1`..`A-R6`, plus Stage 10-B startup-fence fail-closed and CUDA device-match
regressions). `AR4-T01` is now `OFFLINE_AND_REAL_INFRA_VERIFIED` via
[Stage 10-B completion](stage-10b-completion.md); independent review remains
Stage 10-C (`NOT_STARTED`). Stage 10 overall remains PARTIAL; acceptance
remains UNSATISFIED.**

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

## A-R4 Query Cache, freshness and concurrent cache correctness — COMPLETE (offline)

All A-R4 tests use the real `RedisCacheProvider` over a stateful raw-Redis
transport fake, the real `SessionQueryCache`, `CacheFreshnessBarrier`,
retrieval/`RagCore` wrappers, `RuntimeGate`, `MaintenanceCoordinator` and
`ApplicationCoordinator`. PostgreSQL marker state uses an explicit transaction
fake with committed/working state, rollback and ambiguous-commit injection
(`tests/stage10/cache_support.py`).

### AR4-01 — Query Cache identity and eligibility

Files: `tests/stage10/test_ar4_identity.py`, existing `tests/stage8/test_boundary_cache.py`, `tests/stage8/test_cache_provenance.py`

| Scenario | Test | Result |
|---|---|---|
| Same session/exact request hits; different sessions independent | `test_same_session_exact_request_hits_and_other_sessions_are_independent` | PASS |
| Changed query / changed filters produce distinct identity | `test_changed_query_and_changed_filters_have_distinct_identity` | PASS |
| Runtime fingerprint is part of identity | `test_runtime_fingerprint_is_part_of_identity` | PASS |
| Prepare submits no write | `test_prepare_does_not_submit_a_redis_write` | PASS |
| Only completed local eligible results | `test_completed_local_result_is_the_only_eligible_result` | PASS |
| Web/unknown provenance, time-sensitive, oversized bypass | `test_web_provenance_time_sensitivity_and_oversize_bypass`, `test_web_derived_citation_is_not_cacheable` | PASS |
| Time-sensitive lookup bypasses Redis | `test_time_sensitive_lookup_bypasses_redis` | PASS |
| Disabled cache performs no GET/SET | `test_disabled_cache_performs_no_redis_operations_and_freshness_still_runs`, existing `test_disabled_cache_skips_reads_even_with_existing_entry` | PASS |
| Frozen key and session-isolated hash | `test_frozen_key_contract_and_session_isolated_identity` | PASS |
| Existing policy/provenance matrices retained | Stage 8 tests above | PASS |

### AR4-02 — Prepare and commit boundary

File: `tests/stage10/test_ar4_commit.py`

| Invariant | Test | Result |
|---|---|---|
| Cancellation during preparation submits no write | `test_cancellation_during_preparation_submits_no_write` | PASS |
| Cancellation immediately before commit submits no write | `test_cancellation_immediately_before_commit_submits_no_write` | PASS |
| Acknowledged commit stores | `test_acknowledged_commit_stores_one_entry` | PASS |
| Cancellation during submitted write settles then propagates | `test_cancellation_during_submitted_write_settles_then_propagates` | PASS |
| Repeated cancellation settles without hanging | `test_repeated_cancellation_during_write_settles_without_hanging` | PASS |
| Cancellation after acknowledgement keeps a valid entry | `test_cancellation_after_write_acknowledgement_keeps_valid_entry` | PASS |
| Store wrapper reports ambiguous outcome as false | `test_store_wrapper_reports_false_for_unknown_outcome` | PASS |
| Actual retrieval graph stores then hits | `test_actual_retrieval_graph_stores_then_hits` | PASS |
| Canceled request is not reported successful | `test_canceled_application_request_is_not_reported_successful` | PASS |
| Existing F-03/invalid-ack/cancellation regressions | `tests/stage10/test_cache_commit.py` | PASS |

### AR4-03 — Ambiguous Redis write outcomes

File: `tests/stage10/test_ar4_commit.py`

| Submitted outcome | Test case | Result |
|---|---|---|
| Applied + valid ack → STORED | `test_submitted_write_outcome_matrix[store-STORED-True]` | PASS |
| Applied + timeout → UNKNOWN at provider | `test_submitted_write_outcome_matrix[apply_timeout-UNKNOWN-True]` | PASS |
| Applied + connection loss → UNKNOWN | `test_submitted_write_outcome_matrix[apply_connection-UNKNOWN-True]` | PASS |
| Applied + malformed ack → UNKNOWN | `test_submitted_write_outcome_matrix[apply_bad_ack-UNKNOWN-True]` | PASS |
| Server rejection / connection failure before application → conservatively UNKNOWN | `test_submitted_write_outcome_matrix[reject-UNKNOWN-False]`, `[unavailable_before-UNKNOWN-False]` | PASS |
| No write attempted (ineligible/disabled/untrusted) → no commit | `test_no_write_attempt_paths_report_no_stored_outcome` | PASS |
| Dependency error with no execution detail → UNKNOWN | `test_dependency_error_without_execution_detail_is_unknown` | PASS |
| Programming/invariant errors propagate | `test_programming_error_propagates_from_commit` | PASS |
| Ambiguous write is not deleted by cleanup | `test_ambiguous_write_is_never_deleted_by_cleanup` | PASS |

Current adapter errors cannot prove non-execution for a submitted SET, so
submitted transport failures are conservatively UNKNOWN; BYPASSED is restricted
to no-attempt paths. This is recorded as a limitation, not certainty.

### AR4-04 — Concurrent same-key requests and session isolation

File: `tests/stage10/test_ar4_commit.py`

| Invariant | Test | Result |
|---|---|---|
| Both miss before writes; both publish; last valid value wins | `test_concurrent_same_key_misses_both_publish_and_last_value_is_valid` | PASS |
| One canceled before commit does not delete the other entry | `test_canceled_same_key_request_does_not_delete_other_valid_entry` | PASS |
| Ambiguous write plus valid write preserves a valid entry | `test_ambiguous_same_key_write_preserves_other_valid_entry` | PASS |
| Different sessions do not leak values | `test_concurrent_different_sessions_do_not_leak_values` | PASS |
| Incomplete results never publish concurrently | `test_incomplete_results_never_publish_under_concurrency` | PASS |

### AR4-05 — Persisted marker and invalidation ordering

File: `tests/stage10/test_ar4_freshness.py`

| Invariant | Test | Result |
|---|---|---|
| Marker false trusts without invalidation | `test_marker_false_is_trusted_without_invalidation` | PASS |
| Only `atlasrag:query:*` is invalidated; Parent/unrelated keys survive | `test_invalidation_removes_only_query_namespace` | PASS |
| Multi-page SCAN and multiple UNLINK batches | `test_multi_page_scan_and_multiple_unlink_batches` | PASS |
| Empty namespace clears marker | `test_empty_query_namespace_still_clears_marker` | PASS |
| Invalidation failure keeps marker and bypasses reads/writes | `test_invalidation_failure_keeps_marker_and_bypasses_cache` | PASS |
| Partial invalidation / malformed page / invalid key / scan loop / timeout | `test_partial_invalidation_failure_is_bypassed`, `test_invalid_invalidation_responses_keep_marker`, `test_invalidation_timeout_keeps_marker` | PASS |
| Cancellation during invalidation keeps marker true | `test_cancellation_during_invalidation_keeps_marker_true` | PASS |
| Complete invalidation precedes marker clear and confirmation | `test_complete_invalidation_precedes_marker_clear_and_confirmation` | PASS |

### AR4-06 — PostgreSQL marker failure and ambiguous commit

File: `tests/stage10/test_ar4_freshness.py`

| Invariant | Test | Result |
|---|---|---|
| Marker read unavailable/timeout bypasses reads and writes | `test_marker_read_unavailable_bypasses_cache_reads_and_writes`, `test_marker_read_timeout_bypasses` | PASS |
| Canonical marker corruption propagates | `test_canonical_marker_corruption_propagates` | PASS |
| Marker clear set failure keeps marker true | `test_marker_set_failure_keeps_marker_true` | PASS |
| Commit failure rolls back | `test_marker_commit_failure_rolls_back` | PASS |
| Commit applied with lost ack is conservatively BYPASSED, then persisted marker authorizes only an empty/invalidated cache | `test_marker_commit_applied_with_lost_ack_is_conservatively_bypassed` | PASS |
| Confirmatory read failure stays conservative | `test_confirmatory_read_failure_remains_conservative` | PASS |
| Confirmatory read true keeps bypass | `test_confirmatory_true_marker_keeps_cache_bypassed` | PASS |
| Confirmed false after complete invalidation restores trust | `test_confirmed_false_after_complete_invalidation_reestablishes_trust` | PASS |

### AR4-07 — Redis outage and recovery

File: `tests/stage10/test_ar4_lifecycle.py`

| Invariant | Test | Result |
|---|---|---|
| Redis unavailable + marker true: serving continues, all cache reads/writes bypassed; recovery invalidates stale entries before trust | `test_redis_outage_bypasses_cache_and_recovery_removes_stale_entries` | PASS |
| Marker true at restart invalidates before admission | `test_restart_with_marker_true_invalidates_before_cache_trust` | PASS |

### AR4-08 — Concurrent recovery and throttling

Files: `tests/stage10/test_ar4_freshness.py`, existing `tests/stage10/test_freshness.py`

| Invariant | Test | Result |
|---|---|---|
| Concurrent due callers serialize into one recovery | `test_concurrent_due_recovery_is_single_flight` | PASS |
| Callers already waiting on a held recovery lock do not repeat it | `test_due_callers_waiting_behind_held_recovery_run_once` | PASS |
| Concurrent failed recovery is attempted once | `test_concurrent_failed_due_recovery_is_attempted_once` | PASS |
| Failure is throttled; retry after interval succeeds | `test_failed_recovery_is_throttled_then_retried` | PASS |
| Explicit `enforce` is not skipped by lazy throttling | `test_enforce_is_not_throttled_by_lazy_recovery` | PASS |
| Existing serialization regression retained | `test_concurrent_recovery_is_serialized` | PASS |

### AR4-09 — Maintenance, cache commit and late operations

File: `tests/stage10/test_ar4_lifecycle.py`

| Invariant | Test | Result |
|---|---|---|
| Lookup/write occur only while the query lease is active | `test_cache_lookup_and_write_occur_while_query_lease_is_active` | PASS |
| Maintenance waits for a delayed submitted write; the write is then invalidated before serving | `test_maintenance_waits_for_delayed_write_and_invalidates_it` | PASS |
| Drain timeout aborts maintenance and preserves the valid stalled query | `test_maintenance_drain_timeout_preserves_stalled_query` | PASS |
| Invalidation stall keeps admission closed until proven | `test_invalidation_stall_keeps_admission_closed_until_proven` | PASS |
| Canceled invalidation blocks runtime and keeps marker true | `test_cancelled_invalidation_blocks_runtime_and_keeps_marker_true` | PASS |
| Unsafe maintenance cannot authorize cache use | `test_unsafe_maintenance_result_cannot_authorize_cache_use` | PASS |
| Lazy recovery cannot overlap maintenance mutation | `test_lazy_recovery_cannot_overlap_maintenance_mutation` | PASS |

The submitted write settles inside the query lease (`_settle_write` shields and
awaits the write task); a delayed write therefore prevents maintenance from
entering, and shutdown retains resources. No delayed write was observed after a
completed freshness barrier.

### AR4-10 — Bounded settlement and cancellation

Files: `tests/stage10/test_ar4_commit.py`, `tests/stage10/test_ar4_lifecycle.py`

| Invariant | Test | Result |
|---|---|---|
| Stalled write keeps commit pending, then cancellation propagates after settlement | `test_stalled_write_cannot_return_a_stored_outcome` | PASS |
| Repeated cancellation during settlement | `test_repeated_cancellation_during_write_settles_without_hanging` | PASS |
| Shutdown fails, retains resources/ownership and the lease until the write settles; later shutdown succeeds | `test_shutdown_stalled_write_fails_then_recovers_after_settlement` | PASS |
| Drain timeout does not release the query lease | `test_maintenance_drain_timeout_preserves_stalled_query` | PASS |
| No retry loop: one submitted SET attempt per commit | `test_submitted_write_outcome_matrix` (`set_calls == 1`) | PASS |

Composition inspection: the Redis client is created with default redis-py
socket timeout/retry settings (no explicit socket timeout or retry policy).
Bounded caller waiting is the coordinator timeout; it does not prove remote
non-execution, so settlement failure retains resources rather than claiming
success.

Transport settlement update:

```text
AR4-T01 — Redis Transport Settlement

Deterministic Offline Portion: RESOLVED in A-R6 (`AR6-01`..`AR6-05`)
Real Infrastructure Portion: PENDING Stage 10-B (`redis:8.2.10-alpine` integration)
```

### AR4-11 — Namespace isolation and disabled cache

Files: `tests/stage10/test_ar4_freshness.py`, `tests/stage10/test_ar4_identity.py`

| Invariant | Test | Result |
|---|---|---|
| Query namespace only; Parent/unrelated keys preserved; no FLUSH | `test_invalidation_removes_only_query_namespace` | PASS |
| Multi-page/multi-batch invalidation complete | `test_multi_page_scan_and_multiple_unlink_batches` | PASS |
| Malformed/invalid namespace responses rejected | `test_invalid_invalidation_responses_keep_marker` | PASS |
| Disabled cache performs no lookup/publication but freshness still runs | `test_disabled_cache_performs_no_redis_operations_and_freshness_still_runs` | PASS |
| Later enablement requires established freshness | same test (marker cleared before enabled cache is used) | PASS |

### AR4-12 — Complete runtime cache lifecycle

File: `tests/stage10/test_ar4_lifecycle.py`

| Invariant | Test | Result |
|---|---|---|
| Safe startup → Session A miss/store/hit → Session B independent miss → maintenance → marker true/invalidation/trust → old result not returned → new result generated | `test_full_session_cache_lifecycle_and_maintenance_invalidation` | PASS |
| Restart over durable marker=true invalidates before cache trust and regenerates | `test_restart_with_marker_true_invalidates_before_cache_trust` | PASS |
| Redis outage path with stale entries and controlled recovery | `test_redis_outage_bypasses_cache_and_recovery_removes_stale_entries` | PASS |

### A-R4 confirmed defects and regressions

| ID | Defect | Regression(s) | Pre-fix result |
|---|---|---|---|
| A-R4-F01 | `SessionQueryCache.commit` classified a submitted Redis `DependencyError` as BYPASSED, falsely asserting non-execution when SET may have applied | `test_cache_commit_classifies_redis_failure_without_raising` (expectation corrected), `test_dependency_error_without_execution_detail_is_unknown`, `test_submitted_write_outcome_matrix[apply_timeout/apply_connection/reject/unavailable_before]` | BYPASSED for applied-write timeout/connection failures |
| A-R4-F02 | Lazy recovery checked its due throttle before entering the enforcement lock, so callers already queued on a held recovery could each repeat recovery | `test_due_callers_waiting_behind_held_recovery_run_once` | five redundant BYPASSED attempts instead of one |

The F-03 regression remains intact (cache failures do not escape settlement and
are not reported STORED); its expected outcome is now the conservative UNKNOWN.

## A-R5 Parent Cache — COMPLETE (offline)

All A-R5 tests use the real `ParentContextService`, `RedisCacheProvider`,
`CanonicalChunkHeader`, `ChunkRepository`, `validate_text_parent_chunk_header`,
`map_chunk_from_stored`, `RuntimeGate`, `CacheFreshnessBarrier`,
`MaintenanceCoordinator`, `ApplicationCoordinator`, and `build_default_business`
over controlled `FakeRawRedis` / `FakeUnitOfWorkFactory` / `FakeConnection`
boundaries (`tests/stage10/parent_cache_support.py`).

| Requirement | Scenario / invariant | Test(s) | Result |
|---|---|---|---|
| AR5-01 | Production `build_default_business` wires `ParentContextService` with shared `redis_provider`, enabled/disabled toggle, configured TTL, revision/context key isolation, and Query Cache independence | `tests/stage10/test_ar5_parent_cache.py::test_ar5_01_production_builder_wires_parent_cache_with_ttl_and_key_isolation` | PASS |
| AR5-02 | Valid `TEXT_PARENT` hit preserves authoritative canonical provenance, exact content, avoids full canonical content read (`list_by_ids`/`get_by_id`), works with single-header reader, and falls back to full read when partial header omits metadata | `tests/stage10/test_parent_cache.py::test_valid_text_parent_cache_hit_reconstructs_authoritative_provenance`, `tests/stage10/test_ar5_parent_cache.py::test_ar5_02_valid_hit_works_with_single_header_reader_and_partial_header_fallback` | PASS |
| AR5-03 | Miss reads canonical chunk, validates, fills `atlasrag:parent:{revision_id}:{parent_id}` with exact serialized `CachedParentContext` and configured TTL, matches disabled canonical path, and hits next | `tests/stage10/test_ar5_parent_cache.py::test_ar5_03_miss_reads_canonical_fills_exact_payload_ttl_and_hits_next` | PASS |
| AR5-04 | GET unavailable/timeout, malformed JSON, invalid schema, blank content, mismatched doc/rev/context/type, SET failure/timeout/ambiguous ack fall back to canonical without breaking serving; cancellation propagates | `tests/stage10/test_ar5_parent_cache.py::test_ar5_04_get_faults_and_malformed_payloads_fall_back_to_canonical`, `test_ar5_04_mismatched_cached_identity_or_type_falls_back_to_canonical`, `test_ar5_04_put_failures_and_ambiguous_ack_still_return_canonical_evidence`, `test_ar5_04_cancellation_during_redis_propagates` | PASS |
| AR5-05 | Missing document/chunk, stale revision, unpublished revision, wrong document/chunk identity, canonical `TEXT_CHILD` / `TABLE`, invalid `parent_id` fail closed even with valid-looking Redis entry | `tests/stage10/test_ar5_parent_cache.py::test_ar5_05_redis_cannot_resurrect_missing_stale_unpublished_or_wrong_lineage` | PASS |
| AR5-06 | Cache hit matches full `map_chunk_from_stored` validation across metadata, strategy metadata, retrieval role, chunking strategy, provenance type, table_slice, source_anchor, sheet_name, single-line section_path, and chunk_id digest; canonical provenance overrides disagreeing cached provenance; `ChunkRepository` normalizes corrupt header rows to `CanonicalDataError` | `tests/stage10/test_ar5_parent_cache.py::test_ar5_06_cache_hit_matches_full_canonical_metadata_validation`, `test_ar5_06_authoritative_provenance_overrides_cached_provenance`, `test_ar5_06_repository_header_validation_errors_normalize_to_canonical`, `tests/persistence/test_postgres_contracts.py::test_chunk_header_lookup_projects_metadata_without_content_and_normalizes` | PASS |
| AR5-07 | Exact preservation of leading/trailing whitespace, newlines, tabs, and multilingual text; legal whitespace-only nonempty `TEXT_PARENT` returns canonical `LocalEvidence` and bypasses nonblank `CachedParentContext` population | `tests/stage10/test_ar5_parent_cache.py::test_ar5_07_exact_content_preservation_and_whitespace_cacheability` | PASS |
| AR5-08 | Exact ordering preserved for `TEXT_PARENT`-only, `TABLE`-only, `TEXT_PARENT->TABLE`, `TABLE->TEXT_PARENT`, `TEXT_PARENT->TABLE->TEXT_PARENT`, multi-doc, and mixed hit/miss batches; mixed batches cache `TEXT_PARENT` without caching `TABLE`; duplicate `context_id` across complete input rejected before Redis/DB | `tests/stage10/test_ar5_parent_cache.py::test_ar5_08_ordering_mixed_hits_misses_multi_doc_and_duplicates` | PASS |
| AR5-09 | `TABLE` remains canonical with `TableSlice` and `source_anchor`, ignores unrelated Redis keys, performs zero `get_parent`/`put_parent`, and fails closed on missing/invalid `TABLE` | `tests/stage10/test_ar5_parent_cache.py::test_ar5_09_table_remains_canonical_and_ignores_unrelated_redis_keys` | PASS |
| AR5-10 | Disabled cache and `cache=None` perform zero GET/SET/DELETE/UNLINK calls while returning valid ordered canonical `TEXT_PARENT` and `TABLE` evidence | `tests/stage10/test_ar5_parent_cache.py::test_ar5_10_disabled_and_no_provider_skip_all_redis_ops` | PASS |
| AR5-11 | Delayed Redis GET and SET hold zero open PostgreSQL UoWs; document reads deduplicated; misses and headers batched; cancellation leaks zero UoWs/Query Leases; maintenance waits in `DRAINING` during active expansion | `tests/stage10/test_ar5_lifecycle.py::test_ar5_11_delayed_redis_get_and_set_hold_zero_postgres_uows`, `test_ar5_11_cancellation_during_delayed_expansion_never_leaks_uow_or_lease`, `test_ar5_11_maintenance_cannot_mutate_while_admitted_parent_expansion_runs` | PASS |
| AR5-12 | Full lifecycle: R1 miss/fill → R1 hit → maintenance publishes R2 & invalidates only `atlasrag:query:*` → stale R1 rejected → R2 miss/fill → R2 hit → deleted doc rejected despite R2 Redis entry; cache-enabled matches disabled | `tests/stage10/test_ar5_lifecycle.py::test_ar5_12_revision_upgrade_deletion_and_query_cache_invalidation_lifecycle` | PASS |

### A-R5 confirmed defects and regressions

Each regression below was executed against the pre-A-R5 codebase (`6ffab31`); the
pre-fix result is the directly observed failure before applying minimal fixes.

| ID | Defect | Regression(s) | Pre-fix result |
|---|---|---|---|
| A-R5-F01 | `CanonicalChunkHeader` omitted `sheet_name`, `metadata`, and `strategy_metadata`, so cache hits skipped `ChunkPersistenceMetadataV1`, `ChunkStrategyMetadataV1`, `normalize_section_path`, and `derive_chunk_id` checks | `test_cache_hit_rejects_corrupt_canonical_persistence_metadata` (`test_ar5_06_cache_hit_matches_full_canonical_metadata_validation`) | Falsely returned cached evidence (`DID NOT RAISE CanonicalDataError`) |
| A-R5-F02 | `ParentContextService.expand` required `all(EvidenceType.TEXT_PARENT)` so mixed `TEXT_PARENT`/`TABLE` batches bypassed Parent Cache completely | `test_mixed_text_parent_and_table_uses_parent_cache_without_caching_table` (`test_ar5_08_ordering_mixed_hits_misses_multi_doc_and_duplicates`) | Zero Redis `get_parent` / `put_parent` calls on `TEXT_PARENT` items |
| A-R5-F03 | Cache miss fill unconditionally constructed `CachedParentContext` (`_nonblank` content validator), crashing on canonically valid whitespace-only nonempty `TEXT_PARENT` chunks | `test_whitespace_only_canonical_parent_returns_evidence_without_cache_fill` (`test_ar5_07_exact_content_preservation_and_whitespace_cacheability`) | Raised `pydantic.ValidationError` instead of returning canonical evidence |
| A-R5-F04 | `_expand_text_cached` opened the PostgreSQL UoW before `cache.get_parent`, issued repeated document lookups per ref, and used single-item `get_by_id` on misses instead of batched `list_by_ids` | `test_redis_lookup_runs_outside_postgres_uow_and_deduplicates_db_reads` (`test_ar5_11_delayed_redis_get_and_set_hold_zero_postgres_uows`) | `active_uows > 0` during Redis GET; redundant DB calls |
| A-R5-F05 | `ChunkRepository.get_canonical_context_header` called `CanonicalChunkHeader.model_validate` directly without `validate_canonical_row` | `test_repository_get_canonical_context_header_normalizes_corrupt_row` (`test_ar5_06_repository_header_validation_errors_normalize_to_canonical`) | Raised `pydantic.ValidationError` instead of `CanonicalDataError` |

## A-R6 Final offline correctness and composition contracts — COMPLETE (offline)

Added deterministic verification across
`tests/stage10/test_ar6_redis_transport.py`,
`tests/stage10/test_ar6_freshness_lifecycle.py`,
`tests/stage10/test_ar6_composition_contracts.py`, and
`tests/contract/test_config_and_fingerprints.py`:

| Requirement | Scenario / invariant | Test(s) | Result |
|---|---|---|---|
| `AR6-01` | Production Redis client sets explicit `socket_connect_timeout`, `socket_timeout`, `socket_keepalive=True`, `health_check_interval`, `retry_on_timeout=False`, `retry_on_error=[]`, `Retry(NoBackoff(), 0)`; operational config bounds & fingerprint exclusion; Redis fence error/ACK boundary translation | `test_ar6_01_production_redis_client_uses_bounded_no_retry_transport`, `test_ar6_01_redis_fence_primitives_enforce_strict_error_and_ack_contracts`, `test_runtime_operational_redis_transport_bounds`, `test_runtime_operational_redis_transport_excluded_from_fingerprints` | PASS |
| `AR6-02` | Server-side Lua write fencing (`QUERY_WRITE_FENCE_KEY`): missing/revoked/rotated/corrupt fence blocks reads and causes atomic Lua rejection (`BYPASSED` / `0`); unfenced callers rejected when active fence exists | `test_ar6_02_fenced_cache_requires_fence_and_rejects_stale_or_revoked_tokens`, `test_ar6_02_put_fenced_query_direct_error_boundaries` | PASS |
| `AR6-03` | Delayed remote execution (`DelayedRemoteRawRedis`) after `UNKNOWN` timeout/connection/bad-ack and `QueryLease` release, revocation prior to marker clear, cancellation settlement, `UNSAFE`->`BLOCKED`->`recover()`, restart with `marker=False`, and `atlasrag:parent:*` preservation | `test_ar6_03_delayed_remote_write_after_unknown_and_lease_release_rejected_after_maintenance[timeout/connection/bad_ack]`, `test_ar6_03_delayed_remote_write_rejected_before_marker_clear_when_fence_revoked`, `test_ar6_03_cancellation_after_submitted_write_rejected_when_remote_flushes_post_maintenance`, `test_ar6_03_pre_restart_unknown_write_rejected_after_restart_with_marker_false`, `test_ar6_03_parent_cache_survives_delayed_writes_and_fence_lifecycle`, `test_ar6_03_standalone_unfenced_write_rejected_if_fence_established_before_flush`, `test_ar6_03_unsafe_maintenance_blocks_then_recover_rotates_fence_and_rejects_delayed_write` | PASS |
| `AR6-04` | Exact `marker=True` order (`revoke_query_write_fence -> invalidate_queries -> establish_query_write_fence -> PG marker=False -> confirm read`), failure preservation at each step, `CanonicalDataError` propagation, and startup outage rotation on recovery | `test_ar6_04_marker_true_enforces_exact_fence_revoke_sweep_establish_pg_clear_order`, `test_ar6_04_failure_during_fence_revoke_sweep_or_establish_keeps_marker_true_and_blocks_stale_writes`, `test_ar6_04_canonical_pg_error_always_propagates_and_outage_startup_rotates_on_recovery` | PASS |
| `AR6-05` | Periodic `recover_if_due()` inside concurrent `QueryLease` preserves live fence on `marker=False` while re-establishing after empty Redis restart; Parent Cache survives full maintenance drain and invalidation lifecycle | `test_ar6_05_periodic_recover_if_due_preserves_active_fence_for_inflight_concurrent_queries`, `test_ar6_05_periodic_recover_if_due_reestablishes_missing_fence_after_redis_flush`, `test_ar6_05_parent_cache_and_maintenance_exclusion_preserved_under_full_lifecycle` | PASS |
| `AR6-06` | `startup_attempts` / `startup_retry_delay_seconds` retry transient dependency failures cleanly and abort fast on non-transient bugs; BGE reranker init failures soft-degrade preserving `SERVING`/`hard_healthy`; `MilvusIndexProvider` uses `COLLECTION_NAME="atlas_chunks"` + `analyzer=bm25_analyzer`; `HealthRegistry` probes SearXNG | `test_ar6_06_startup_retries_transient_hard_dependencies_and_cleans_up_on_exhaustion`, `test_ar6_06_reranker_failure_and_searxng_failure_soft_degrade_preserving_hard_readiness[error0/1/2]` | PASS |
| `AR6-07` | Unsupported semantic fields in `ModelConfig`, `PartIConfig`, `RetrievalConfig`, `AnswerConfig` and mismatched `SharedResources` identities/dimensions/analyzers raise `ConfigurationError`; production `build_default_business` enforces `require_write_fence=True` | `test_ar6_07_unsupported_semantic_configs_fail_explicitly_with_configuration_error`, `test_ar6_07_shared_resource_identity_and_index_contract_mismatches_raise_configuration_error`, `test_ar6_07_production_builder_requires_write_fence_before_query_cache_store` | PASS |
| `AR6-08` | `2C + 2` PG pool bound, flock exclusion, `ResourceGuard` GPU concurrency, secret redaction in `ConfigurationError`, operational config fingerprint exclusion | `test_ar6_08_pool_bound_ownership_lock_and_secret_redaction`, `test_ar6_08_resource_guard_bounds_concurrency_and_operational_fields_excluded_from_fingerprints` | PASS |

## A-R6 Final offline verification

| Check | Result |
|---|---|
| `make test-stage10` | PASS — 304 tests |
| `make verify` | PASS — 3612 passed, 1 skipped (`deepeval` absent), 119 deselected |
| `ruff check .` / `ruff format --check .` (477 files) / `mypy` (390 source files) | PASS |
| `make architecture-check` / `git diff --check` | PASS |
| Stage 9 raw artifact integrity | PASS — 12/12 SHA-256 and size matches |
| `AR4-T01` Redis transport settlement | OFFLINE_AND_REAL_INFRA_VERIFIED (`A-R6` + Stage 10-B) |
| Real isolated PostgreSQL/Milvus/Redis integration | COMPLETE (Stage 10-B — see `docs/stages/stage-10b-completion.md`) |
| Independent review | NOT_STARTED (Stage 10-C — see `docs/stages/stage-10-review/REVIEW_SUMMARY.md`) |

*Stage 10-B offline regression additions incorporated into `AR6-04` and
`AR6-07` above:* `CacheFreshnessBarrier.active_write_fence` +
`SessionQueryCache(expected_write_fence=...)` fail-closed gate when startup
fence reset is `BYPASSED` (`B10-09-F01`), and `composition._device_matches`
reconciling `model_device="cuda"` with resolved provider device `"cuda:0"`
(`B10-04-F01`).
