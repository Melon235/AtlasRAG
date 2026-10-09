# AtlasRAG Stage 2 — Infrastructure & Persistence

## Stage

Stage 2 — Infrastructure & Persistence.

## Status

**COMPLETE**

The Stage 2 implementation, offline gate, clean-state service-backed gate, and
local closeout audits are complete. The final implementation commit is
published on the Stage branch, and its remote CI and Integration workflows
both completed successfully. No force push or merge has been performed.

## Scope

Stage 2 implements only the technical infrastructure and persistence
capabilities required by later application services:

- pinned, localhost-only Docker Compose infrastructure for PostgreSQL, Redis,
  Milvus standalone with etcd/MinIO, and SearXNG;
- immutable PostgreSQL storage records, one append-only migration, an async
  pool, canonical repositories, an explicit Unit of Work, and per-document
  session advisory-lock leases;
- typed Parent Context and Session Query Redis cache primitives;
- immutable Milvus retrieval records, a frozen collection/index schema, and an
  async fail-closed provider around the synchronous PyMilvus client;
- real readiness, migration-head, service-topology, and integration checks;
- stronger production import/transaction boundaries; and
- separate offline and real-service GitHub Actions workflows.

This Stage stores state and exposes technical capabilities. It does not
implement ingestion transitions, publish coordination, retrieval policy, or
any business graph.

## Resolved dependencies and toolchain

The frozen lock resolved the following direct Stage 2 additions during final
verification:

| Dependency | Scope | Resolved version |
| --- | --- | --- |
| Alembic | production migration framework | 1.20.0 |
| Psycopg + binary extra | production PostgreSQL driver | 3.3.6 |
| psycopg-pool | production PostgreSQL pool | 3.3.3 |
| redis-py | production async Redis client | 8.1.0 |
| PyMilvus | production Milvus SDK | 2.6.17 |
| pytest-asyncio | development/integration tests | 1.4.0 |

Verification used Python 3.11.16. The local integration host used Docker
29.6.0 and Docker Compose v5.2.0; those host-tool versions are evidence, not
application runtime contracts.

## Container images and Compose topology

| Service | Frozen image | Host exposure | Persistence |
| --- | --- | --- | --- |
| PostgreSQL | `postgres:17.8-alpine` | `127.0.0.1:5432` by default | named volume |
| Redis | `redis:8.2.10-alpine` | `127.0.0.1:6379` by default | disposable; RDB/AOF disabled |
| etcd | `quay.io/coreos/etcd:v3.5.25` | none | named volume |
| MinIO | `minio/minio:RELEASE.2024-12-18T13-15-44Z` | none | named volume |
| Milvus | `milvusdb/milvus:v2.6.20` | `127.0.0.1:19530` and health `:9091` | named volume |
| SearXNG | `searxng/searxng:2026.9.25-12f8b6515` | `127.0.0.1:8080` | none |

Every service has a health check. Milvus waits for healthy etcd and MinIO.
PostgreSQL, Redis, Milvus, its health endpoint, and SearXNG bind through
`ATLASRAG_BIND_HOST`, whose tracked default is loopback. etcd and MinIO are
internal-only. The AtlasRAG Python/GPU application remains outside Compose.

The tracked environment file contains placeholders only; the local
`deploy/local/.env` is ignored. Ordinary `make infra-down` preserves named
volumes. The Integration workflow uses a unique Compose project and always
runs `down -v --remove-orphans`. Because the archived MinIO image is no longer
available from its original registry, CI builds the exact frozen release from
the official repository commit, verifies the embedded release and commit
identifiers, and assigns the required frozen tag. Its minimal runtime base is
also pinned by immutable content digest.

## PostgreSQL schema and migration

There is one linear append-only migration head:

```text
20260928_0001
```

Destructive downgrade is deliberately unsupported. The migration creates
three canonical domain tables plus one singleton correctness-marker table:

| Table | Purpose | Principal indexes/constraints |
| --- | --- | --- |
| `documents` | Stable source identity, current/building revision triples, ingestion status, failure stage, parse metadata | primary key; unique relative source path; current-revision and building-revision indexes; source/status/failure and all-or-none triple checks |
| `document_elements` | Revision-scoped normalized parser facts | primary key; document foreign key; `(document_id, revision_id)` index; order and JSON-shape checks |
| `chunks` | Canonical Parent/Child/Table content and lineage | primary key; unique `(chunk_id, document_id, revision_id)`; document/revision, parent, and chunk-type indexes; deferrable same-revision parent FK |
| `runtime_metadata` | Singleton persisted query-cache invalidation marker | singleton primary key/check; seeded `runtime` row |

