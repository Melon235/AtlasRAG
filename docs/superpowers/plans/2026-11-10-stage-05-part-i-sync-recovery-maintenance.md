# AtlasRAG Stage 5 — Part I: Sync / Recovery / Maintenance

## 1. Status and authority

- Stage: **Stage 5 — Part I: Sync / Recovery / Maintenance**
- Base: `origin/main` at
  `23def47e4ceaa4b8332b6db575d9534d6177fc2b` (Stage 4 merge, PR #5)
- Branch: `stage/05-part-i-sync-recovery-maintenance`
- Authority: tracked
  `docs/architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md`,
  frozen Stage 1–4 contracts, and the approved Stage 4 amendments.
- Database migration: **NONE planned**.
- Implementation status: **COMPLETE** — see
  [the Stage 5 report](../../stages/stage-05-part-i-sync-recovery-maintenance.md).
- Frozen Stage 3 canonical IDs/chunk semantics and Stage 4 publish/verification
  contracts are reused unchanged.

### Approved ownership amendment

Architecture Manual §24A.1 sketches `enter_maintenance` and `resume_serving`
inside `index_graph`. The final RuntimeGate / MaintenanceCoordinator ownership
requires those lifecycle transitions to live in the Part IV application
coordinator (Stage 10). Stage 5 therefore implements only the knowledge
maintenance business workflow:

```text
reconcile_runtime
      ↓
scan_directory
      ↓
build_sync_plan
      ↓
execute_builds
      ↓
execute_deletions
      ↓
final_verify
```

Stage 5 returns a `ServingSafety` verdict. It does **not** enter/leave
SERVING, does not implement RuntimeGate, and does not create a second runtime
composition root.

## 2. Scope

Implemented:

- complete Knowledge Directory scanner with immutable `ScanSnapshot`;
- canonical PostgreSQL `CanonicalDatabaseSnapshot`;
- pure deterministic `SyncPlanner` and immutable `SyncPlan`;
- `BUILD` / `DELETE` / `UNCHANGED` with `NEW_SOURCE`, `CONTENT_CHANGED`,
  `PIPELINE_CHANGED`, `RETRY_FAILED_BUILD`;
- bounded per-document build execution reusing
  `DocumentBuildRunner` / `document_build_subgraph`;
- resume from durable `PARSED` and `CHUNKED` boundaries;
- `StartupReconciliationService` with the full persisted-state matrix;
- candidate residue cleanup and exact-set current index repair;
- missing-source deletion with Milvus-first ordering and PostgreSQL
  deletion + freshness marker in one transaction;
- `GlobalKnowledgeVerificationService` over all published revisions and the
  complete Milvus collection;
- separated `SyncOutcome` and `ServingSafety`;
- top-level `index_graph`;
- real PostgreSQL/Milvus integration tests and Stage 5 CI/Make gates.

Explicitly not implemented:

- RuntimeGate / MaintenanceCoordinator lifecycle;
- SERVING/DRAINING admission control and health registry;
- CLI/HTTP server, production scheduler, or second composition root;
- Redis SCAN/UNLINK or freshness-marker clearing;
- Part II retrieval/generation, web evidence, RRF/reranking, benchmarks;
- Stage 6 work.

## 3. Layer boundaries

```text
Graph nodes
    ↓
Stage 5 business services
    ↓
Stage 4 build/index/publish services + Stage 3 persistence services
    ↓
Repository / UnitOfWork + provider protocols
    ↓
PostgreSQL / Milvus
```

- Scanner performs filesystem observation only; it never plans or deletes.
- `SyncPlanner` is pure: no filesystem, database, Milvus, parser, embedding, or
  mutation.
- Graph nodes contain no SQL, Redis, Milvus SDK, filesystem scanning, parser or
  model plumbing.
- Graph State contains only immutable request/plan/result data; never providers,
  connections, leases, vectors, raw bytes, receipts, exceptions, or config.

### Module layout

```text
domain/sync/__init__.py
domain/sync/models.py          typed immutable Stage 5 DTOs
domain/sync/failures.py        internal classified failures

services/sync/__init__.py
services/sync/ports.py         narrow UoW/index/lease/executor protocols
services/sync/scanner.py       KnowledgeDirectoryScanner
services/sync/snapshot.py      CanonicalDatabaseSnapshotService
services/sync/planner.py       pure SyncPlanner
services/sync/normalization.py durable boundary normalization helpers
services/sync/reconciliation.py StartupReconciliationService
services/sync/recovery.py      resume PARSED/CHUNKED + failed-residue cleanup
services/sync/current_repair.py current derived-index repair
services/sync/execution.py     bounded build/recovery execution
services/sync/deletion.py      missing-source deletion
services/sync/verification.py  global published-knowledge verification

graphs/indexing/sync_state.py  minimal IndexGraphState
graphs/indexing/sync_contracts.py SyncRequest / graph output
graphs/indexing/index_graph.py six-node top-level graph

application/index_sync.py       injected Stage 5 application runner/factory
```

## 4. ScanSnapshot contract

`ScanEntry` contains the revalidated Stage 3 `SourceFile` plus its streamed
SHA-256 `content_hash`. `ScanSnapshot` contains entries sorted by
`relative_source_path`; paths and document identities are unique.

The scanner:

1. resolves the configured Knowledge Directory as an existing directory;
2. recursively enumerates entries without following symlinks;
3. rejects symlinked files/directories and non-regular special files;
4. ignores regular files with unsupported suffixes deterministically;
5. constructs `SourceFile.from_knowledge_directory` for every supported path;
6. hashes each source with the Stage 3 streaming hasher;
7. checks size/mtime stability around hashing and fails on disappearance;
8. returns only a complete snapshot.

Any directory enumeration, permission, symlink, disappearance, duplicate,
unreadable source, or identity failure raises classified `ScanError`. A partial
snapshot is never returned and no deletion can be planned from a failed scan.

Unsupported regular files are not knowledge sources and cannot cause deletion
of supported sources. The scanner never treats a failed walk as an empty
directory.

## 5. CanonicalDatabaseSnapshot and SyncPlanner

`DatabaseDocumentSnapshot` is a dependency-free projection of durable
`documents` fields:

```text
document_id, relative_source_path, file_name, source_type,
current_revision_id/content_hash/pipeline_fingerprint,
building_revision_id/content_hash/pipeline_fingerprint,
ingestion_status, failed_stage
```

`CanonicalDatabaseSnapshot` requires unique document/path identities and
validates revision triples, `current != building`, READY ownership, and
failure/status consistency. Invalid canonical ownership is a reconciliation
blocker, not a planner repair.

`SyncPlanner` inputs are exactly `ScanSnapshot`,
`CanonicalDatabaseSnapshot`, and the target `pipeline_fingerprint`. It returns
an immutable `SyncPlan` with fixed action order: BUILD (sorted path), DELETE
(sorted path), UNCHANGED (sorted path).

Rules:

| Condition | Action / reason |
| --- | --- |
| present source has no published current revision | BUILD / NEW_SOURCE |
| present source current content differs | BUILD / CONTENT_CHANGED |
| present source current pipeline differs | BUILD / PIPELINE_CHANGED |
| present source, no current, previous classified FAILED | BUILD / RETRY_FAILED_BUILD |
| present source, current content and pipeline match | UNCHANGED |
| published/known document path absent after complete scan | DELETE |

`NEW_SOURCE` means no published current revision exists for a present source,
including an interrupted first build. A failed R2 with a healthy current R1 is
UNCHANGED; it is not automatically retried. `RETRY_FAILED_BUILD` applies only
when there is no current revision to preserve. Rename/move is DELETE old +
BUILD new; no inode/mtime/similarity inference.

## 6. Reconciliation contract and recovery matrix

`StartupReconciliationService` reads durable PostgreSQL state, validates
envelope ownership, cleans derived residue, repairs reproducible current
indexes, and returns:

```text
outcome: CLEAN | REPAIRED | BLOCKED
recovery_targets: PARSED/CHUNKED resume targets
repaired_document_ids
blocked_reasons (safe codes only)
```

It does not perform a directory sync and does not auto-resume FAILED builds.

| Persisted state | Reconciliation action |
| --- | --- |
| RECEIVED / PARSING | delete incomplete candidate rows/residue; restore READY if current exists else RECEIVED |
| PARSED | validate element exact-set/envelope; keep as resume target when source and pipeline still match |
| CHUNKING | delete partial chunks; restore logical PARSED; resume target |
| CHUNKED | validate chunk exact-set/lineage; keep as resume target |
| INDEXING | delete candidate Milvus revision; prove absence; restore logical CHUNKED; resume target |
| FAILED | preserve failed-stage facts; remove candidate Milvus residue; never auto-resume |
| READY | validate current revision/slot/no building; verify current Milvus exact-set; repair if exactly reproducible |

Source/pipeline mutation for a non-FAILED candidate discards that candidate
while preserving current. FAILED candidates retain failure facts; a later
planner-requested retry may reset the stale candidate before a fresh build.

Impossible states are BLOCKED without deleting last-good data:

```text
current_revision_id == building_revision_id
current/building triple or metadata-slot mismatch
READY without valid current / with building ownership
invalid canonical parent lineage or missing parent
unknown envelope version
current Milvus mismatch that cannot be reproduced exactly
unresolvable canonical document identity
```

## 7. Resume services and current repair

Recovery reuses Stage 3/4 functions:

- `resume_from_parsed`: re-read persisted elements and current source hash,
  reconstruct `DocumentContainer` from PostgreSQL, mark CHUNKING, chunk,
  persist CHUNKED, then run Stage 4 index/verify/publish. No parser rerun.
- `resume_from_chunked`: re-read persisted chunks, validate canonical lineage,
  then run Stage 4 index/verify/publish. No parse/chunk rerun.
- `INDEXING` is first normalized to CHUNKED and its Milvus candidate set is
  deleted; resume never appends to a partial set.
- A source hash or pipeline mismatch discards the old candidate and returns
  SOURCE_MUTATED; it never forces current to change.

Current index repair is **not** a candidate build and never sets
`building_revision_id`. It:

1. re-reads current revision, elements, chunks and lineage from PostgreSQL;
2. requires the stored current pipeline fingerprint and persisted chunk
   strategy/profile to match the configured representation/embedding/index
   pipeline exactly;
3. rebuilds only the Milvus derived current revision in exact-set order;
4. verifies the complete Strong projection;
5. leaves PostgreSQL current/status/marker unchanged.

If exact reproducibility cannot be proven, reconciliation returns BLOCKED.

## 8. Missing-source deletion

Deletion is planned only after a complete successful scan. For each DELETE:

```text
acquire same per-document PostgreSQL advisory lease
    ↓
revalidate source still absent and persisted state unchanged
    ↓
Milvus scoped delete of all document revision records
    ↓
visibility barrier + exact absence scan
    ↓
one PostgreSQL transaction: delete chunks/elements/document + marker=true
    ↓
commit
```

Milvus is deleted first; PostgreSQL is deleted second, so a crash cannot leave
a canonical row whose evidence is already gone while searchable orphan evidence
appears. If Milvus deletion succeeds but the PostgreSQL transaction fails, the
result is FAILED, not deleted, and global safety is UNSAFE. A retry is exact
and idempotent. The freshness marker is written in the same PostgreSQL
transaction as the document deletion and rolls back together.

## 9. Global verification and verdicts

`GlobalKnowledgeVerificationService` uses PostgreSQL as expected authority and
Milvus as actual derived state. It enumerates the complete collection and
per-published-revision projections without relying on counts or insertion
acknowledgements:

```text
all current published revisions and valid canonical lineage
exact expected IDs / count / scalar + typed metadata projection
TEXT_PARENT absent from Milvus
no duplicate/orphan/non-current/half-published rows
no unresolved required deletion
no unrepaired current/PG/Milvus mismatch
```

`SyncOutcome` and `ServingSafety` are independent:

- SUCCESS / PARTIAL_SUCCESS / FAILED for run completion;
- SAFE_TO_SERVE / UNSAFE_TO_SERVE for the published knowledge view.

A failed R2 with healthy, exact R1 and no required deletion is
PARTIAL_SUCCESS + SAFE. A failed required deletion or unresolved current
mismatch is UNSAFE even if current rows agree with each other. Global
verification technical failures propagate; they never yield SAFE.

## 10. index_graph

State (closed, no dependencies):

```text
IndexGraphInput  = request
IndexGraphState  = request, reconciliation_result?, scan_snapshot?,
                   sync_plan?, build_summary?, deletion_summary?,
                   final_result?, blocked_code?
IndexGraphOutput = final_result
```

Topology:

```text
START → reconcile_runtime → scan_directory → build_sync_plan
      → execute_builds → execute_deletions → final_verify → END
```

Classified reconciliation/scan/plan blocking routes directly to END with a
FAILED/UNSAFE `SyncResult` and no build/deletion side effects. Unclassified
exceptions propagate. `execute_builds` consumes plan BUILD actions plus
reconciliation recovery targets, deduplicated by document identity. It uses
the Stage 4 full build runner for NEW/CONTENT/PIPELINE/RETRY actions and the
resume service when a matching durable target exists.

## 11. Concurrency, locks, cancellation

- Different documents build concurrently under an explicit bounded semaphore.
- The same document is serialized by the Stage 2/4 PostgreSQL advisory lease.
- Full builds and resume operations acquire the lease before source hashing and
  hold it through publish.
- Deletions use the same per-document lease.
- Qwen/index providers remain process-shared; GPU concurrency is bounded inside
  the existing provider. No per-document model load.
- One document's classified failure does not cancel independent documents.
- Cancellation settles issued work before releasing a lease; it is never
  persisted as business FAILED.

## 12. Failure classification

| Class | Handling |
| --- | --- |
| business build failure | Stage 4 `BuildResult` FAILED with safe stage/code; no exception conversion |
| partial run | build/deletion summary; SyncOutcome + ServingSafety |
| classified scan/plan/reconciliation block | FAILED/UNSAFE result, no destructive action |
| technical dependency/timeout/internal error | propagate; never SAFE and never business FAILED |
| cancellation | propagate after settlement; durable state remains for recovery |

Candidate Milvus residue is cleaned after a controlled failed build. Cleanup
failure is recorded as a technical residue failure and makes global safety
UNSAFE until a later reconciliation removes it.

## 13. Test matrix

Unit/offline:

- scanner: empty, deterministic order, unsupported files, symlink, permission,
  disappearance, duplicate identity, partial-walk failure, no plan/deletion;
- planner: all four BUILD reasons, DELETE/UNCHANGED, rename semantics,
  deterministic ordering, no I/O, invalid snapshot rejection;
- DTO/state: closure, immutability, no providers/vectors/connections;
- graph: six nodes, empty KB, blocked routing, no deletion after scan failure,
  build isolation, missing deletion -> UNSAFE, R2 failed + R1 healthy -> SAFE;
- reconciliation: every lifecycle state, impossible states, source mutation,
  FAILED no-resume, exact-set INDEXING reset;
- resume: PARSED/CHUNKED no reparse, R1 preservation, source mutation;
- current repair: reproducible repair, non-reproducible BLOCKED, extra/missing
  rows, no building impersonation;
- deletion: order, revalidation, marker rollback, visibility/exact absence;
- global verify: exact all-collection set, duplicates, orphans, non-current,
  TEXT_PARENT, required deletion failures.

Real integration (PostgreSQL + Milvus + deterministic fake embedding):

- full sync empty/populated/temp knowledge directory;
- content change, pipeline change, rename, delete;
- crash states for PARSED/CHUNKING/CHUNKED/INDEXING/FAILED/READY;
- current index repair and unreproducible block;
- candidate residue cleanup and partial index retry;
- failed deletion + rollback + UNSAFE verdict;
- same-document serialization, different-document bounded concurrency;
- cancellation.

## 14. Commands and CI

New targets:

```text
make test-stage5
make test-stage5-integration
make stage5-verify
```

`make verify` remains offline, CPU-safe, model-free, Docker-free, and
network-free. Stage 5 integration uses real PostgreSQL/Milvus and deterministic
fake embedding. Stage 2/3/4 integration jobs remain. All actions stay pinned to
immutable SHAs.

## 15. Definition of Done

- All local gates: verify, Stage 2, Stage 3, real MinerU, Stage 4, Stage 4 Qwen,
  Stage 5, architecture, diff check.
- Architecture/security review confirms the approved ownership and fail-closed
  boundaries.
- Report records base, decisions, matrix, tests, fault evidence, and deferred
  work.
- Normal commit/push; final-HEAD CI and Stage 2/3/4/5 Integration green.
- Stage 5 PR opened against `main` and left unmerged. Stage 6 not started.

## 16. Task breakdown

| Task | Scope |
| --- | --- |
| T0 | base/contracts/scope, report skeleton, baseline |
| T1 | scanner and ScanSnapshot |
| T2 | DB snapshot, SyncPlanner, SyncPlan |
| T3 | bounded build execution with Stage 4 runner |
| T4 | reconciliation/normalization matrix |
| T5 | resume PARSED/CHUNKED services |
| T6 | current index repair |
| T7 | missing-source deletion |
| T8 | global safety verification |
| T9 | index_graph and application runner |
| T10 | real PostgreSQL/Milvus fault injection |
| T11 | commands/CI/full closeout/report/PR |
