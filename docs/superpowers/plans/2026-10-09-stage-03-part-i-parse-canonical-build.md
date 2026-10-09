# AtlasRAG Stage 3 Part I Parse & Canonical Build Implementation Plan

> **For Codex:** Use `superpowers:executing-plans` and
> `superpowers:test-driven-development` while executing this plan. Complete each
> red-green-refactor checkpoint in order and do not begin Stage 4 work.

**Goal:** Build deterministic, secure, production-validated canonical parsing
and Parent/Child/Table chunk persistence for PDF, DOCX, XLSX, Markdown, and text
without changing the frozen Stage 1/2 contracts or database schema.

**Architecture:** Parser providers normalize directly into immutable AtlasRAG
canonical models. Pure identity, validation, analysis, and chunking services
produce deterministic output. Explicit typed/versioned mappers translate that
output into the frozen Stage 2 `Stored*` records, and small services use the
existing repositories and caller-owned Unit of Work to persist exact candidate
sets. Heavy parser SDKs remain lazy, non-default dependency groups.

**Tech stack:** Python 3.11, Pydantic v2, asyncio, psycopg 3, pytest,
pytest-asyncio, Ruff, mypy, uv, Docling Slim, MinerU 4, Docker Compose
PostgreSQL, GitHub Actions.

**Authoritative design:**
`docs/superpowers/specs/2026-10-09-stage-03-part-i-parse-canonical-build-design.md`

---

## Task 1: Freeze Stage 3 dependency, scope, and architecture gates

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `scripts/check_import_boundaries.py`
- Create: `tests/stage3/__init__.py`
- Create: `tests/stage3/test_stage3_scope.py`
- Modify: `tests/test_import_boundaries.py`
- Modify: `tests/contract/test_stage2_scope.py`

### Step 1: Write the failing scope tests

Add tests that require:

- `parsing-docling` to contain only a bounded
  `docling-slim[format-office]` direct requirement;
- `parsing-mineru` to contain only a bounded MinerU 4 direct requirement;
- neither group to be in uv's default groups;
- ordinary production dependencies not to gain LangGraph, model, embedding,
  reranking, benchmark, or parser packages;
- the Alembic head and migration-file set to remain exactly Stage 2's set;
- Stage 3 package roots to exist; and
- the architecture checker to reject parser-SDK imports from ingestion domain,
  repository imports from parser providers, and Redis/Milvus/LangGraph imports
  from Stage 3 services.

Use temporary source trees in the import-boundary tests so every forbidden
edge is proven by a red test rather than only by inspecting current files.

### Step 2: Run the tests and confirm RED

Run:

```bash
uv run --frozen pytest tests/stage3/test_stage3_scope.py \
  tests/test_import_boundaries.py tests/contract/test_stage2_scope.py -q
```

Expected: failures for missing groups/package roots and missing Stage 3 layer
rules.

### Step 3: Add minimal dependency and boundary declarations

Add bounded non-default groups to `pyproject.toml`:

```toml
parsing-docling = ["docling-slim[format-office]>=2.135.0,<2.136.0"]
parsing-mineru = ["mineru>=4.0.11,<4.1.0"]
```

Resolve the lock, record the actual versions, and adjust only if uv proves a
compatibility conflict. Create empty package roots needed by the scope test.
Extend `scripts/check_import_boundaries.py` with path-specific rules; do not
weaken existing global, repository, or provider rules.

### Step 4: Prove default and optional installation behavior

Run:

```bash
uv lock
uv sync --frozen
uv run --frozen python -c "import importlib.util; assert importlib.util.find_spec('docling') is None; assert importlib.util.find_spec('mineru') is None"
uv sync --frozen --group parsing-docling
uv run --frozen python -c "import importlib.util; assert importlib.util.find_spec('docling') is not None"
```

Use a temporary uv environment if the existing worktree environment has been
contaminated by optional-group validation. Expected: the lock resolves both
groups, while a clean default sync installs neither parser SDK.

### Step 5: Run focused GREEN and commit

