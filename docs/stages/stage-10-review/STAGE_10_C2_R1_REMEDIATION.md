# Stage 10-C2-R1 Final Startup Ownership Remediation

- **Starting Baseline (`HEAD`):** `5f74b201b076850bc18da9ecff12b56acde0260b`
- **Accepted Base (`origin/main`):** `2f3f487f9346a8067faabca1ee49820035d8ed8d`
- **Branch / PR:** `stage/10-part-iv-application-runtime` / `PR #11` (`OPEN / UNMERGED`)
- **Stage 10-C2-R1 Status:** **COMPLETE**
- **`F-R10-01-01` / `C3-RISK-01` Status:** **`FIX_IMPLEMENTED` / `REGRESSION_PASS` / `PENDING_INDEPENDENT_REVIEW`**

## 1. Root Cause & Localized Correction

- **Confirmed Defects (`5f74b201`, [`src/atlasrag/runtime/composition.py`](../../../src/atlasrag/runtime/composition.py)):**
  1. `_open_pool()` kept `pool = PostgresPool(...)` in a local variable until `_retry_startup_dependency` returned, so a failed or cancelled `pool.close()` left `self._pool = None` and left no cleanup failure recorded on the `ApplicationComposition` instance; outer `_close_resources()` then returned `None` and `self._ownership.release()` released `RuntimeOwnershipLock` prematurely (`C3-RISK-01`).
  2. Inside `_open_pool()`'s `except BaseException as cleanup:` handler (`line 282`), bare `raise` re-raised the inner cleanup `OSError` instead of the primary `asyncio.CancelledError` when `pool.open()` was cancelled and `pool.close()` raised.
- **Surgical Fix:**
  1. Added `self._pool_cleanup_failed = False` on `ApplicationComposition` and guarded `_open_pool()` against running while `self._pool is not None`, `self._pool_cleanup_failed`, or `self._cleanup_error is not None`.
  2. Assigned `self._pool = pool` immediately before `await pool.open()`. If `await pool.open()` fails/cancels and `await pool.close()` succeeds, `self._pool` is cleared to `None` so retries (or outer lock release upon exhausted retries / clean cancel) proceed cleanly. If `await pool.close()` fails or is cancelled, `self._pool` remains tracked (`pool`), `self._pool_cleanup_failed = True` and `self._cleanup_error = cleanup` are recorded, and bare `raise` was replaced with `raise primary` when `primary` is `asyncio.CancelledError`.
  3. Updated `_close_resources()` to seed `first`/`cancelled` from pre-existing `self._cleanup_error` and skip duplicate `self._pool.close` invocation when `getattr(self, "_pool_cleanup_failed", False)` is true, guaranteeing unsettled failures remain retained across empty/clean/failing/repeated resource closes without dropping `object.__new__` test compatibility.

## 2. Targeted Regression Matrix (`C3R-01`–`C3R-08`)

Implemented in [`tests/stage10/test_f_r10_01_lifecycle_health_guard.py`](../../../tests/stage10/test_f_r10_01_lifecycle_health_guard.py):

- **`C3R-01`** (`test_c3r_01_recoverable_pool_open_failure_retry_and_exhaustion`): Confirmed clean `pool.close()` clears `self._pool`, permits retry to full `SERVING` startup, and releases `RuntimeOwnershipLock` once when retries exhaust cleanly.
- **`C3R-02` / `C3R-03` / `C3R-05`** (`test_r10_03_f02_open_pool_aborts_retries_on_close_cancel_or_error`): Re-verified pre-fix failure on `5f74b201` (`comp._pool` was `None`, lock released); post-fix verifies `open_attempts == 1`, `close_attempts == 1`, `comp._pool` retained, `comp._cleanup_error` recorded, real `RuntimeOwnershipLock` retained, and competing `ApplicationComposition.start()` + `RuntimeOwnershipLock.acquire()` raise `RuntimeOwnershipConflictError`.
- **`C3R-04` / `C3R-05`** (`test_c3r_04_pool_open_cancel_with_close_failure_preserves_cancel_and_lock`): Re-verified pre-fix `5f74b201` raised `OSError` and released lock; post-fix confirms outward exception is `asyncio.CancelledError` with sanitized `"OSError"` note (zero `"top-secret-pw"`), `open_attempts == 1`, `comp._pool` retained, `RuntimeOwnershipLock` held, and competing lock raises `RuntimeOwnershipConflictError`.
- **`C3R-06`** (`test_c3r_06_pool_open_cancel_with_clean_close_releases_lock`): Confirms `CancelledError` propagates, zero retry, `comp._pool is None`, and lock releases once.
- **`C3R-07`** (Existing rollback/shutdown tests across `test_f_r10_01_lifecycle_health_guard.py`, `test_ar6_composition_contracts.py`, `test_production_startup.py`, `test_runtime_core.py`, `test_runtime_lifecycle.py`): `84/84` targeted tests pass unchanged.
- **`C3R-08`** (`test_c3r_08_close_resources_retains_preexisting_cleanup_error_across_variants`): Re-verified pre-fix `5f74b201` Variant A falsely returned `None`; post-fix confirms pre-existing `_cleanup_error` is preserved across empty/clean/failing secondary closes and repeated calls with sanitized notes and zero duplicate `pool.close()` calls.

## 3. Verification Summary

- `make test-stage10`: **PASS** — `352 passed in 13.22s`
- `make verify`: **PASS** — `3660 passed, 1 skipped` (`deepeval`), `120 deselected`; `ruff format --check .` (`487` files); `ruff check .`; `mypy src/atlasrag scripts tests` (`396` source files); `make architecture-check`; `git diff --check`
- Stage 9 raw artifact integrity ([`docs/stages/stage-09-closeout/artifact_integrity.json`](../../stage-09-closeout/artifact_integrity.json)): **PASS** — `12/12` SHA-256 & byte-size matches
- `make test-stage10-integration`: **PASS** — `12 passed in 734.68s (0:12:14)` across isolated `postgres:17.8-alpine`, `redis:8.2.10-alpine`, `milvusdb/milvus:v2.6.20`; `Stage 10-B cleanup_evidence: project=atlasrag-stage10b-local-20261127015818-948771-5a5b7bd4 run_id=atlasrag_s10b_477d8a7611edd3e727c24d20 containers_removed=5 volumes_removed=4 networks_removed=1 remaining=0`

Five previously closed findings (`C10-RISK-01`, `F-R10-02-01`, `F-R10-01-02`, `F-R10-01-03`, `F-R10-02-02`) remain preserved and unaffected. `G-07` and `G-10` remain **OPEN** pending Stage 10-C3-R1 independent re-review.
