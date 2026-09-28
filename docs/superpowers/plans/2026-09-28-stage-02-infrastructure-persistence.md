# Stage 2 Infrastructure & Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build AtlasRAG's local, async infrastructure foundation: canonical PostgreSQL persistence, disposable Redis caches, a rebuildable Milvus index, pinned Docker Compose services, health probes, migrations, and deterministic integration verification.

**Architecture:** PostgreSQL is the sole canonical store and is accessed through explicit async repositories bound to one caller-controlled Unit of Work; session advisory locks use a dedicated pooled connection. Redis and Milvus live under provider boundaries as disposable derived infrastructure, validate typed payloads at every boundary, and translate dependency failures into the frozen Stage 1 error vocabulary. Alembic owns append-only SQL migrations without ORM domain models, while the ordinary CI path remains service-free and a separate integration path owns Compose lifecycle and cleanup.

**Tech Stack:** Python 3.11, Pydantic v2, Psycopg 3 async + `psycopg_pool`, Alembic, redis-py asyncio, PyMilvus 2.6.x, PostgreSQL 17, Redis 8.2, Milvus 2.6.20 + etcd + MinIO, SearXNG, Docker Compose, pytest/pytest-asyncio, mypy strict, Ruff, uv.

---

## Frozen decisions and file map

The Stage 2 instruction attachment and the Architecture Manual are the approved design. This plan does not reopen product architecture. It applies these implementation decisions:

- Keep every Stage 1 DTO, enum, graph-state schema, fingerprint, request hash, and technical error class unchanged.
- Evolve only the temporary Stage 1 "no future infrastructure exists" scope allowlist. Preserve its adversarial import/dependency checks and replace the obsolete whole-tree freeze with a Stage 2 exact allowlist and layer-specific imports.
- Use Alembic because the repository has no migration framework. Migration code uses explicit SQL/DDL and no ORM models.
- Use PostgreSQL composite foreign keys plus a deferrable constraint trigger to enforce that every `TEXT_CHILD` parent exists in the same document/revision and is a `TEXT_PARENT`.
- Acquire UoW connections with `AsyncConnectionPool.getconn()`/`putconn()` instead of `pool.connection()`, because Psycopg's pool context commits on normal exit. Stage 2 requires rollback unless `commit()` was called explicitly.
- Wrap the synchronous Milvus administration/data API behind `asyncio.to_thread`; callers see an async provider boundary.
- Pin Milvus server `v2.6.20` with PyMilvus `>=2.6.16,<2.7.0`, matching Milvus's official compatibility table. Use the official companion etcd `v3.5.25` and MinIO `RELEASE.2024-12-18T13-15-44Z` versions.
- Pin SearXNG to `2026.9.25-12f8b6515`; do not use `latest`. Do not implement a search provider or execute external searches.
- Exclude `integration` tests from ordinary pytest through the configured marker expression; `make test-integration` explicitly overrides that expression and fails when services are unavailable.

### Production files

- `src/atlasrag/repositories/postgres/records.py`: immutable PostgreSQL persistence records and storage-only enums.
- `src/atlasrag/repositories/postgres/errors.py`: Psycopg/pool exception classification and frozen-error translation.
- `src/atlasrag/repositories/postgres/pool.py`: explicit async pool lifecycle and PostgreSQL health probe.
- `src/atlasrag/repositories/postgres/documents.py`: typed document reads/writes only.
- `src/atlasrag/repositories/postgres/elements.py`: revision-scoped element exact-set/read primitives.
- `src/atlasrag/repositories/postgres/chunks.py`: revision-scoped chunk exact-set/canonical-context primitives.
- `src/atlasrag/repositories/postgres/runtime_metadata.py`: singleton cache-invalidation correctness marker.
- `src/atlasrag/repositories/postgres/uow.py`: explicit-commit Unit of Work binding all repositories to one connection.
- `src/atlasrag/repositories/postgres/locks.py`: deterministic signed-int64 key and dedicated session advisory-lock lease.
- `src/atlasrag/providers/cache/models.py`: typed Redis value contracts.
- `src/atlasrag/providers/cache/keys.py`: exact frozen key builders.
- `src/atlasrag/providers/cache/redis.py`: async parent/query cache capabilities, bounded invalidation, health, and error translation.
- `src/atlasrag/providers/index/models.py`: validated Milvus infrastructure records.
- `src/atlasrag/providers/index/schema.py`: expected collection/schema/function/index metadata and fail-closed comparison.
- `src/atlasrag/providers/index/milvus.py`: async Milvus administration and exact-set provider.