Run the Step 2 command again, then:

```bash
git add pyproject.toml uv.lock scripts/check_import_boundaries.py \
  tests/stage3/__init__.py tests/stage3/test_stage3_scope.py \
  tests/test_import_boundaries.py tests/contract/test_stage2_scope.py \
  src/atlasrag/domain/ingestion/__init__.py \
  src/atlasrag/providers/parsing/__init__.py \
  src/atlasrag/services/ingestion/__init__.py
git commit -m "build(stage-03): isolate parser dependencies and boundaries"
```

---

## Task 2: Implement secure source identity and mutation detection

**Files:**

- Create: `src/atlasrag/domain/ingestion/enums.py`
- Create: `src/atlasrag/domain/ingestion/source.py`
- Create: `src/atlasrag/services/ingestion/identity.py`
- Create: `src/atlasrag/services/ingestion/source_guard.py`
- Create: `tests/stage3/test_source_identity.py`
- Create: `tests/stage3/test_source_guard.py`

### Step 1: Write failing source and identity tests

Cover:

- exact case-insensitive suffix routing eligibility for `.pdf`, `.docx`,
  `.xlsx`, `.md`, and `.txt`;
- canonical POSIX relative paths and rejection of absolute, dotted, traversal,
  NUL, control-character, directory, and unsupported-suffix inputs;
- allowed in-root symlinks and rejected escaping symlinks;
- `document_id` depending only on canonical relative path;
- bounded streaming `content_hash` with multiple reads for a synthetic file;
- `revision_id` changing for source bytes or supplied pipeline fingerprint;
- `CandidateBuildContext` containing data only and stable JSON-safe IDs; and
- no use of UUID or absolute path in canonical identities.

### Step 2: Confirm RED

```bash
uv run --frozen pytest tests/stage3/test_source_identity.py \
  tests/stage3/test_source_guard.py -q
```

Expected: import failures for the new contracts.

### Step 3: Implement the minimal immutable contracts and algorithms

Implement `SourceType` integration, `SourceFile`, `CanonicalSource`, and
`CandidateBuildContext` using the frozen `FrozenModel`. Reuse
`canonical_sha256`; stream raw file bytes with a fixed chunk size. Resolve and
validate roots before opening files. Keep absolute paths execution-only.

Implement a mutation guard that hashes after parser work, returns normally for
an equal hash, and raises the frozen `SourceMutationError` for a mismatch.

### Step 4: Run GREEN, static checks, and commit

```bash
uv run --frozen pytest tests/stage3/test_source_identity.py \
  tests/stage3/test_source_guard.py -q
uv run --frozen mypy src/atlasrag/domain/ingestion/source.py \
  src/atlasrag/services/ingestion/identity.py \
  src/atlasrag/services/ingestion/source_guard.py tests/stage3/test_source_identity.py \
  tests/stage3/test_source_guard.py
git add src/atlasrag/domain/ingestion tests/stage3/test_source_identity.py \
  tests/stage3/test_source_guard.py src/atlasrag/services/ingestion/identity.py \
  src/atlasrag/services/ingestion/source_guard.py
git commit -m "feat(stage-03): add secure source identity"
```

---

## Task 3: Implement canonical parsed-document contracts

**Files:**

- Create: `src/atlasrag/domain/ingestion/document.py`
- Create: `src/atlasrag/domain/ingestion/elements.py`
- Modify: `src/atlasrag/domain/ingestion/__init__.py`
- Create: `tests/stage3/test_document_contracts.py`
- Create: `tests/stage3/test_table_contracts.py`

### Step 1: Write failing immutable-contract tests

Cover all enum values and valid/invalid combinations for:

- `ParseInfo`: usable `SUCCESS/RELIABLE`, `SUCCESS/PARTIAL`, and
  `DEGRADED/PARTIAL`; failed `FAILED/UNAVAILABLE`; stable warning/failure codes;
