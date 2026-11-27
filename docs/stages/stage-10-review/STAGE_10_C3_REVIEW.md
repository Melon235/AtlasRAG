# Stage 10-C3 Independent Remediation Re-Review Record

- **Baseline Main Commit:** `2f3f487f9346a8067faabca1ee49820035d8ed8d`
- **Historical C1 Candidate (`HEAD`):** `3a9de079281118879f247abc6e9b38a3239f7d5a`
- **Reviewed Stage 10-C3 Candidate (`HEAD`):** `5f74b201b076850bc18da9ecff12b56acde0260b`
- **Branch / PR:** `stage/10-part-iv-application-runtime` / `PR #11` (`OPEN / UNMERGED`)
- **Stage 10-C3 Verdict:** **FAIL** (`FIX_REQUIRED` — `5` CLOSED, `1` STILL OPEN)
- **Independent Reviewers:** Four read-only `research` tracks (`R10-01`, `R10-02`, `R10-03`, `R10-04`) plus read-only coordinator verification

## 1. Original Six Blocking Findings — Stage 10-C3 Disposition

| ID | C3 Disposition | Summary of Independent Finding |
|---|---|---|
| `C10-RISK-01` | **INDEPENDENTLY VERIFIED / CLOSED** | Fail-closed `scripts/stage10_isolation.py`, `scripts/stage10_verify.sh`, `Makefile`, `.github/workflows/integration.yml`, `tests/integration/conftest.py`, `tests/integration/stage10_helpers.py`, and module-autouse ownership guards prevent shared-resource reuse/destruction. Two minor non-blocking defense-in-depth notes recorded (standalone `establish` invocation and `COMPOSE_FILE` / `HostConfig.Mounts`). |
| `F-R10-02-01` | **INDEPENDENTLY VERIFIED / CLOSED** | `GlobalKnowledgeVerificationService._load_one` uses `map_current_document_from_stored`, keeping valid published `R1` `SAFE_TO_SERVE` and retrievable alongside failed/interrupted `R2` (`test_f_r10_02_01_*` and live PG/Milvus `test_b10_13`) while remaining fail-closed on canonical/projection/residue faults. |
| `F-R10-01-01` (`+ R10-03-F02`) | **STILL OPEN** | Direct coordinator rollback, conflict skip, and retry-abort landed, **but `C3-RISK-01` confirmed `CONFIRMED_DEFECT`**: when `pool.open()` fails/cancels inside `ApplicationComposition.start._open_pool()` and `pool.close()` fails or is cancelled, `self._pool` remained `None`, so outer `_close_resources()` succeeded silently and `self._ownership.release()` prematurely released `RuntimeOwnershipLock`. Furthermore, bare `raise` at `composition.py:282` overwrote `asyncio.CancelledError` with `OSError` when `pool.open()` was cancelled and `pool.close()` failed. |
| `F-R10-01-02` | **INDEPENDENTLY VERIFIED / CLOSED** | `HealthRegistry.refresh_if_stale()` validates `lifecycle_generation`, coordinator rejects cross-epoch mismatches cleanly with bounded retry for stale unhealthy snapshots, and single-threaded `asyncio` transition from validation to `acquire_query()` has no suspension point. |
| `F-R10-01-03` | **INDEPENDENTLY VERIFIED / CLOSED** | Single shared `ResourceGuard.begin_shutdown()` wired into `CoordinatorDependencies` and `ApplicationComposition.start`, keeping admitted multi-op drain open during `wait_empty()`, closing on cancellation/cleanup boundary, and rejecting subsequent guarded operations. |
| `F-R10-02-02` | **INDEPENDENTLY VERIFIED / CLOSED** | `is_cache_key_component_safe` bypasses Redis/PG cleanly across `lookup`, `prepare`, `commit`, and `store` for domain-valid key-unsafe `session_id` values without altering `UserTurnRequest` or key formats. |

## 2. Stage 10-C3 Matrix & Verification Summary (`5f74b201`)

- **Acceptance Matrix:** `21/23` satisfied within approved scope (`S10-02` and `S10-03` `FIX_REQUIRED / PARTIAL` pending `F-R10-01-01` / `C3-RISK-01`; `G-07` reopened/partial; `G-10` OPEN; `AR4-T01` `OFFLINE_AND_REAL_INFRA_VERIFIED`).
- **Executed Offline Verification (`5f74b201`):** `make test-stage10` (`352` post-remediation vs `348` at `5f74b201`), `make verify` (`3656 passed, 1 skipped, 120 deselected` at `5f74b201`), `ruff`, `mypy` (`396` source files), `make architecture-check`, Stage 9 `12/12` artifact integrity.
- **Historical Local & Remote Real-Infrastructure Evidence (`5f74b201`):** `make test-stage10-integration` (`12 passed in 741.24s`), `Push CI` (`45880305778`), `Push Integration` (`45880305880`), `PR CI` (`45880310483`), `PR Integration` (`45880310535`), and Stage 10 real-infra jobs `140117689312` & `140117704794` all succeeded.
- **Minor Non-Blocking Doc Drift Flagged:** Stale pre-C2 counts/wording noticed in `stage-10a-verification-ledger.md:3-8`, `stage-10b-completion.md:51`, and `stage-10-acceptance.md:10-32`.