A deferred constraint trigger verifies that every stored `TEXT_CHILD` resolves
to a `TEXT_PARENT` in the same document and revision. Cross-document,
cross-revision, missing, and wrong-type relationships fail at transaction
commit; a check constraint rejects self-parent rows. PostgreSQL remains
canonical; Milvus is derived.

The immutable storage layer defines `StoredDocument`, `StoredElement`,
`StoredChunk`, and `RuntimeMetadata`, plus persistence-only status/failure/chunk
enums. Records reject unknown fields, invalid canonical identifiers,
non-finite/non-JSON metadata, invalid paths, partial revision triples, and
inconsistent failure or parent state.

## Repository APIs and Unit of Work

- `DocumentRepository`: parameterized `put`, identity/path lookup, and
  deterministic list.
- `ElementRepository` and `ChunkRepository`: ordered revision reads,
  count/identity queries, revision delete, and delete-then-insert exact-set
  replacement on the caller-owned connection. Chunk lookup also supports
  canonical parent/table resolution.
- `RuntimeMetadataRepository`: read and update the singleton query-cache
  invalidation marker.
- `PostgresPool`: explicit `open(wait=True)`, close, checkout/return, and a real
  `SELECT 1` health probe.

`PostgresUnitOfWork` checks out exactly one connection and binds all four
repositories to that transaction. Repositories never commit. Only explicit
`await uow.commit()` can commit, and it is single-use. Normal uncommitted exit,
repository failure, or exceptional exit rolls back; cleanup always attempts to
return or safely close the connection. Operations after finish fail as local
invariant violations. Exact-set retry cannot leave stale rows when the caller
does not commit or an injected write fails.

The AST architecture gate enforces that repository modules contain no
`.commit()` call outside `uow.py`; repositories cannot import providers,
graphs, or benchmarks; providers cannot import repositories, graphs, or
benchmarks; and no production layer may import tests or benchmarks.

## Advisory-lock semantics

`PostgresAdvisoryLockManager` hashes the canonical `document_id` with SHA-256
and maps the first eight bytes into a deterministic signed PostgreSQL int64.
It uses `pg_try_advisory_lock` on a dedicated pooled session, outside the Unit
of Work transaction. Contention is the only path that returns `None`.

An `AdvisoryLockLease` owns that exact session until idempotent explicit
release. Release verifies `pg_advisory_unlock`, rolls back incidental
transaction state, and returns only a known-unlocked connection. Error or
cancellation closes a possibly lock-bearing session before pool
reconciliation, so a leaked session lock cannot be handed to another caller.
Stage 2 does not choose document-build concurrency policy.

## Redis key and value contracts

Exact key shapes are:

```text
atlasrag:parent:{revision_id}:{parent_id}
atlasrag:query:{session_id}:{runtime_fingerprint}:{request_hash}
```

Key components must be nonblank canonical Unicode scalar text without colons
or control characters. The Query Cache key intentionally has no knowledge
revision slot.

`CachedParentContext` stores the complete canonical context:
`evidence_type`, `context_id`, `document_id`, `revision_id`, full `content`, and
`LocalProvenance`. `CachedQueryResult` stores literal `schema_version=1`, an
aware `created_at`, and a complete validated `FinalResponse`.

Both caches expose async typed get/put/delete primitives with positive TTLs.
A missing key alone returns `None`; malformed data raises
`ProviderResponseError`, and dependency failures remain typed failures. Query
invalidation uses bounded `SCAN atlasrag:query:*` pages and `UNLINK` batches.
It never uses `KEYS`/`FLUSHDB`/`FLUSHALL` and preserves Parent Cache and
unrelated keys. Stage 2 does not decide when caches are read, bypassed, or
invalidated.

## Milvus collection, records, and indexes

The provider owns the single canonical collection name `atlas_chunks`.
`MilvusChunkRecord` admits only retrievable `TEXT_CHILD` and `TABLE` records;
`TEXT_PARENT` remains PostgreSQL-only. It validates canonical identities,
TEXT_CHILD parent presence, source type, finite vectors, exact configured
dimension, UTF-8 limits, and bounded immutable JSON metadata.