### Migration and local infrastructure files

- `alembic.ini`: migration entry point with URL supplied only from environment.
- `migrations/env.py`: explicit SQL migration environment; no application imports or ORM metadata.
- `migrations/script.py.mako`: deterministic revision template.
- `migrations/versions/20260928_0001_stage2_persistence.py`: initial append-only schema.
- `deploy/local/compose.yaml`: pinned localhost-only services and named canonical volumes.
- `deploy/local/.env.example`: safe local placeholders and port defaults.
- `deploy/local/searxng/settings.yml`: deterministic SearXNG settings with no provider credentials.
- `scripts/wait_for_infrastructure.py`: condition-based PostgreSQL/Redis/Milvus/SearXNG readiness checks.
- `scripts/check_migration.py`: compare database revision with Alembic head.
- `scripts/stage2_verify.sh`: trap-based Compose lifecycle and cleanup.

### Test and governance files

- `tests/persistence/test_records.py`: storage record invariants.
- `tests/persistence/test_postgres_contracts.py`: SQL/UoW/error/lock unit contracts with fakes.
- `tests/providers/test_redis_cache.py`: cache keys, typed JSON, invalidation, and error unit tests.
- `tests/providers/test_milvus_models.py`: record/vector validation.
- `tests/providers/test_milvus_schema.py`: canonical schema/index comparison.
- `tests/providers/test_milvus_provider.py`: sync-client adapter/error/exact-set unit tests.
- `tests/integration/conftest.py`: opt-in environment and isolated service fixtures.
- `tests/integration/test_postgres.py`: migrations, constraints, repositories, transactions, marker, locks, and failure paths.
- `tests/integration/test_redis.py`: exact keys, TTL, corruption, namespace isolation, and unavailable service.
- `tests/integration/test_milvus.py`: schema/index, exact-set visibility, dense/BM25 smoke, and failure paths.
- `tests/integration/test_compose.py`: SearXNG reachability and host-bind assertions.
- `tests/contract/test_stage1_scope.py`: preserve Stage 1 invariants while replacing its intentionally temporary Stage 1-only manifest/dependency freeze.
- `tests/contract/test_stage2_scope.py`: Stage 2 architecture/layer/static-commit/image/secret checks.
- `.github/workflows/integration.yml`: secret-free CPU-only Compose integration workflow.
- `docs/stages/stage-02-infrastructure-persistence.md`: evidence-bearing Stage report.

## Task 1: Dependency boundary and immutable persistence records

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/contract/test_stage1_scope.py`
- Create: `tests/contract/test_stage2_scope.py`
- Create: `src/atlasrag/repositories/postgres/__init__.py`
- Create: `src/atlasrag/repositories/postgres/records.py`
- Create: `tests/persistence/test_records.py`

- [ ] Write failing scope tests that allow `psycopg`, `redis`, and `pymilvus` only in their Stage 2 layers; continue rejecting graph imports from repositories/providers, benchmark/test imports from production, business-policy vocabulary, and unexpected packages/files.
- [ ] Run `uv run --frozen pytest tests/contract/test_stage1_scope.py tests/contract/test_stage2_scope.py -q`; record that the new Stage 2 manifest/dependency expectations fail before production changes.
- [ ] Add compatible constraints: `alembic>=1.20.0,<2.0.0`, `psycopg[binary,pool]>=3.3.6,<4.0.0`, `redis>=8.1.0,<9.0.0`, `pymilvus>=2.6.16,<2.7.0`; add `pytest-asyncio>=1.4.0,<2.0.0` to dev dependencies and refresh `uv.lock`.
- [ ] Define storage-only `IngestionStatus`, `FailedStage`, and `StoredChunkType`; define frozen `StoredDocument`, `StoredElement`, `StoredChunk`, and `RuntimeMetadata` models with nonblank canonical IDs, aware timestamps, all-or-none current/building triples, failed-stage consistency, JSON-safe structured metadata, and no graph/provider/benchmark fields.
- [ ] Test record immutability, unknown-field rejection, JSON round trips, source-path validation reuse, allowed statuses/types, and invalid current/building/failed combinations.
- [ ] Run focused tests, `make lint`, and `make typecheck`; commit as `stage(02): add persistence records and dependencies`.

## Task 2: Append-only PostgreSQL migration

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/20260928_0001_stage2_persistence.py`
- Create: `tests/persistence/test_migration_contract.py`