- `DocumentMetadata` and provider/version recording;
- `CanonicalElementMetadata` without vendor-native payloads;
- rectangular `TableData`, no data-row promotion to headers, merged/missing
  cell representation, caption/sheet/anchor preservation;
- TABLE requiring `TableData` and non-TABLE forbidding it;
- Unicode NFC/line-ending normalization and surrogate/NUL rejection;
- element identity sensitivity to revision, order, type, content, section,
  anchor, and table data;
- nonnegative, unique, strictly increasing element order;
- document/element document and revision equality; and
- immutable nested tuples, strict enums, forbidden unknown fields, and stable
  JSON round trips.

### Step 2: Confirm RED

```bash
uv run --frozen pytest tests/stage3/test_document_contracts.py \
  tests/stage3/test_table_contracts.py -q
```

### Step 3: Implement models and deterministic normalization

Create strict enums for parse outcome/reliability and element type. Implement
typed anchors by reusing the frozen `SourceAnchor` where it fully represents
the fact; do not modify that frozen contract. Add deterministic element-ID
factory functions in `identity.py` rather than random/default IDs.

Keep failed parse business results separate from usable
`DocumentContainer` instances if needed to preserve the nonempty usable
document invariant.

### Step 4: Run GREEN, serialization regression, and commit

```bash
uv run --frozen pytest tests/stage3/test_document_contracts.py \
  tests/stage3/test_table_contracts.py tests/contract/test_serialization_roundtrip.py -q
git add src/atlasrag/domain/ingestion src/atlasrag/services/ingestion/identity.py \
  tests/stage3/test_document_contracts.py tests/stage3/test_table_contracts.py
git commit -m "feat(stage-03): define canonical parsed document"
```

---

## Task 4: Implement parser protocol, routing, TXT, and Markdown

**Files:**

- Create: `src/atlasrag/providers/parsing/base.py`
- Create: `src/atlasrag/providers/parsing/routing.py`
- Create: `src/atlasrag/providers/parsing/text.py`
- Create: `src/atlasrag/providers/parsing/markdown.py`
- Modify: `src/atlasrag/providers/parsing/__init__.py`
- Create: `tests/stage3/test_parser_routing.py`
- Create: `tests/stage3/test_text_parser.py`
- Create: `tests/stage3/test_markdown_parser.py`
- Create: `tests/fixtures/stage3/simple.txt`
- Create: `tests/fixtures/stage3/simple.md`

### Step 1: Write failing routing/native parser tests

Require exact routing and no fallback. For text, test paragraphs, Unicode,
UTF-8 BOM, CRLF/CR normalization, invalid UTF-8, and empty-file business
failure. For Markdown, test nested ATX headings, paragraphs, ordered/unordered
lists, fenced code, pipe tables, Unicode, deterministic section paths, source
order, and unsupported syntax retained as content.

Patch Python import machinery in a test and prove importing/running native
parsers does not import Docling or MinerU.

### Step 2: Confirm RED

```bash
uv run --frozen pytest tests/stage3/test_parser_routing.py \
  tests/stage3/test_text_parser.py tests/stage3/test_markdown_parser.py -q
```

### Step 3: Implement the minimum parser boundary

Define an async `DocumentParser` protocol. Route by validated `SourceType`, not
by MIME guessing. Implement small deterministic native parsers with no broad
exception-to-empty conversion. Build canonical elements through shared helper
functions so IDs and normalization are uniform.

### Step 4: Run GREEN, determinism replay, and commit

```bash
uv run --frozen pytest tests/stage3/test_parser_routing.py \
  tests/stage3/test_text_parser.py tests/stage3/test_markdown_parser.py -q
git add src/atlasrag/providers/parsing tests/stage3/test_parser_routing.py \
  tests/stage3/test_text_parser.py tests/stage3/test_markdown_parser.py \
  tests/fixtures/stage3/simple.txt tests/fixtures/stage3/simple.md
git commit -m "feat(stage-03): parse native text and markdown"
```

---

## Task 5: Normalize MinerU output and add the lazy PDF adapter

**Files:**

