# AtlasRAG Stage 5 — Part I: Sync / Recovery / Maintenance

## Status

**COMPLETE — all local gates pass; the branch was published normally; final-HEAD
push and pull-request CI/Integration checks are green; pull request #6 is open
and unmerged.**

Base: `origin/main` at
`23def47e4ceaa4b8332b6db575d9534d6177fc2b` (Stage 4 merge, PR #5).

## Authority and approved ownership

The tracked Architecture Manual is authoritative. Stage 5 follows the approved
implementation plan
`docs/superpowers/plans/2026-11-10-stage-05-part-i-sync-recovery-maintenance.md`.

Architecture Manual §24A.1 sketches `enter_maintenance` and `resume_serving`
inside `index_graph`. Final RuntimeGate / MaintenanceCoordinator ownership puts
those lifecycle transitions in the Part IV application coordinator (Stage 10).
Stage 5 therefore implements only the knowledge-maintenance business workflow
and returns a `ServingSafety` verdict. It does not enter/leave SERVING and does
not create a second runtime composition root.

Database migration: **NONE**. Frozen Stage 1 public contracts, Stage 2
schema/UoW, Stage 3 canonical IDs/chunk semantics, and Stage 4 publish
verification semantics are preserved.

## Implemented scope

- complete fail-closed Knowledge Directory scanner and immutable
  `ScanSnapshot`;
- canonical PostgreSQL `CanonicalDatabaseSnapshot`;
- pure deterministic `SyncPlanner` and immutable `SyncPlan`;
- `BUILD` / `DELETE` / `UNCHANGED` with `NEW_SOURCE`, `CONTENT_CHANGED`,
  `PIPELINE_CHANGED`, `RETRY_FAILED_BUILD`;
- bounded build execution reusing `DocumentBuildRunner` /
  `document_build_subgraph`;
- durable `PARSED`/`CHUNKED` resume and `INDEXING` normalization;
- `StartupReconciliationService` with the complete recovery matrix;
- candidate residue cleanup and exact-set current-index repair;
- Milvus-first missing-source deletion with PostgreSQL deletion + freshness
  marker in one transaction;
- global exact-set published-knowledge verification;
- separated `SyncOutcome` / `ServingSafety`;
- six-node `index_graph`;
- Stage 5 offline and real PostgreSQL/Milvus integration gates.

Not implemented: RuntimeGate/MaintenanceCoordinator, SERVING/DRAINING
admission, health registry, CLI/HTTP server, production scheduler, Redis
invalidation execution, Part II, benchmark harness, Stage 6.

## Scanner contract

The scanner recursively enumerates the resolved Knowledge Directory without
following symlinks, rejects symlinked/invalid/special entries, ignores
unsupported regular files deterministically, constructs Stage 3 `SourceFile`
objects for supported sources, streams SHA-256 hashes, checks size/mtime/inode
stability around hashing, and returns entries sorted by canonical relative
path.

Any root, permission, disappearance, change-during-hash, symlink, special-file,
duplicate-identity, or unreadable-source failure raises classified `ScanError`.
No partial snapshot is returned; a failed scan cannot produce a plan or delete.

## Planner contract

`SyncPlanner` is pure (no filesystem, database, Milvus, parser, embedding, or
mutation). Inputs are `ScanSnapshot`, `CanonicalDatabaseSnapshot`, and the
target pipeline fingerprint. Output actions are ordered BUILD (sorted path),
DELETE (sorted path), UNCHANGED (sorted path), and every BUILD revision is bound
to `derive_revision_id(document_id, content_hash, pipeline_fingerprint)`.

Rules: a present source with no published current revision builds
(NEW_SOURCE; RETRY_FAILED_BUILD when a prior classified FAILED exists); changed
content builds CONTENT_CHANGED; changed pipeline builds PIPELINE_CHANGED;
matching current builds nothing (UNCHANGED); a source absent after a complete
scan is DELETE. A failed R2 with a healthy R1 is UNCHANGED. Rename is DELETE old
+ BUILD new. Stale non-failed building ownership is a `SyncPlanError`.

## Reconciliation matrix

`StartupReconciliationService` validates envelope/revision ownership and
normalizes durable boundaries without auto-resuming FAILED builds:

| State | Action |
| --- | --- |
| RECEIVED / PARSING | delete incomplete candidate; restore READY if current exists else RECEIVED |
| PARSED | validate elements; keep as PARSED resume target when source/pipeline match |
| CHUNKING | delete partial chunks; restore PARSED; resume target |
| CHUNKED | validate chunks/lineage; keep as CHUNKED resume target |
| INDEXING | delete candidate Milvus revision; prove absence; restore CHUNKED; resume target |
| FAILED | preserve failure facts; remove candidate Milvus residue; no auto-resume |
| READY | validate current revision/slot/no building; verify current Milvus; repair if exactly reproducible |

Source hash or pipeline mismatch discards a non-FAILED candidate while
preserving current. Impossible states (current == building, slot/triple
mismatch, READY without valid current, corrupt lineage, unknown metadata,
unreproducible current index) return BLOCKED without deleting last-good data.
Non-current/orphan Milvus rows are removed by exact document/revision scope.

## Resume and current repair

- PARSED resume reconstructs `DocumentContainer` from persisted PostgreSQL
  elements, marks CHUNKING, chunks, persists CHUNKED, then reuses Stage 4
  index/verify/publish. No parser rerun.
- CHUNKED resume re-reads persisted chunks, validates canonical lineage, then
  reuses Stage 4 index/verify/publish. No parse/chunk rerun.
- INDEXING is first normalized to CHUNKED with its partial Milvus set deleted.
- Source mutation discards the candidate and leaves current unchanged.
- Current repair never impersonates a building candidate. It requires the
  stored current pipeline, chunk strategy/profile, representation, model,
  tokenizer, dimension, analyzer, and vector identity to be exactly
  reproducible, rebuilds only the Milvus current revision, verifies the complete
  projection, and leaves PostgreSQL ownership/marker unchanged.

## Deletion and global safety

Deletion is planned only after a complete scan. Per document it acquires the
same advisory lease, revalidates the source still absent and state unchanged,
deletes all Milvus document records, proves exact absence with a Strong
projection scan, then deletes chunks/elements/document and sets
`query_cache_invalidation_required=true` in one PostgreSQL transaction. If
PostgreSQL fails after Milvus succeeds, the result is FAILED and the serving
verdict UNSAFE; a retry is idempotent.

`GlobalKnowledgeVerificationService` enumerates the complete Milvus collection
and every published current revision, validating exact IDs/count, no
duplicates, no TEXT_PARENT rows, full scalar/typed metadata projection,
canonical lineage, non-current/orphan residue, and required deletion failures.
It never derives safety from counts alone.

`SyncOutcome` (SUCCESS/PARTIAL_SUCCESS/FAILED) is independent from
`ServingSafety` (SAFE_TO_SERVE/UNSAFE_TO_SERVE). A failed R2 with exact R1 and
no required deletion is PARTIAL_SUCCESS + SAFE. A failed required deletion or
unresolved current mismatch is UNSAFE. Technical failures propagate; they never
yield SAFE.

## index_graph

State is closed and dependency-free:

```text
IndexGraphInput  = request
IndexGraphState  = request, reconciliation_result?, scan_snapshot?,
                   sync_plan?, build_summary?, deletion_summary?,
                   final_result?
IndexGraphOutput = final_result
```

Topology:

```text
reconcile_runtime → scan_directory → build_sync_plan
→ execute_builds → execute_deletions → final_verify → END
```

Classified reconciliation/scan/plan blocking routes directly to END with a
FAILED/UNSAFE result and no build/deletion side effects. The graph has no
checkpointer, provider, connection, lease, vector, receipt, or exception in
State.

## Concurrency and failure handling

- Different documents build concurrently under a bounded semaphore; the same
  document is serialized by the PostgreSQL advisory lease.
- Resume/reset/cleanup share the same document lease via the injected recovery
  runner.
- Deletion uses the same lease manager.
- Qwen/index providers remain process-shared; no per-document model load.
- One document's classified build failure does not cancel independent builds.
- Controlled build failures record only Stage 4 failure stages; technical
  dependency/timeouts/cancellation propagate or appear as technical summary
  items, never as business FAILED. Cancellation settles issued work.

## Commands and CI

```text
make test-stage5
make test-stage5-integration
make stage5-verify
```

`make verify` remains offline, CPU-safe, model-free, Docker-free, and
network-free. Integration uses real PostgreSQL/Milvus and deterministic fake
embedding. The new CI job uses frozen dependencies and pins every third-party
action by SHA.

## Evidence

| Gate | Fresh result on the Stage 5 tree |
| --- | --- |
| `make verify` | PASS — 3112 passed, 92 deselected in 143.86s; 247 formatted files, Ruff clean, mypy 227 files, architecture checks clean |
| `make stage5-verify` | PASS — 3112 offline + 75 focused Stage 5 + 8 real PostgreSQL/Milvus tests in 573.06s; volume-preserving cleanup |
| Stage 5 focused suite | PASS — 75 tests |
| Stage 5 real integration | PASS — 8 tests: empty KB, build/unchanged/content-change/rename/delete, current-index repair, CHUNKED resume, INDEXING residue reindex, PARSED resume, failed required deletion, unknown-orphan cleanup |
| `make stage3-parser-smoke` | PASS — 1 passed in 3.10s; real MinerU 4.0.11, one page, seven elements |
| `make stage2-verify` | PASS — 28 real integration tests in 156.69s |
| `make stage3-verify` | PASS — 529 focused in 6.36s + 18 real PostgreSQL/Docling tests in 9.01s |
| `make stage4-verify` | PASS — 654 focused + 36 real PostgreSQL/Milvus tests in 848.57s |
| `make stage4-model-smoke` | PASS — 1 passed in 36.80s; pinned Qwen revision, `cuda:0`, float16/SDPA, 1024 dim, whitespace/order/finiteness |
| `make architecture-check` / `git diff --check` | PASS |

### Final audit corrections

An independent read-only audit found one blocker and several safety gaps. All
were corrected before publication and covered by regressions:

1. **BLOCKER — RECEIVED without a building candidate.** Stage 3 discard and
   Stage 5 normalized recovery can legitimately produce
   `ingestion_status=RECEIVED`, `building_revision_id=None`. The snapshot DTO
   now permits that state, reconciliation treats it as clean empty work (only
   PARSING without identity blocks), and the planner emits
   `BUILD/NEW_SOURCE`. Invalid snapshot projections are mapped to a classified
   `SyncPlanError` instead of leaking pydantic `ValidationError`.
2. **Full-build revision race.** `BuildRequest` now accepts an optional
   expected revision; the Stage 4 runner re-hashes after lease acquisition and
   raises `SourceMutationError` before candidate preparation if the revision
   changed or the source became unreadable. A scan-time revision is never
   silently replaced by a different published revision.
3. **Technical build failure cannot be SAFE.** `finalize_sync_result` now adds
   `TECHNICAL_BUILD_FAILURE` and forces `UNSAFE_TO_SERVE` whenever a technical
   build item exists, independent of the global projection result.
4. **Lease discipline.** Recovery lease acquisition now uses the Stage 4
   shield/settle pattern and releases a late-returned lease on cancellation;
   recovery phases receive lease checkpoints before chunk/index/verify/publish;
   deletion acquisition/release is cancellation-safe and probes the lease
   before Milvus and PostgreSQL work.
5. **Reconciliation root validation.** A non-directory/permission knowledge
   root returns BLOCKED before any candidate mutation; source state
   distinguishes disappearance from unreadable paths.
6. **Current repair.** Duplicate current rows are treated as an exact-set
   mismatch and repaired when reproducible, not automatically blocked; a
   current revision change between reads is blocked.
7. **Deletion revalidation.** A changed current revision or missing document
   yields `STATE_CHANGED` without deletion; successful deletion records both
   stores.
8. **Verification classification.** Global verification converts only
   `CanonicalDataError` to `CURRENT_CANONICAL_INVALID`; other programming
   `ValueError`s propagate rather than becoming an unsafe business verdict.
9. **Technical failure classification narrowed.** Deletion/execution catch only
   dependency/provider/resource failures, not invariant/configuration
   programming errors.
10. **Regressions added.** RECEIVED dead-end, invalid snapshot classification,
    scan-time revision drift, technical-failure safety, duplicate current
    repair, changed deletion state, lease/root behavior, and the full real
    interrupted-state/integration matrix.

## Git history and publication

```text
0e72598 feat(stage5): implement sync recovery and global safety
2c34d8d docs(stage5): add design plan and closeout evidence
```

The documentation-only commit containing this publication section supersedes
`2c34d8d` for the report itself and is verified through the pull-request head
rather than embedding its own commit hash.

Publication evidence:

- Verified implementation/docs closeout commit:
  `2c34d8de23ab98fc2d84d1ced3597e803fe6c123`
- Normal push to `origin/stage/05-part-i-sync-recovery-maintenance`: **PASS**
- Force push: **NOT USED; PROHIBITED**
- Remote CI `Verify`: **PASS** — push run
  [42851516783](https://github.com/Melon235/AtlasRAG/actions/runs/42851516783)
  (2m43s) and pull-request run
  [42851883916](https://github.com/Melon235/AtlasRAG/actions/runs/42851883916)
  (2m56s)
- Remote `Integration`: **PASS** — push run
  [42851516964](https://github.com/Melon235/AtlasRAG/actions/runs/42851516964)
  and pull-request run
  [42851883901](https://github.com/Melon235/AtlasRAG/actions/runs/42851883901)
  passed Stage 2 (8m41s/8m9s), Stage 3 (35s/35s), Stage 4 (17m4s/17m11s),
  and Stage 5 (12m42s/12m20s)
- Pull request number/status:
  [#6](https://github.com/Melon235/AtlasRAG/pull/6) — **OPEN, CLEAN,
  UNMERGED**
- Merge into `main`: **NOT PERFORMED; MUST REMAIN MANUAL**

## Reviews

- **Architecture review:** PASS after corrections. Scanner/planner purity,
  durable recovery, current-index repair ownership, Milvus-first deletion,
  global exact-set safety, and the six-node graph were independently audited.
  The audit's RECEIVED dead-end blocker and full-build revision race were
  fixed; no RuntimeGate/Stage 10 responsibility was pulled into Stage 5.
- **Security/release review:** PASS. No secrets, model weights, `.env`, logs,
  benchmark data, or migrations are tracked. Technical exceptions are not
  converted to business FAILED; deletion failures and technical build items
  cannot produce a SAFE verdict. No force push, automatic merge, or Stage 6
  work is included.

## Task ledger

| Task | Status |
| --- | --- |
| T0 base, scope, contracts, report skeleton | COMPLETE |
| T1 scanner and ScanSnapshot | COMPLETE |
| T2 database snapshot, SyncPlanner, SyncPlan | COMPLETE |
| T3 bounded build execution | COMPLETE |
| T4 reconciliation and normalization matrix | COMPLETE |
| T5 resume PARSED/CHUNKED and INDEXING reset | COMPLETE |
| T6 current index repair | COMPLETE |
| T7 missing-source deletion | COMPLETE |
| T8 global published-knowledge safety | COMPLETE |
| T9 six-node index_graph and injected application runner | COMPLETE |
| T10 real PostgreSQL/Milvus integration and fault injection | COMPLETE |
| T11 commands, CI, full closeout, report, push, PR | COMPLETE — 2c34d8d; PR #6 open and unmerged |

## Deferred work

No RuntimeGate/MaintenanceCoordinator, SERVING/DRAINING admission, health
registry, CLI/server, production scheduler, Redis invalidation execution,
directory resume scheduler, Part II retrieval/generation, web evidence,
benchmark, or Stage 6 work.

Stage 5 is not complete until the report, commits, normal push, final-HEAD CI
and Stage 2/3/4/5 Integration are green, and an unmerged PR exists.