- [ ] Write failing static migration tests asserting exactly four business tables, an Alembic version table, required columns/indexes/checks, no destructive automatic DDL, and one linear migration head.
- [ ] Add an Alembic environment that reads `ATLASRAG_POSTGRES_DSN`, converts `postgresql://` to the Psycopg dialect when needed, has `target_metadata = None`, and supports offline SQL generation without importing AtlasRAG production modules.
- [ ] Create `documents` with unique `relative_source_path`, explicit source/status/failed-stage checks, nullable all-or-none current/building triples, JSONB parse metadata, and UTC timestamps.
- [ ] Create revision-scoped `document_elements` and `chunks`; add only the required document/revision, parent, and type indexes.
- [ ] Add the deferrable composite child-parent foreign key and a deferrable constraint trigger that rejects a same-document/revision parent whose `chunk_type` is not `TEXT_PARENT`.
- [ ] Create/seed the singleton `runtime_metadata` row with `query_cache_invalidation_required = false`.
- [ ] Run offline migration generation and focused static tests; commit as `stage(02): add canonical postgres migration`.

## Task 3: Async PostgreSQL pool, repositories, and explicit Unit of Work

**Files:**
- Create: `src/atlasrag/repositories/postgres/errors.py`
- Create: `src/atlasrag/repositories/postgres/pool.py`
- Create: `src/atlasrag/repositories/postgres/documents.py`
- Create: `src/atlasrag/repositories/postgres/elements.py`
- Create: `src/atlasrag/repositories/postgres/chunks.py`
- Create: `src/atlasrag/repositories/postgres/runtime_metadata.py`
- Create: `src/atlasrag/repositories/postgres/uow.py`
- Create: `tests/persistence/test_postgres_contracts.py`

- [ ] Write failing fake-connection tests for pool `open(wait=True)`/close/health, typed exception translation, not-found versus dependency failure, repository SQL parameterization, and the absence of repository `commit()` calls.
- [ ] Implement `PostgresPool` around `AsyncConnectionPool(open=False)` and a `SELECT 1` health probe; map pool wait timeouts to `DependencyTimeoutError` and connection failures to `DependencyUnavailableError`.
- [ ] Implement typed document `put`, `get_by_id`, `get_by_relative_source_path`, and deterministic `list` methods using `dict_row` and boundary validation.
- [ ] Implement element/chunk `replace_revision_set`, `count_revision`, `list_revision_ids`, `delete_revision`, and ordered revision reads. Delete then insert on the caller's connection; never auto-commit.
- [ ] Implement chunk identity lookup and canonical parent/table lookup; a row that cannot validate as `StoredChunk` raises `CanonicalDataError`, while legal absence returns `None`.
- [ ] Implement runtime metadata `get` and `set_query_cache_invalidation_required` against the singleton row.
- [ ] Implement `PostgresUnitOfWork`: acquire one connection with `getconn()`, expose all four repositories, commit only through explicit `await uow.commit()`, rollback on normal uncommitted exit and exceptional exit, and always return the connection.
- [ ] Test all repositories share the exact connection object, commit is single-use, post-finish operations fail, and driver exceptions never escape.
- [ ] Run focused tests and `make verify`; commit as `stage(02): implement postgres repositories and unit of work`.

## Task 4: Session advisory locks and PostgreSQL failure semantics

**Files:**
- Create: `src/atlasrag/repositories/postgres/locks.py`
- Modify: `src/atlasrag/repositories/postgres/errors.py`
- Modify: `tests/persistence/test_postgres_contracts.py`

- [ ] Write failing tests that map the first eight SHA-256 bytes of `document_id` into deterministic PostgreSQL signed int64 range, including stable positive and negative fixtures.
- [ ] Define `PostgresAdvisoryLockManager.try_acquire(document_id) -> AdvisoryLockLease | None`; acquire a dedicated pooled connection, call `pg_try_advisory_lock`, and retain that connection for the lease lifetime.
- [ ] Implement idempotent async lease release with `pg_advisory_unlock`; on cancellation/error, close or safely return the connection so PostgreSQL releases the session lock.
- [ ] Ensure false lock contention is the only path returning `None`; pool timeout/unavailability raises the frozen typed dependency errors.
- [ ] Add static tests forbidding `pg_advisory_xact_lock` and proving the lock manager is separate from the UoW transaction.
- [ ] Run focused tests and `make verify`; commit as `stage(02): add postgres advisory lock leases`.

