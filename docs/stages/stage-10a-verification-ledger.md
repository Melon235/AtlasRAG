# Stage 10-A Offline Verification Ledger

Status: **PARTIAL**. Thirty deterministic Stage 10 tests pass; several mandatory
matrices remain MISSING_TEST. No offline PASS is claimed for missing rows.

## A-R1 Production composition

| Scenario | Test | Result |
|---|---|---|
| Default builder assembles Part I/II services | `test_default_builder_constructs_existing_part_i_and_part_ii_services` | PASS |
| Default production entry exists | `test_default_production_composition_is_available` | PASS |
| Shared fields (MinerU/HTTPs) present | same | PASS |
| Composition-to-coordinator startup with technical fakes | — | MISSING_TEST |
| Partial bootstrap rollback through composition | — | MISSING_TEST |
| Single ownership at composition boundary | coordinator regression only | PARTIAL |

## A-R2 Admission, health, shutdown

| Scenario | Test | Result |
|---|---|---|
| Atomic admission/release | `test_gate_admission_and_exactly_once_release` | PASS |
| Registration failure releases lease | `test_registration_failure_releases_query_lease` | PASS |
| Shutdown unsettled worker retains resources/lock/error | `test_shutdown_unsettled_worker_retains_resources_and_raises` | PASS |
| Drain timeout preserves query | `test_gate_drain_timeout_restores_serving_and_preserves_query` | PASS |
| Recovery abort returns BLOCKED | `test_gate_recovery_abort_returns_blocked` | PASS |
| Shutdown terminal | `test_gate_shutdown_is_terminal` | PASS |
| Admission vs maintenance/shutdown race | — | MISSING_TEST |
| Delayed health probe after lifecycle transition | — | MISSING_TEST |
| Health probes settle before resource close | — | MISSING_TEST |

## A-R3 Startup source safety

| Scenario | Test | Result |
|---|---|---|
| Missing published source | `test_source_safety_blocks_missing_published_source` | PASS |
| Changed available source | `test_source_safety_allows_available_changed_source` | PASS |
| Empty consistent KB | `test_empty_consistent_kb_is_safe` | PASS |
| Unreadable source | `test_unreadable_published_source_is_unsafe` | PASS |
| Inaccessible root hard failure | `test_inaccessible_root_is_a_hard_failure` | PASS |
| Interrupted deletion before/after Milvus/PG | fake projection matrix | MISSING_TEST |
| Required deletion failure + restart | fake sync/restart matrix | MISSING_TEST |

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
| Cache-hit through query lease | — | MISSING_TEST |
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
| `make test-stage10` | PASS — 30 tests |
| `pytest` full | PASS — 3324 passed, 1 skipped, 108 deselected |
| ruff / mypy / architecture / format / diff | PASS |
| Real isolated infrastructure | NOT_EXECUTED (Stage 10-B) |
| Independent review | NOT_EXECUTED (Stage 10-C) |