- Create: `src/atlasrag/providers/parsing/mineru_normalizer.py`
- Create: `src/atlasrag/providers/parsing/mineru.py`
- Modify: `src/atlasrag/providers/parsing/routing.py`
- Create: `tests/stage3/test_mineru_normalizer.py`
- Create: `tests/stage3/test_mineru_adapter.py`
- Create: `tests/fixtures/parser_outputs/mineru/v4/simple.json`
- Create: `tests/fixtures/stage3/simple.pdf`
- Create: `scripts/generate_stage3_fixtures.py`

### Step 1: Write failing frozen-normalizer tests

From a tiny real MinerU 4 `ParseResult.to_dict()` export of the repository PDF,
test heading, paragraph/list/code/table ordering, anchors, normalization, and
stable parser version. Mutate/remove required fixture keys and require
`ProviderResponseError`. Add incidental timestamp/temp-path/random-ID fields
and prove canonical output and IDs remain unchanged.

### Step 2: Write failing lazy-adapter tests

Without the optional group installed, importing the module must work and
calling the PDF parser must raise `DependencyUnavailableError`. With a fake
module injected at the lazy boundary, assert the adapter calls the local SDK
with `tier="flash"`, full pages, converts `to_dict()`, and translates only
documented dependency timeout/unavailable and malformed-response cases.

### Step 3: Confirm RED

```bash
uv run --frozen pytest tests/stage3/test_mineru_normalizer.py \
  tests/stage3/test_mineru_adapter.py -q
```

### Step 4: Implement fail-closed normalization and adapter

Keep every MinerU import inside the runtime boundary. Accept only the observed,
versioned MinerU 4 shape. Do not retain provider objects or raw payloads and do
not add a PyPDF/Docling fallback.

Generate the PDF deterministically with a tiny dependency-free writer in the
fixture script; check in both the small input and sanitized output fixture.

### Step 5: Run GREEN and commit

```bash
uv run --frozen pytest tests/stage3/test_mineru_normalizer.py \
  tests/stage3/test_mineru_adapter.py -q
git add src/atlasrag/providers/parsing tests/stage3/test_mineru_*.py \
  tests/fixtures/parser_outputs/mineru/v4/simple.json \
  tests/fixtures/stage3/simple.pdf scripts/generate_stage3_fixtures.py
git commit -m "feat(stage-03): normalize MinerU PDF output"
```

---

## Task 6: Add the lazy Docling Office adapter and real fixtures

**Files:**

- Create: `src/atlasrag/providers/parsing/docling.py`
- Modify: `src/atlasrag/providers/parsing/routing.py`
- Modify: `scripts/generate_stage3_fixtures.py`
- Create: `tests/stage3/test_docling_adapter.py`
- Create: `tests/integration/test_stage3_docling.py`
- Create: `tests/fixtures/stage3/simple.docx`
- Create: `tests/fixtures/stage3/simple.xlsx`

### Step 1: Write failing lazy and normalized-shape tests

Require import without Docling installed, typed unavailable failure when
called, exact DOCX/XLSX acceptance, PDF rejection, reading-order normalization,
heading/paragraph/list/table extraction, structural cells, and XLSX sheet name.
Use small fake protocol-shaped objects only to isolate mapping branches.

### Step 2: Write failing real integration tests

Mark the real tests `integration`. Parse deterministic tiny DOCX and XLSX
fixtures with actual Docling. Assert the expected canonical semantic facts,
not provider-private object details.

### Step 3: Implement the lazy Docling boundary

Discover the installed Docling API through official public imports and keep
them lazy. Normalize only documented fields, translate documented SDK
failures, and fail closed on shapes that cannot preserve table structure.

Generate fixtures deterministically and license-safely. If fixture generation
needs Docling-adjacent packages already brought by the optional group, keep the
script outside production.

### Step 4: Run unit and real GREEN, then commit