## Task 5: Typed Redis cache providers

**Files:**
- Create: `src/atlasrag/providers/cache/__init__.py`
- Create: `src/atlasrag/providers/cache/models.py`
- Create: `src/atlasrag/providers/cache/keys.py`
- Create: `src/atlasrag/providers/cache/redis.py`
- Create: `tests/providers/test_redis_cache.py`

- [ ] Write failing tests for the exact keys `atlasrag:parent:{revision_id}:{parent_id}` and `atlasrag:query:{session_id}:{runtime_fingerprint}:{request_hash}`; reject blank/control-delimited key components and prove no knowledge-revision slot exists.
- [ ] Define frozen `CachedParentContext` containing full canonical content and `LocalProvenance`, plus `CachedQueryResult` containing schema version, aware creation time, and a complete `FinalResponse`.
- [ ] Implement async parent/query `get`, `put(ex=ttl)`, and `delete` capabilities using Pydantic JSON serialization and validation. Missing key returns `None`; malformed payload raises `ProviderResponseError`.
- [ ] Implement bounded `SCAN match=atlasrag:query:* count=<batch>` plus `UNLINK` batches. Never call `KEYS`, `FLUSHDB`, or `FLUSHALL`; parent and unrelated keys remain untouched.
- [ ] Add a Redis `PING` health probe and map Redis timeout/unavailable errors without converting them into cache misses.
- [ ] Test byte/string payloads, TTL arguments, corrupt data, scan cursor/batching, namespace isolation, and injected driver failures with async fakes.
- [ ] Run focused tests and `make verify`; commit as `stage(02): implement typed redis caches`.

## Task 6: Milvus record and frozen schema/index contract

**Files:**
- Create: `src/atlasrag/providers/index/__init__.py`
- Create: `src/atlasrag/providers/index/models.py`
- Create: `src/atlasrag/providers/index/schema.py`
- Create: `tests/providers/test_milvus_models.py`
- Create: `tests/providers/test_milvus_schema.py`

- [ ] Write failing model tests for `TEXT_CHILD` and `TABLE`, mandatory TEXT_CHILD parent, prohibited `TEXT_PARENT`, exact embedding dimension, finite float vectors, unique IDs, canonical source type, and typed structured metadata.
- [ ] Implement frozen `MilvusChunkRecord` without modifying `RetrievalCandidate` or any graph state.
- [ ] Define canonical collection fields for IDs/provenance/content/sparse text/JSON/dense vector; set analyzer-enabled `sparse_text`, a generated `SPARSE_FLOAT_VECTOR`, and a BM25 function from `sparse_text` to the sparse field.
- [ ] Define the dense index as HNSW/COSINE with `M=32`, `efConstruction=200`; define sparse index as `SPARSE_INVERTED_INDEX`/BM25 with `DAAT_MAXSCORE`, `bm25_k1=1.2`, `bm25_b=0.75`.
- [ ] Implement pure normalization/comparison functions for PyMilvus collection/index descriptions. Report every mismatch and fail closed; ignore only documented response-order/noise fields.
- [ ] Run focused tests and `make verify`; commit as `stage(02): define milvus schema and records`.

## Task 7: Async Milvus provider and exact-set primitives

**Files:**
- Create: `src/atlasrag/providers/index/milvus.py`
- Create: `tests/providers/test_milvus_provider.py`