The frozen fields are:

```text
chunk_id (primary), document_id, revision_id, file_name, parent_id?,
chunk_type, source_type, content, sparse_text, dense_vector,
sparse_vector (generated), structured_metadata
```

`sparse_text` uses the `standard` analyzer. Function `atlas_bm25` generates
`sparse_vector` from `sparse_text`. Indexes are:

| Index | Field | Type/metric | Frozen parameters |
| --- | --- | --- | --- |
| `dense_hnsw` | `dense_vector` | HNSW / COSINE | `M=32`, `efConstruction=200` |
| `sparse_bm25` | `sparse_vector` | SPARSE_INVERTED_INDEX / BM25 | `DAAT_MAXSCORE`, `bm25_k1=1.2`, `bm25_b=0.75` |

Initialization creates schema/functions/indexes only when absent. Existing
descriptions are normalized and compared field by field; incompatibility
raises `ConfigurationError` without drop/recreate. Exact-set replacement is
validate-first, delete, flush/visibility, insert the complete unique set, then
flush/visibility. Count, paged identity enumeration, revision/document delete,
health, and close primitives are included. Every blocking SDK call crosses an
async thread boundary and cancellation waits for the owned call to settle.

## Failure translation

| Boundary | Translation |
| --- | --- |
| PostgreSQL pool/driver timeout classes | `DependencyTimeoutError` |
| PostgreSQL connection/driver/OS availability failures | `DependencyUnavailableError` |
| PostgreSQL integrity/data violations | `InvariantViolationError` |
| PostgreSQL programming/unsupported operations | `ConfigurationError` |
| Invalid canonical database rows | `CanonicalDataError` |
| Redis timeout | `DependencyTimeoutError` |
| Redis transport/client availability failure | `DependencyUnavailableError` |
| Redis invalid local command data | `InvariantViolationError` |
| Redis malformed/unexpected provider payload | `ProviderResponseError` |
| Milvus timeout/deadline | `DependencyTimeoutError` |
| Other PyMilvus/gRPC/socket SDK failures | `DependencyUnavailableError` |
| Milvus schema/index incompatibility | `ConfigurationError` |
| Invalid local Milvus input | `InvariantViolationError` |
| Malformed Milvus response | `ProviderResponseError` |

No PostgreSQL, Redis, or Milvus technical failure is converted into `None`, a
cache miss, zero records, or an empty retrieval result. Business fallback and
semantic retry remain deferred above these adapters.

## Integration topology and isolation

Integration tests are marked `integration`, excluded from ordinary pytest,
and fail unless `ATLASRAG_RUN_INTEGRATION=1` is explicitly set. They use only
the local Compose services and deterministic fixed records; SearXNG is probed
at its root without issuing a search.

Before any destructive setup, PostgreSQL, Redis, and Milvus fixture endpoints
must parse to loopback hosts. A remote hostname or non-loopback IP fails closed
before a client is created, a database/collection is created, or Redis cleanup
can begin.

- Every PostgreSQL test gets a UUID-named database, migrates it to head, and
  force-drops only that validated test database during cleanup.
- Redis uses dedicated database 15, guarded by an exclusive token lease; it
  refuses cleanup unless the database was empty at acquisition and deletes
  only keys protected by the lease.
- Every Milvus test registers cleanup before creating a unique test
  collection. An intentionally incompatible collection is verified to remain
  present after fail-closed initialization, then test cleanup removes only
  that isolated collection.
- Compose tests verify six healthy services, loopback-only published ports,
  private etcd/MinIO, and local SearXNG reachability.

The real suite covers fresh migrations and exact schema, relational
constraints, rollback and exact-set semantics, marker durability, advisory
lock contention/release, typed unavailable/timeout failures, Redis TTL and
namespace isolation, Milvus compatible reopen/incompatible preservation,
dense HNSW and built-in BM25 smoke searches, write/delete visibility, and
service topology.

## CI separation

The regular `CI` workflow remains secret-free, CPU-only, and service-free. It
performs frozen dependency sync followed by `make verify`.

The separate `Integration` workflow creates non-secret ephemeral local
credentials, chooses a run-unique Compose project, syncs the frozen lock, runs
`make stage2-verify`, captures Compose logs before failure cleanup, and always
destroys containers, networks, volumes, and orphans. It builds MinIO
`RELEASE.2024-12-18T13-15-44Z` from official source commit
`16f8cf1c52f0a77eeb8f7565aaf7f7df12454583` with Go 1.23.4 and rejects any
binary that does not report that exact release, commit, and runtime. Neither
workflow reads GitHub secrets.