```bash
uv run --frozen pytest tests/stage3/test_docling_adapter.py -q
uv run --frozen --group parsing-docling pytest \
  tests/integration/test_stage3_docling.py -m integration -q
git add src/atlasrag/providers/parsing/docling.py \
  src/atlasrag/providers/parsing/routing.py scripts/generate_stage3_fixtures.py \
  tests/stage3/test_docling_adapter.py tests/integration/test_stage3_docling.py \
  tests/fixtures/stage3/simple.docx tests/fixtures/stage3/simple.xlsx
git commit -m "feat(stage-03): parse Office sources with Docling"
```

---

## Task 7: Implement canonical chunk contracts, analysis, and identities

**Files:**

- Create: `src/atlasrag/domain/ingestion/chunks.py`
- Create: `src/atlasrag/services/ingestion/analysis.py`
- Create: `src/atlasrag/services/ingestion/chunking.py`
- Modify: `src/atlasrag/services/ingestion/identity.py`
- Create: `tests/stage3/test_chunk_contracts.py`
- Create: `tests/stage3/test_chunking.py`
- Create: `tests/stage3/test_table_chunking.py`
- Create: `tests/stage3/test_determinism.py`

### Step 1: Write failing lineage and chunk-contract tests

Cover strict typed `ElementSpan`, Parent/Child/Table provenance, `TableSlice`,
and `ChunkRecord`. Assert exact role/strategy/parent cross-invariants, range
bounds, same document/revision, stable content hashes, deterministic chunk
IDs, contiguous order, immutable JSON round trips, and absence of vector,
score, query, session, citation, benchmark, or provider fields.

### Step 2: Write failing text chunking tests

Require:

- same-section contiguous Parent grouping;
- a structural atom split when one element exceeds the Parent limit;
- separator priority and Unicode-scalar fallback;
- complete source-span coverage with no trim/truncation;
- zero-overlap ordered Children covering every Parent character exactly once;
- every Child referencing a Parent, never a Child/Table;
- Parent emitted immediately before its Children; and
- a large synthetic document without quadratic concatenation behavior.

Use a deliberately small `ChunkingProfile` in tests. Add a static assertion
that output-affecting profile changes require the supplied
`chunker_behavior_version` to change in persisted strategy metadata.

### Step 3: Write failing table chunking tests

Cover FULL, ROW_GROUP, repeated headers, exact row coverage/order, source
element lineage, sheet name/caption/anchor preservation, and fail-closed
oversized header/cell/row behavior.

### Step 4: Implement pure analysis and chunking

Use list accumulation and final joins, never repeated whole-string copying.
Make `ChunkingProfile` immutable with defaults documented in the approved
design. Keep strategy selection limited to STRUCTURE, RECURSIVE, and TABLE.

### Step 5: Run GREEN, repeat determinism, and commit

```bash
uv run --frozen pytest tests/stage3/test_chunk_contracts.py \
  tests/stage3/test_chunking.py tests/stage3/test_table_chunking.py \
  tests/stage3/test_determinism.py -q
git add src/atlasrag/domain/ingestion/chunks.py \
  src/atlasrag/services/ingestion/{analysis,chunking,identity}.py \
  tests/stage3/test_chunk_contracts.py tests/stage3/test_chunking.py \
  tests/stage3/test_table_chunking.py tests/stage3/test_determinism.py
git commit -m "feat(stage-03): build deterministic canonical chunks"
```

---

## Task 8: Add fail-closed production validation

**Files:**

- Create: `src/atlasrag/domain/ingestion/validation.py`
- Create: `src/atlasrag/services/ingestion/validation.py`
- Create: `tests/stage3/test_production_validation.py`

### Step 1: Write failing result and invariant tests

Require immutable `ValidationResult(outcome, reason_codes)` with stable ordered
unique codes only. Test PASS for valid parsed/chunked outputs and FAIL for all
specified identity, usability, duplicate, order, Unicode/JSON, table,
Parent/Child, lineage, content-hash, and coverage violations. Ensure no LLM
explanation, Chain-of-Thought, benchmark score, or free-form provider message
exists in the result.