- [ ] Write failing sync-client fake tests proving every blocking SDK call runs through the provider's thread boundary and that the public methods are async.
- [ ] Implement initialization: create the expected schema/functions/indexes when absent; when present, describe and compare them; never drop or recreate an incompatible collection.
- [ ] Implement insert validation and payload projection, excluding the generated sparse vector while supplying raw sparse text and dense vector.
- [ ] Implement `replace_revision_set` as delete → flush/visibility → insert complete unique set → flush/visibility, with document/revision consistency checked before the first SDK mutation.
- [ ] Implement revision count, paged/iterator chunk-ID enumeration, revision delete, document delete, and health probe. Escape filter literals deterministically rather than interpolating raw IDs.
- [ ] Map built-in/SDK timeouts to `DependencyTimeoutError`, connectivity/SDK failures to `DependencyUnavailableError`, schema mismatch to `ConfigurationError`, and invalid local records to `InvariantViolationError`; never return empty on technical failure.
- [ ] Test call ordering, empty replacements, visibility barriers, duplicate/mixed-scope rejection, fail-closed schema mismatch, and unavailable clients.
- [ ] Run focused tests and `make verify`; commit as `stage(02): implement milvus index provider`.

## Task 8: Pinned localhost-only Docker infrastructure and commands

**Files:**
- Create: `deploy/local/compose.yaml`
- Create: `deploy/local/.env.example`
- Create: `deploy/local/searxng/settings.yml`
- Create: `scripts/wait_for_infrastructure.py`
- Create: `scripts/check_migration.py`
- Create: `scripts/stage2_verify.sh`
- Modify: `Makefile`
- Modify: `.gitignore`
- Create: `tests/test_stage2_tooling.py`

- [ ] Write failing static tests asserting pinned non-`latest` images, `${...:?required}` credential substitution, localhost host bindings, no etcd/MinIO host ports, required named volumes, disposable Redis, and all requested Make targets.
- [ ] Compose PostgreSQL `17.8-alpine`, Redis `8.2.10-alpine`, etcd `v3.5.25`, MinIO `RELEASE.2024-12-18T13-15-44Z`, Milvus `v2.6.20`, and SearXNG `2026.9.25-12f8b6515`; use service health checks and internal dependency networking.
- [ ] Bind PostgreSQL, Redis, Milvus, Milvus health, and SearXNG only to `${ATLASRAG_BIND_HOST:-127.0.0.1}`. Persist PostgreSQL/etcd/MinIO/Milvus in named volumes; configure application Redis with RDB/AOF disabled.
- [ ] Add safe placeholder variables only; ignore `deploy/local/.env`; keep MinIO/PG passwords out of tracked Compose and SearXNG settings.
- [ ] Implement condition-based readiness using the production PostgreSQL/Redis/Milvus probes plus an HTTP-only SearXNG root check; no sleep-only success criterion.
- [ ] Add `infra-up`, `infra-wait`, `infra-status`, `infra-down`, `migrate`, `migration-check`, `test-integration`, and `stage2-verify`. Ordinary `infra-down` must not pass `-v`.
- [ ] Implement `stage2_verify.sh` with an EXIT trap: offline `make verify`, start, wait, migrate, check head, run integration tests, then always `docker compose down` without deleting local persistent volumes.
- [ ] Run tooling tests and `docker compose --env-file deploy/local/.env.example -f deploy/local/compose.yaml config`; commit as `stage(02): add local infrastructure tooling`.