The first published closeout run failed before Stage verification because the
GitHub runner denied access to a third-party registry mirror used to recover
the archived MinIO image. The final implementation commit removed that mirror,
built the same frozen release from its official fixed source commit, and
passed both remote workflows.

## Verification

Fresh final local verification ran on 2026-10-02. The offline path ran with
PostgreSQL, Redis, Milvus, etcd, MinIO, and SearXNG stopped.

| Command/gate | Result |
| --- | --- |
| `uv sync --offline --frozen` | PASS — checked 42 packages without network resolution |
| `make format` | PASS — 100 files left unchanged in the final rerun |
| `make format-check` | PASS — 100 files already formatted |
| `make lint` | PASS — all Ruff checks passed |
| `make typecheck` | PASS — no issues in 88 source files |
| `make architecture-check` | PASS — layer/import/commit and tracked-artifact checks emitted no violations |
| `make test` | PASS — 1,728 passed, 28 integration tests deselected |
| `make verify` | PASS — all offline gates; 1,728 passed, 28 deselected |
| clean-state `make stage2-verify` offline segment | PASS — 1,728 passed, 28 deselected |
| infrastructure readiness/status | PASS — all six services healthy; published ports loopback-only |
| `make migration-check` | PASS — database revision equals single head `20260928_0001` |
| `make test-integration` | PASS — 28 passed, 1,728 deselected in 151.54s |
| automatic local pipeline shutdown | PASS — no project containers or network remained |
| explicit post-run volume cleanup | PASS — all four project volumes removed |
| Compose config validation | PASS — frozen configuration resolves with the ignored local env |
| whitespace/artifact/secret scans | PASS — no errors, forbidden tracked files, tracked local env, or credential-pattern matches |

The final collected suite contains 1,756 tests: 1,728 unit/contract/static tests
in the service-free gate and 28 explicitly selected integration tests.

## Architecture deviations

NONE

The implementation was checked against the authoritative Architecture Manual
canonical-store, Parent/Child, Milvus baseline, local deployment, persistence
ownership, and failure-policy sections; the Stage 2 instruction; the committed
implementation plan; and the complete `origin/main` diff. No frozen Stage 1
public contract or graph State was changed. PostgreSQL remains canonical,
Redis remains disposable, Milvus remains derived, and dependencies are not
stored in graph State.

## Known limitations and deferred work

The following are intentionally absent and are not Stage 2 defects:

- no parser (including MinerU or Docling integration);
- no chunk builder or representation builder;
- no embedding model/provider;
- no Part I build graph or publish coordinator;
- no retrieval graph, RRF, reranker, or answer execution;
- no real SearXNG provider/search behavior;
- no Query Cache read/bypass/invalidation policy;
- no RuntimeGate, MaintenanceCoordinator, startup reconciliation, or runtime
  composition root;
- no benchmark harness or benchmark-aware production fields; and
- no Stage 3 business logic.

Exact-set primitives are available, but later services own orchestration,
state transitions, semantic retry, publish/rollback policy, cache policy, and
runtime safety gates.

## Git branch, commit, push, and CI

- Base: `origin/main` at `0db10fa2aa27f2cac06fbde7a8734862a52834ca`
- Branch: `stage/02-infrastructure-persistence`
- Final implementation commit:
  `92d656b6058a2741141b5b7373b4075ca5ae67af`
- GitHub push: PASS — published normally to
  `origin/stage/02-infrastructure-persistence`
- Remote CI: PASS —
  [run 37006636651](https://github.com/Melon235/AtlasRAG/actions/runs/37006636651)
- Remote Integration CI: PASS —
  [run 37006636633](https://github.com/Melon235/AtlasRAG/actions/runs/37006636633)
- Publication-evidence revision: this documentation-only successor; its final
  commit and workflow reruns are reported in the delivery handoff to avoid a
  self-referential documentation commit cycle
- Force push: not used and prohibited
- Integration into `main`: not performed

## Next

Stage 3 — Part I: Parse & Canonical Build: **NOT STARTED**.

Stage 3 has NOT been started. Do not begin MinerU, Docling, chunking, embedding,
or any Stage 3 implementation as part of Stage 2 publication.