### Step 2: Confirm RED

```bash
uv run --frozen pytest tests/stage3/test_production_validation.py -q
```

### Step 3: Implement deterministic validators

Return all stable reason codes in a fixed rule order. Do not silently repair
input. Treat duplicate IDs, broken lineage, invalid hashes, cross-revision
references, malformed tables, and truncation as FAIL.

### Step 4: Run GREEN and commit

```bash
uv run --frozen pytest tests/stage3/test_production_validation.py -q
git add src/atlasrag/domain/ingestion/validation.py \
  src/atlasrag/services/ingestion/validation.py \
  tests/stage3/test_production_validation.py
git commit -m "feat(stage-03): validate canonical production output"
```

---

## Task 9: Implement typed, versioned persistence mappers

**Files:**

- Create: `src/atlasrag/services/ingestion/mapping.py`
- Create: `tests/stage3/test_persistence_mapping.py`

### Step 1: Write failing typed-payload tests

Require immutable extra-forbid schema-version-1 payloads for document parse
metadata, element structured content/metadata, and chunk metadata/strategy
metadata. Test semantic canonical-to-Stored-to-canonical round trips for every
element and chunk kind.

Mutate payloads to unknown/missing versions, missing/extra keys, unknown enums,
bad nested ranges, contradictory first-class fields, wrong content hashes,
wrong sheet/anchor/parent/type, and invalid TABLE/non-TABLE combinations; every
read must raise `CanonicalDataError`.

### Step 2: Confirm RED

```bash
uv run --frozen pytest tests/stage3/test_persistence_mapping.py -q
```

### Step 3: Implement explicit bidirectional mappers

Serialize only via `model_dump(mode="json")` into Stage 2 JSON columns. Accept
an injected aware UTC timestamp for Stored records. Revalidate typed payloads
on every read and wrap only Pydantic/canonical-data validation failures in the
frozen safe error. Sort reconstructed chunks by validated canonical `order`.

Do not alter `Stored*`, repositories, migration, or database columns.

### Step 4: Run GREEN plus frozen Stage 2 record tests and commit

```bash
uv run --frozen pytest tests/stage3/test_persistence_mapping.py \
  tests/persistence/test_records.py tests/contract/test_stage2_scope.py -q
git add src/atlasrag/services/ingestion/mapping.py \
  tests/stage3/test_persistence_mapping.py
git commit -m "feat(stage-03): map canonical records to frozen storage"
```

---

## Task 10: Implement small candidate persistence capabilities

**Files:**

- Create: `src/atlasrag/services/ingestion/persistence.py`
- Create: `src/atlasrag/services/ingestion/candidate.py`
- Create: `tests/stage3/fakes.py`
- Create: `tests/stage3/test_candidate_services.py`
- Create: `tests/stage3/test_source_mutation_flow.py`

### Step 1: Write failing fake-UoW transition tests

Build narrow async repository/UoW protocols and in-memory fakes. Cover:

- new and current-R1 candidate preparation;
- rejection of a different existing building revision;
- exact legal PARSING/PARSED/CHUNKING/CHUNKED transitions;
- parsed/chunked exact-set retry replacing stale rows;
- validation before writes;
- explicit commit exactly once and rollback/no commit on failure;
- preservation of all current revision fields;
- explicit PARSE/CHUNK business failure recording;
- unexpected exception/cancellation propagation without `FAILED` conversion;
- candidate discard deleting candidate rows and clearing building/failure;
- discard restoring READY with current and RECEIVED without current; and
- no query-cache invalidation or publication mutation.

### Step 2: Write failing controlled mutation-flow tests

Use a parser callback that modifies the source deterministically after reading.
Assert the coordinator invokes discard, writes no parsed/chunked output, and
never invokes the failure recorder. Test both current and new-document status.

### Step 3: Confirm RED

```bash
uv run --frozen pytest tests/stage3/test_candidate_services.py \
  tests/stage3/test_source_mutation_flow.py -q
```

### Step 4: Implement focused capabilities