## Task 9: PostgreSQL and Redis integration verification

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/conftest.py`
- Create: `tests/integration/test_postgres.py`
- Create: `tests/integration/test_redis.py`
- Modify: `pyproject.toml`

- [ ] Register the `integration` marker and keep it excluded from ordinary tests. Require `ATLASRAG_RUN_INTEGRATION=1` in integration fixtures so `make test-integration` never silently skips unavailable services.
- [ ] Add a fresh-database migration test that reaches the single Alembic head and sees exactly the expected four business tables and indexes.
- [ ] Test document path uniqueness/status checks/current-building round trips; element/chunk revision scoping; missing/cross-document/cross-revision/wrong-type parent rejection.
- [ ] Test element and chunk exact-set commit removes stale rows, while injected failure/no explicit commit/exception restores the old set.
- [ ] Test runtime marker persistence across pool reconnect and PostgreSQL unavailable/timeout translation.
- [ ] Test same-document advisory lock contention, different-document concurrency, explicit release/reacquire, and connection-close release.
- [ ] Test Redis parent revision isolation, query session/runtime/request isolation, TTL expiry, typed round trip, malformed payload, unavailable service, query-only invalidation, and preservation of parent/unrelated keys.
- [ ] Run `make migrate`, `make migration-check`, and the focused integration files against Compose; commit as `test(stage-02): verify postgres and redis integration`.

## Task 10: Milvus, Compose, and failure integration verification

**Files:**
- Create: `tests/integration/test_milvus.py`
- Create: `tests/integration/test_compose.py`

- [ ] Create a uniquely named test collection per run and register cleanup before creation; cleanup may drop only that test collection.
- [ ] Test initial creation and compatible reopen, plus a deliberately incompatible isolated collection that raises `ConfigurationError` and remains present.
- [ ] Test accepted TEXT_CHILD/TABLE records, rejected TEXT_PARENT, wrong dimension, non-finite vector, revision count/ID enumeration, exact replacement, revision deletion, document deletion, and write/delete visibility.
- [ ] Perform deterministic minimal dense HNSW and built-in BM25 searches over fixed local records; assert expected IDs, not relevance-quality metrics.
- [ ] Test unreachable Milvus translation without turning failure into zero records.
- [ ] Assert all published host ports resolve to `127.0.0.1`, etcd/MinIO have no host bindings, all required services report healthy, and SearXNG responds locally without issuing a search.
- [ ] Run the focused integration files twice to expose namespace/cleanup leaks; commit as `test(stage-02): verify milvus and compose integration`.

## Task 11: Architecture regression, CI, documentation, and publication

**Files:**
- Modify: `.github/workflows/ci.yml` only if needed to preserve the offline gate
- Create: `.github/workflows/integration.yml`
- Modify: `README.md`
- Modify: `docs/stages/README.md`
- Create: `docs/stages/stage-02-infrastructure-persistence.md`

- [ ] Extend AST checks to prove repositories do not import graph/provider/benchmark layers, providers do not import repositories/graphs/benchmarks, production never imports tests/benchmarks, and repository modules contain no `.commit()` call outside `uow.py`.
- [ ] Add an integration workflow that syncs the frozen lock, creates ephemeral non-secret local credentials at runtime, uses a unique Compose project, runs `make stage2-verify`, prints logs on failure, and always performs `docker compose down -v --remove-orphans`.
- [ ] Keep `.github/workflows/ci.yml` secret-free, CPU-only, and service-free with `make verify`.
- [ ] Document resolved Python/container versions, Compose topology, migration head, four tables/indexes, UoW and lock semantics, exact Redis keys/value schemas, Milvus fields/functions/index parameters, failure mapping, test isolation, and deferred Stage 3+ work.
- [ ] Run `uv sync`, `make format`, `make format-check`, `make lint`, `make typecheck`, `make architecture-check`, `make test`, and `make verify` with PostgreSQL/Redis/Milvus/SearXNG stopped; record exact offline test count.
- [ ] Run `make stage2-verify` from a clean Compose state; record exact unit/contract and integration counts, migration head, collection/index verification, service status, and cleanup result.
- [ ] Run `git diff --check`, secret scans, tracked-artifact checks, Compose config validation, and a complete `origin/main...HEAD` audit.
- [ ] Request an independent Stage 2 spec-compliance review and a separate code-quality review; fix all Critical/Important findings and rerun both verification paths.
- [ ] Finalize the Stage report only after evidence exists. State `Architecture deviations: NONE` only if true, and state explicitly that Stage 3 has not started.
- [ ] Commit, push `stage/02-infrastructure-persistence`, and wait for both branch CI workflows to finish green. Do not merge automatically and do not start Stage 3.

## Plan self-review

- Spec coverage: all 68 sections map to Tasks 1-11, including service topology, persistence ownership, exact-set operations, rollback, locks, error translation, schema fail-closed behavior, deterministic smoke tests, CI separation, docs, and publication.
- Stage boundary: the only prior-test change is the unavoidable transition of the Stage 1 temporary "no Stage 2 code/dependencies" allowlist. Frozen Stage 1 business contracts and their tests remain unchanged and green.
- Type consistency: PostgreSQL accepts `Stored*` records, Redis accepts only `Cached*` values, and Milvus accepts only `MilvusChunkRecord`; none are added to Stage 1 graph states.
- Destructive behavior: production code has no automatic drop/truncate path. Volume deletion and isolated collection/table cleanup exist only in explicit CI/test cleanup.
- Deferred work: no parser, chunk builder, embedding model, publish coordinator, retrieval service, RRF/reranking, SearXNG provider, cache policy, RuntimeGate, benchmark, or Stage 3 graph appears in this plan.
