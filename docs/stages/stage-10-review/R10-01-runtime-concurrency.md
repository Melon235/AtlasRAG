# R10-01 — Runtime Lifecycle & Concurrency

- Review ID: R10-01
- Reviewed commit: `622434e8de4c65c4b919e34b2c5bfb1481f998aa`
- Independent reviewer: **INTERRUPTED / NO REVIEW EVIDENCE** (subagent stopped without findings)
- Self-review: `SELF_REVIEW` (not a substitute for independence)
- Scope: `gate.py`, `tasks.py`, `coordinator.py`, `maintenance.py`, `health.py`, ownership, core tests
- Requirements: S10-05, S10-05a, S10-06, S10-07, S10-11, S10-17, S10-18

## Findings

### F-01 — CRITICAL — double ownership acquisition
`ApplicationComposition.start` acquired the ownership lock, then `ApplicationCoordinator._start` acquired it again. Result: every composition start fails with `RUNTIME_OWNERSHIP_CONFLICT`.
Status: **FIXED**; coordinator now skips acquisition when `CoordinatorDependencies.ownership_acquired=True`; composition uses that path.

### F-02 — HIGH — maintenance freshness failure leaves MAINTENANCE
`MaintenanceCoordinator._finish` did not transition to BLOCKED when `CacheFreshnessBarrier.enforce()` raised.
Status: **FIXED**; barrier failure now blocks the lease and re-raises.

### G-01 — HIGH verification gap — shutdown settlement
No deterministic test proves cancellation of a worker that never settles retains resources/ownership and raises `RuntimeShutdownFailedError`.
Status: **OPEN**.

### G-02 — HIGH verification gap — admission/registration race
No test proves shutdown cannot observe an empty registry while an admitted operation starts.
Status: **OPEN**.

### G-03 — MEDIUM verification gap — stale health probe
No test proves a late generation cannot reopen admission after a lifecycle transition.
Status: **OPEN**.

## Review status

Independent review incomplete. Self-review found and fixed two defects; three mandatory evidence gaps remain. **R10-01: PARTIAL / INDEPENDENT REVIEW UNAVAILABLE.**