Implement separate preparation, transition, parsed persistence, chunk
persistence, failure, and discard services. Each operation creates one short
UoW and explicitly commits. Keep parser/hash/analyze/chunk work outside UoWs.
Do not add a production graph or one God ingestion service.

### Step 5: Run GREEN and commit

```bash
uv run --frozen pytest tests/stage3/test_candidate_services.py \
  tests/stage3/test_source_mutation_flow.py -q
git add src/atlasrag/services/ingestion/{candidate,persistence}.py \
  tests/stage3/fakes.py tests/stage3/test_candidate_services.py \
  tests/stage3/test_source_mutation_flow.py
git commit -m "feat(stage-03): persist candidate parse and chunks"
```

---

## Task 11: Prove Stage 3 transactions against PostgreSQL

**Files:**

- Create: `tests/integration/test_stage3_postgres.py`
- Create: `scripts/wait_for_postgres.py`
- Modify: `tests/integration/conftest.py`

### Step 1: Write failing real transaction tests

Against only PostgreSQL, cover all Stage 3 integration cases required by the
execution specification: preparation, R1-current/R2-building preservation,
PARSING, atomic PARSED exact-set/rollback/retry, CHUNKING, atomic CHUNKED
exact-set/rollback/retry, mutation discard, unpublished new documents, and
PARSE/CHUNK failure recording.

Inject a controlled repository error after part of an exact-set write and
verify transaction rollback from a fresh UoW.

### Step 2: Confirm RED with PostgreSQL only

```bash
docker compose --env-file deploy/local/.env -f deploy/local/compose.yaml up -d postgres
uv run --env-file deploy/local/.env --frozen python scripts/wait_for_postgres.py
uv run --env-file deploy/local/.env --frozen alembic upgrade head
ATLASRAG_RUN_INTEGRATION=1 uv run --env-file deploy/local/.env --frozen pytest \
  tests/integration/test_stage3_postgres.py -m integration -q
```

Expected: failures until production services use the real Stage 2 UoW shape.

### Step 3: Adapt protocols, not frozen repositories

Make the Stage 3 service protocols structurally compatible with
`PostgresUnitOfWork`. Correct Stage 3 code only; do not add repository commits,
new methods, columns, or migrations.

### Step 4: Run GREEN, prove no other service is required, and commit

```bash
docker compose --env-file deploy/local/.env -f deploy/local/compose.yaml ps
ATLASRAG_RUN_INTEGRATION=1 uv run --env-file deploy/local/.env --frozen pytest \
  tests/integration/test_stage3_postgres.py -m integration -q
docker compose --env-file deploy/local/.env -f deploy/local/compose.yaml down
git add tests/integration/test_stage3_postgres.py scripts/wait_for_postgres.py \
  tests/integration/conftest.py src/atlasrag/services/ingestion
git commit -m "test(stage-03): verify PostgreSQL candidate transactions"
```

---

## Task 12: Add Stage 3 commands and CI separation

**Files:**

- Modify: `Makefile`
- Create: `scripts/stage3_verify.sh`
- Create: `scripts/stage3_parser_smoke.py`
- Create: `tests/stage3/test_stage3_tooling.py`
- Create: `tests/integration/test_stage3_mineru_smoke.py`
- Modify: `.github/workflows/integration.yml`
- Modify: `tests/test_ci_workflow.py`
- Modify: `tests/test_integration_configuration.py`

### Step 1: Write failing tooling/CI tests

Require commands:

```text
make test-stage3
make test-stage3-integration
make stage3-parser-smoke
make stage3-verify
```

Assert:

- `test-stage3` and ordinary `make verify` use default locked dependencies;
- Stage 3 integration starts PostgreSQL only and runs real Docling fixtures via
  `parsing-docling`;
- parser smoke runs `simple.pdf` through real MinerU flash via
  `parsing-mineru`;
- cleanup always stops PostgreSQL without deleting user volumes by default;
- normal CI remains parser/model-free; and
- Stage 2 integration workflow remains present and unchanged in semantics.

### Step 2: Confirm RED

```bash
uv run --frozen pytest tests/stage3/test_stage3_tooling.py \
  tests/test_ci_workflow.py tests/test_integration_configuration.py -q
```

### Step 3: Implement safe commands and workflow job

Use a shell trap in `stage3_verify.sh`. Compose
`stage3-verify = make verify + PostgreSQL Stage 3 integration + real Docling +
native/frozen parser tests`; keep MinerU in the separate mandatory
`stage3-parser-smoke` gate. Pin all GitHub Actions by immutable commit as the
existing workflows do.

The real MinerU smoke script must report SDK/model version and semantic output
counts without printing source content, vendor payloads, secrets, or paths.

### Step 4: Run tooling GREEN and commit

```bash
uv run --frozen pytest tests/stage3/test_stage3_tooling.py \
  tests/test_ci_workflow.py tests/test_integration_configuration.py -q
make test-stage3
git add Makefile scripts/stage3_verify.sh scripts/stage3_parser_smoke.py \
  tests/stage3/test_stage3_tooling.py tests/integration/test_stage3_mineru_smoke.py \
  .github/workflows/integration.yml tests/test_ci_workflow.py \
  tests/test_integration_configuration.py
git commit -m "ci(stage-03): add parser and integration gates"
```

---

## Task 13: Complete documentation, full verification, review, and publication

**Files:**

- Modify: `README.md`
- Create: `docs/stages/stage-03-part-i-parse-canonical-build.md`
- Modify only if evidence requires: files changed in Tasks 1–12

### Step 1: Write the Stage report from verified facts

Record every field required by Stage 3 §94, including exact locked parser
versions, algorithms, schemas, policies, transaction semantics, test counts,
real Docling/MinerU evidence, deferrals, architecture deviations, commits,
push, and remote checks. Mark incomplete evidence honestly until each gate has
run. Document explicit full-runtime installation:

```bash
uv sync --frozen --group parsing-docling --group parsing-mineru
```

### Step 2: Run focused and offline gates

```bash
uv run --frozen ruff format .
make test-stage3
make verify
git diff --check
```

Expected: all pass without network, Docker, parser SDK, models, or service
credentials for `make verify`.

### Step 3: Run real Stage 2 and Stage 3 infrastructure gates

```bash
make stage2-verify
make stage3-verify
```

Expected: Stage 2's entire gate remains green; Stage 3 uses only PostgreSQL and
Docling for its integration portion.

### Step 4: Run the mandatory real MinerU gate

```bash
make stage3-parser-smoke
```

Expected: `simple.pdf -> real MinerU flash -> DocumentContainer` passes. A
frozen fixture or fake cannot satisfy this step. If it cannot pass in the
reference environment, stop with Stage 3 BLOCKED.

### Step 5: Perform final architecture and security review

Use `superpowers:requesting-code-review` and
`superpowers:verification-before-completion`. Confirm:

- no frozen contract or migration changed;
- no parser SDK import occurs in default import paths;
- no production dependency on tests/benchmarks;
- no vectors/Milvus/Redis/LangGraph/query/session/benchmark semantics leaked;
- no provider payload, secret, runtime data, log, model, or `.env` is tracked;
- repositories still never auto-commit;
- every candidate transaction and mutation recovery invariant holds; and
- `git status`, `git diff --check`, and commit history are clean/auditable.

### Step 6: Finalize, commit, and push normally

Update the report with final evidence, then run the repository's safe
finalizer as applicable. Commit only tracked Stage 3 deliverables:

```bash
git add README.md docs/stages/stage-03-part-i-parse-canonical-build.md \
  <review-driven Stage 3 fixes only>
git commit -m "docs(stage-03): record verification evidence"
git push -u origin stage/03-part-i-parse-canonical-build
```

Never force-push and never merge automatically. Verify the remote branch head
and required GitHub checks. Stage 3 is COMPLETE only after the push succeeds
and all applicable remote checks are green.
