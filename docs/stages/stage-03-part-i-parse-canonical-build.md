# AtlasRAG Stage 3 — Part I: Parse & Canonical Build

## Stage

Stage 3 — Part I: Parse & Canonical Build.

## Status

**LOCAL CLOSEOUT GATES AND FINAL REVIEWS PASS — commit and publication are
PENDING.**

The offline, complete Stage 2 service-backed, Stage 3 PostgreSQL/Docling, and
mandatory real MinerU gates have passed locally. Implementation through the
Stage 3 command/CI separation is committed; review-driven Stage 2 selector and
immutable CI-action fixes plus this documentation are still uncommitted. The
final architecture review is `ARCHITECTURE READY`, and the final security
review is `SECURITY RELEASE READY` after the CI pinning and parser exception
boundary corrections. This report deliberately does not declare Stage 3
COMPLETE: the closeout commit, normal GitHub push, and applicable remote checks
remain PENDING.

## Authority, base, and scope

- Architecture authority:
  `docs/architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md`
- Base: `origin/main` at
  `7be89c84ae4fc5766914cf987c5c625cd82c9319`
- Base evidence: that commit is the merge of Stage 2 pull request #3, is the
  current locally observed `origin/main`, and is the merge base/ancestor of the
  Stage 3 branch.
- Branch: `stage/03-part-i-parse-canonical-build`
- Current implementation HEAD before this report:
  `60e36e30052e579d6cfccb4973a9dc323f57ee81`
- Database migration: **NONE**
- Additional provider-neutral intermediate AST: **NONE** beyond AtlasRAG's
  canonical models.

This Stage turns one supported source file into normalized parser facts and
deterministic Parent/Child/Table chunks, then establishes the `PARSED` and
`CHUNKED` candidate boundaries in the frozen Stage 2 PostgreSQL schema. It does
not implement the remainder of Part I.

## Dependencies and parser versions

Parser dependencies are locked in non-default dependency groups:

| Group | Direct dependency | Locked version | Runtime ownership |
| --- | --- | --- | --- |
| `parsing-docling` | `docling-slim[format-office]` | 2.135.0 | DOCX/XLSX only |
| `parsing-mineru` | `mineru` | 4.0.11 | PDF only, local `flash` tier |

The native Markdown and text parser versions are both `1` and add no parser
dependency. Ordinary `uv sync --frozen`, `make verify`, and regular CI exclude
both optional groups. Parser SDK imports are lazy and confined to provider
boundaries.

Install the complete Stage 3 parser runtime explicitly with:

```bash
uv sync --frozen --group parsing-docling --group parsing-mineru
```

## Supported source types and routing

Routing is deterministic and case-insensitive after canonical source
validation:

| Source | Required route | Fallback |
| --- | --- | --- |
| PDF | MinerU 4 local SDK, `tier="flash"`, all pages | none |
| DOCX | Docling Slim Office | none |
| XLSX | Docling Slim Office | none |
| Markdown | AtlasRAG native parser | none |
| text | AtlasRAG native parser | none |

Unsupported suffixes fail explicitly. MinerU does not take over native Office
documents, and PDF never falls back to Docling or another PDF library.

The native text loader accepts strict UTF-8 with an optional UTF-8 BOM,
normalizes CRLF/CR to LF, and emits ordered paragraphs. Empty, undecodable, or
control-bearing input becomes a stable failed parse result. The native
Markdown loader recognizes nested ATX headings, paragraphs, ordered/unordered
lists, fenced code, and pipe tables; unrecognized syntax remains paragraph
content instead of triggering semantic guesses.

## Source identity and path policy

`SourceFile` contains execution-only absolute path/root data plus canonical
relative identity. Before any parser is selected it requires:

- a nonblank canonical POSIX relative path with no absolute/Windows anchor,
  backslash, dot/traversal component, surrounding whitespace, NUL, or control
  character;
- a suffix mapping to exactly one supported source type;
- `file_name` equal to the final path component;
- an existing regular source file; and
- for a symlink, a resolved target that remains below the resolved Knowledge
  Directory.

Host paths never enter canonical persistence, identity hashes, or provenance.
Every stable structured identity below uses compact sorted-key UTF-8 JSON with
no NaN/Inf and lowercase SHA-256 hexadecimal output.

```text
document_id = "doc_" + SHA256({
  "relative_source_path": canonical_relative_source_path
})

content_hash = streaming SHA256(exact source bytes)

revision_id = "rev_" + SHA256({
  "document_id": document_id,
  "content_hash": content_hash,
  "pipeline_fingerprint": pipeline_fingerprint
})

element_id = "elem_" + SHA256({
  "revision_id": revision_id,
  "order": order,
  "element_type": element_type,
  "content": canonical_content,
  "section_path": section_path,
  "source_anchor": source_anchor,
  "table_data": table_data_or_null
})

chunk_content_hash = SHA256(canonical raw_content)

chunk_id = "chunk_" + SHA256({
  "document_id": document_id,
  "revision_id": revision_id,
  "chunk_type": chunk_type,
  "chunking_strategy": chunking_strategy,
  "provenance": typed_structural_lineage,
  "content_hash": chunk_content_hash
})
```

Source content hashing reads bytes in bounded 1 MiB blocks.
`CandidateBuildContext` contains only the validated `SourceFile`,
document/content/revision identities, and the pipeline fingerprint; it contains
no dependency, repository, State, trace, or provider object.

## Canonical parsed-document schema

All Stage 3 domain contracts are immutable Pydantic models, reject unknown
fields and invalid nested values, and support stable JSON serialization.

```text
DocumentContainer (schema_version = 1)
├── document_id
├── revision_id
├── source: CanonicalSource
│   ├── relative_source_path
│   ├── file_name
│   └── source_type
├── metadata: DocumentMetadata
│   ├── title?
│   ├── authors[]
│   ├── language?
│   ├── page_count?
│   └── sheet_names[]
├── parse_info: ParseInfo
└── elements: tuple[CanonicalElement, ...]
```

Elements must have unique IDs, strictly increasing order, and the same
document/revision identity as their container. A usable parse requires at
least one element; a failed parse carries none. Production providers emit a
contiguous zero-based order, while the canonical contract relies only on the
strictly increasing invariant.

### ParseInfo rules

```text
ParseInfo
├── outcome: SUCCESS | DEGRADED | FAILED
├── reliability: RELIABLE | PARTIAL | UNAVAILABLE
├── parser_name
├── parser_version
├── warning_codes[]
└── failure_code?
```

Allowed combinations are `SUCCESS/RELIABLE`, `SUCCESS/PARTIAL`,
`DEGRADED/PARTIAL`, and `FAILED/UNAVAILABLE`. `PARTIAL` requires a stable
warning code. `FAILED` requires a stable failure code and no elements; usable
results forbid a failure code. Tracebacks, vendor messages, temporary paths,
provider objects, and raw provider payloads are never retained.

### Element model

```text
CanonicalElement
├── element_id
├── document_id
├── revision_id
├── element_type: HEADING | PARAGRAPH | LIST | CODE | TABLE
├── order
├── content
├── section_path
├── source_anchor?
├── metadata: CanonicalElementMetadata
│   ├── heading_level?
│   ├── list_kind?
│   ├── list_depth?
│   └── code_language?
└── table_data?
```

Metadata is type-specific and contradictory combinations fail validation.
Unicode is normalized to NFC and newlines to LF; forbidden controls and lone
surrogates fail closed. Code retains whitespace semantics. Only `TABLE` may
carry `table_data`, and every `TABLE` must carry it.

### TableData normalization

```text
TableData
├── headers: tuple[str, ...]
├── rows: tuple[tuple[str, ...], ...]
├── caption?
├── sheet_name?
└── source_anchor?
```

The logical grid is nonempty and rectangular. Missing cells and covered parts
of merged cells normalize to empty strings; merged text is retained only in
the top-left logical cell. Provider-declared headers are preserved; an absent
header is represented by equal-width empty header cells rather than by
promoting a data row. Cells are normalized individually and rendered with a
deterministic escaped table renderer, never stored as a provider-native
Markdown blob. XLSX sheet names and cell anchors are retained and required to
agree.

## Parser normalization policies

- MinerU is invoked through its lazy local SDK boundary with `tier="flash"`
  and an empty full-page range. Only the result's exported dictionary enters a
  versioned v4 normalizer; unsupported or malformed shapes fail closed.
- Docling accepts only DOCX/XLSX, preserves reading order, extracts structured
  tables, keeps XLSX sheet/cell provenance, and prevents Docling objects from
  crossing the provider boundary. Unsupported formula/non-body situations are
  classified or rejected rather than silently omitted.
- Native loaders read the complete file outside database work and apply stable
  normalization; none silently truncates input.

The committed frozen MinerU v4 export tests only the normalizer. It is not and
cannot be counted as the mandatory real MinerU acceptance gate.

## Chunking baseline and algorithms

`DocumentAnalyzer` is pure and groups contiguous non-table elements only when
they share a section; tables retain their exact source position.
`ChunkRouter` is fixed to:

```text
text Parent -> STRUCTURE
text Child  -> RECURSIVE
table       -> TABLE
```

It never selects semantic or embedding-backed strategies in Stage 3.

The immutable baseline `ChunkingProfile` is:

```text
profile_schema_version  = 1
parent_max_chars        = 8_000
child_max_chars         = 1_500
table_slice_max_chars   = 8_000
child_overlap_chars     = 0
inter_element_separator = "\n\n"
algorithm_version       = lossless-structure-recursive-table-v7
chunker_behavior_version = chunker-v7
profile_fingerprint     = 46aba26b91b69ccd6ba414561b594b88f49df674077cf0ef8344b04039737fcc
```

Any output-affecting profile or algorithm change requires a corresponding
`PartIConfig.chunker_behavior_version` change; Stage 3 does not modify the
frozen `RuntimeConfig`.

### Parent policy

Within a section, ordered element ranges are split into bounded structural
atoms, then greedily grouped into Parents without reordering. Boundary
priority is paragraph, newline, sentence, whitespace, then Unicode scalar.
The inter-element separator is canonical renderer-owned whitespace. Every
source character is retained exactly once, and each `TEXT_PARENT` is emitted
immediately before its Children.

`TEXT_PARENT` is always `CONTEXT_ONLY`, uses `STRUCTURE`, has no `parent_id`,
and carries `TextParentProvenance` with ordered `ElementSpan` values.

### Child policy

Each Parent is recursively partitioned in order with zero overlap. Children
are nonempty, bounded, contiguous ranges whose union covers the Parent exactly
once, including renderer-owned join whitespace through the explicit Parent
range. Every Child has nonempty source-backed spans and exact half-open Parent
offsets.

`TEXT_CHILD` is always `RETRIEVABLE`, uses `RECURSIVE`, references a same-
document/same-revision Parent, and cannot exist without that Parent. Typed
element/order/character lineage—not string containment—proves ownership.

### Table policy

A table fitting `table_slice_max_chars` produces one `FULL` slice. A larger
table produces ordered `ROW_GROUP` slices. Every slice repeats all headers,
contains only complete rows, retains source element/order, exact half-open row
range, caption, source anchor, and sheet name, and collectively covers each
data row exactly once. A header, cell, or single rendered row that cannot fit
without loss fails closed; there is no truncation.

### ChunkRecord schema

```text
ChunkRecord
├── chunk_id
├── document_id
├── revision_id
├── chunk_type: TEXT_PARENT | TEXT_CHILD | TABLE
├── retrieval_role: CONTEXT_ONLY | RETRIEVABLE
├── chunking_strategy: STRUCTURE | RECURSIVE | TABLE
├── raw_content
├── content_hash
├── parent_id?
├── order
├── section_path
├── provenance: TextParentProvenance | TextChildProvenance | TableProvenance
└── table_slice?
```

Chunk order is contiguous and deterministic. `ChunkRecord` has no vector,
sparse text, model token, score, query/session, or benchmark field.

## Production validation

`ValidationResult` contains only `PASS | WARN | FAIL` and a canonically ordered,
unique tuple of stable reason codes. Only degraded/partial parse conditions are
warnings; every structural or lineage problem is a failure. Validation never
repairs data or stores free-form reasoning.

Parsed validation covers schema/unknown-field integrity, source and revision
identity, usable parse state, nonempty elements, unique deterministic IDs,
strict order, document/revision scope, canonical Unicode/JSON, section paths,
source anchors, and table invariants.

Chunk validation covers a nonempty exact set, unique IDs and contiguous order,
document/revision scope, exact role/strategy/type shapes, deterministic content
hashes and IDs, Parent existence/type/order, typed lineage bounds and
containment, complete Parent/Child coverage, table source/slice/row coverage,
repeated headers, JSON safety, and the absence of truncation state.

## Typed, versioned persistence mapping

No migration was added. Canonical business models remain distinct from frozen
Stage 2 `Stored*` persistence records. Every JSON field touched by the mapper
uses a strict immutable v1 envelope and is revalidated on read:

```text
StoredDocument.parse_metadata
└── DocumentParseMetadataEnvelopeV1
    ├── schema_version = 1
    ├── current: DocumentParseMetadataSlotV1?
    │   ├── revision_id
    │   ├── parse_info
    │   └── document_metadata
    └── building: DocumentParseMetadataSlotV1?

StoredElement.structured_content
└── ElementStructuredContentV1(schema_version, table_data?)

StoredElement.metadata
└── ElementPersistenceMetadataV1(schema_version, canonical_metadata)

StoredChunk.metadata
└── ChunkPersistenceMetadataV1(
      schema_version, order, retrieval_role, content_hash,
      provenance, table_slice?)

StoredChunk.strategy_metadata
└── ChunkStrategyMetadataV1(
      schema_version, chunking_strategy,
      chunker_behavior_version, profile_fingerprint)
```

Slot revision IDs must equal their corresponding document triples; current and
building revisions must differ. Pre-parse candidates forbid building metadata,
post-parse candidates require it, and `READY` requires current metadata with no
building triple/slot. Candidate work replaces only `building`; discard removes
only `building`; `current` is never overwritten. A future publish must
atomically transfer the building facts to current while updating triples, but
publish is deliberately not implemented here.

Canonical-to-Stored-to-canonical semantic round trips cover all element and
chunk kinds. Reads reject unknown schema versions/fields, missing fields,
contradictory first-class/JSON values, invalid ownership, and invalid nested
data with `CanonicalDataError`. Injected aware UTC timestamps are persistence
facts and never enter canonical identities.

## Candidate transaction semantics

Every capability receives a Unit-of-Work factory, opens one short transaction,
and explicitly commits once. Repositories remain unchanged and never
auto-commit. Parsing, source hashing, analysis, chunk construction, and
validation happen outside database transactions.

| Capability | Required state and atomic effect |
| --- | --- |
| `prepare_candidate` | Finds/creates the canonical document identity, preserves the complete current triple/slot, installs a distinct building triple, clears stale failure/building metadata, and sets `RECEIVED`. A conflicting existing candidate fails closed. |
| `mark_parsing` | Verifies source/building identity and applies only `RECEIVED -> PARSING`. |
| `persist_parsed` | From `PARSING` or same-revision `PARSED` retry, exact-set replaces candidate elements, writes the revision-bound building parse slot, clears failure state, and sets `PARSED` in one transaction. |
| `mark_chunking` | Verifies the candidate and applies only `PARSED -> CHUNKING`. |
| `persist_chunked` | From `CHUNKING` or same-revision `CHUNKED` retry, reloads and reconstructs the persisted `PARSED` element exact set, requires semantic equality with the caller document, validates chunks against that persisted authority, then exact-set replaces chunks and sets `CHUNKED` in one transaction. |
| `record_failure` | Explicitly records only classified PARSE failure from `PARSING` or CHUNK failure from `CHUNKING`, preserving both revision triples. No catch-all invokes it. |
| `discard_candidate` | Deletes only candidate elements/chunks, clears the building triple/slot and failure state, preserves current data, and restores `READY` when current exists or `RECEIVED` when it does not. |

Any repository write, status update, metadata update, exact-set replacement, or
persisted/caller mismatch failure rolls back the whole capability. A process
crash, cancellation, or unexpected exception is not automatically converted
to `FAILED`. Discard retains the stable document identity row even when a new
document has never been published.

### Source mutation

The pre-parse `content_hash` is authoritative. After the parser returns, the
orchestrator re-resolves and streams the source again. A mismatch or now-
invalid source raises the mutation route, invokes `discard_candidate`, persists
neither parsed nor chunked output, does not call `record_failure`, and never
changes the last-good current triple/metadata. The next sync is responsible for
the new bytes.

Stage 3 never mutates `current_revision_id`, current content/pipeline identity,
or the persisted Query Cache invalidation marker.

## Error boundary

The frozen shared error vocabulary is reused without semantic changes:

| Condition | Stage 3 behavior |
| --- | --- |
| missing parser dependency | `DependencyUnavailableError` |
| documented parser timeout | `DependencyTimeoutError` |
| malformed/unsupported provider export | `ProviderResponseError` |
| invalid canonical production output | `InvariantViolationError` |
| invalid typed persisted payload | `CanonicalDataError` |
| source hash changed during parsing | source-mutation discard route |

Providers may return a stable failed parse business result for classifiable
corrupt user input. Unclassified provider `Exception` instances propagate as
the fixed, caller-safe `ProviderResponseError`; vendor types, messages, paths,
payloads, and exception chains do not cross the adapter boundary. Cancellation
and other `BaseException` control signals remain uncaught. No provider or
service converts an exception into empty successful output, `None`, or an
automatic business failure.

## Command and CI separation

| Command | Scope |
| --- | --- |
| `make test-stage3` | Focused offline Stage 3 tests under the default locked dependencies. |
| `make test-stage3-integration` | PostgreSQL-only real candidate transactions plus real Docling DOCX/XLSX fixtures; installs only `parsing-docling`. |
| `make stage3-verify` | Ordinary `make verify`, focused Stage 3 tests, then the PostgreSQL/Docling integration gate. It deliberately excludes MinerU. |
| `make stage3-parser-smoke` | Separate mandatory local `simple.pdf -> real MinerU flash -> DocumentContainer` acceptance under `parsing-mineru`. |

The Stage 3 integration script starts only PostgreSQL and uses an EXIT trap to
stop the Compose project on success, failure, interruption, or cancellation.
Local cleanup uses `down --remove-orphans` without `-v`, preserving named user
volumes. The ephemeral CI cleanup may use `-v` for its run-unique project.

Regular CI remains parser/model/service-free. The Stage 3 Integration job
installs only the Docling group, starts only PostgreSQL, and never installs or
runs MinerU. The pre-existing Stage 2 Integration job and its complete service
gate remain separate. Every third-party action in both workflows is pinned to
an immutable release commit. A static regression test rejects floating action
tags in ordinary CI as well as integration CI.

## Verification evidence

### Review-driven Stage 2 selector correction

The first closeout `make stage2-verify` exposed a selector regression rather
than a product fallback: the historical Stage 2 `test-integration` target used
the global `integration` marker and therefore began collecting newly added
Stage 3 tests. It completed 41 tests, then reported six parser-dependency
failures because the Stage 2 environment correctly excludes Docling and MinerU.

The correction was developed test-first: `test-integration` now names exactly
the four original Stage 2 integration files (`test_compose.py`,
`test_milvus.py`, `test_postgres.py`, and `test_redis.py`). Contract tests prove
the single locked invocation contains those four files, no Stage 3 selector,
and no parser dependency group. Two independent reviews approved the fix. No
test was skipped, mocked, or made parser-aware to hide the boundary error.

### Review-driven parser exception-boundary correction

Final security review found that unclassified MinerU/Docling exceptions were
previously re-raised with their provider type and free-form message. That could
expose a local source path or vendor diagnostic even though documented
timeout/unavailable cases were already typed. New red tests injected provider
messages containing private paths through SDK loading, execution, result
serialization, and iteration. The adapters now map every provider `Exception`
to the frozen safe technical vocabulary, return only stable internal outcome
markers across Docling's worker-thread boundary, and raise a fresh exception
without the vendor exception chain. AtlasRAG canonical validation failures keep
their frozen `InvariantViolationError` type. Cancellation-like non-`Exception`
`BaseException` signals still propagate through both execution and Docling
input-rejection paths, and no failure becomes an empty result, `None`, or
business `FAILED` state. MinerU exception-code metadata is treated as untrusted:
only exact strings participate in frozen code classification, while raising or
non-string code values fail closed to a fresh `ProviderResponseError`.

### Successful local closeout gates

| Required gate | Result |
| --- | --- |
| `make test-stage3` | **PASS** — 529 passed in 4.79s |
| `make verify` | **PASS** — 2,273 passed in 72.42s, 47 integration tests deselected; Ruff format/lint, mypy, architecture, artifact, and test gates passed |
| `make stage2-verify` | **PASS** — offline 2,273 passed/47 deselected in 71.96s; all six services ready; migration at `20260928_0001`; the original 28 Stage 2 integration tests passed in 152.73s; automatic cleanup succeeded |
| `make stage3-verify` | **PASS** — offline 2,273 passed/47 deselected in 71.54s; focused Stage 3 529 passed in 4.80s; PostgreSQL/Docling integration 18 passed in 7.36s |
| `make stage3-parser-smoke` | **PASS** — real MinerU 4.0.11 flash produced a validated one-page `DocumentContainer`; 1 passed in 3.56s |

The Stage 3 integration result consists of 13 real PostgreSQL candidate tests
and five real Docling tests. Its dedicated PostgreSQL-only Compose project
left no containers or network after cleanup and preserved its named volume as
required for local runs.

Remaining finalization evidence:

| Required closeout gate | Status | Final evidence |
| --- | --- | --- |
| `uv run --frozen ruff format .` | **PASS** | 157 files left unchanged |
| `git diff --check` after final report update | **PASS** | Final staged diff check produced no output |
| final architecture review | **PASS** | Independent review: `ARCHITECTURE READY`; the advisory-lock concern was withdrawn after the complete-build ownership boundary was verified |
| final security review | **PASS** | Independent review: `SECURITY RELEASE READY`; immutable CI pins and parser exception redaction/type boundaries were verified |
| final Stage report completeness review | **PASS** | Independent review: `REPORT READY`; counts, DoD, deferrals, 16 review answers, and publication placeholders reconcile |

Final test summary fields:

| Field | Verified count |
| --- | --- |
| total | 2,320 = 2,273 offline + 47 integration |
| offline | 2,273 |
| integration | 47 = 28 Stage 2 + 13 Stage 3 PostgreSQL + 5 Docling + 1 MinerU |
| parser-real | 6 = 5 Docling + 1 MinerU |

### Real parser acceptance

| Required route | Status | Evidence |
| --- | --- | --- |
| MinerU PDF flash | **PASS** | SDK 4.0.11, canonical producer 4.0.11, 1 page, 7 elements: 1 HEADING, 1 PARAGRAPH, 3 LIST, 1 CODE, 1 TABLE |
| Docling DOCX | **PASS** | Included in the five real Docling tests within the 18-test Stage 3 integration result |
| Docling XLSX | **PASS** | Included in the five real Docling tests within the 18-test Stage 3 integration result |

The MinerU smoke reporter is constrained to SDK version, canonical producer
version, page count, total element count, and per-type counts. It suppresses
provider process output and reports no document content, payload, local path,
credential, or secret.

## Test coverage

The current suite covers:

- path traversal, canonical paths, symlink escape/inside-root behavior,
  streaming hashing, and all identity algorithms;
- immutable document/element/table/chunk contracts and JSON round trips;
- native Markdown/text fixtures, parser routing, lazy dependencies, frozen
  MinerU v4 normalization, and fake-boundary provider failure translation;
- deterministic repeated builds, lossless Parent/Child coverage, typed lineage,
  table FULL/ROW_GROUP coverage, repeated headers, and oversized row/cell
  rejection;
- parsed/chunk production validation and typed/versioned persistence
  round trips with fail-closed corrupt payloads;
- candidate state transitions, exact-set idempotency, rollback injection,
  persisted-source authority, current/building isolation, explicit failures,
  discard, and controlled source mutation using fake UoWs;
- real PostgreSQL atomicity/rollback/read-after-commit cases;
- real Docling fixture semantics; and
- the separate real MinerU smoke contract and safe-output boundary.

The final local collection contains 2,320 tests: 2,273 selected by the ordinary
offline gate and 47 integration tests kept behind explicit gates. The focused
Stage 3 offline command selected 529 tests. Real parser acceptance comprises
five Docling tests and one MinerU test.

## Stage 3 Definition of Done

Implementation, local gate checks, final reviews, and report finalization below
are backed by fresh closeout evidence. Commit, publication, and remote checks
intentionally remain open.

| Requirement | State | Evidence |
| --- | --- | --- |
| Stage 2 merge verified from actual `origin/main` | [x] | Base is Stage 2 PR #3 merge `7be89c84...`; it is the Stage 3 merge base. |
| Stage 0–2 regression gates pass | [x] | Final `make stage2-verify` passed its offline gate and original 28 integration tests. |
| `SourceFile` canonical validation | [x] | Immutable source contract and source tests. |
| path traversal protection | [x] | Canonical POSIX validation rejects traversal/anchors. |
| symlink escape policy | [x] | Resolved target must remain beneath resolved root. |
| deterministic `document_id` | [x] | Canonical relative-path JSON SHA-256. |
| streaming SHA-256 `content_hash` | [x] | Exact bytes, bounded 1 MiB reads. |
| deterministic `revision_id` | [x] | Document/content/pipeline canonical JSON SHA-256. |
| `CandidateBuildContext` | [x] | Immutable dependency-free context. |
| Source Mutation Guard | [x] | Post-parse rehash and discard route. |
| PDF routes exclusively to MinerU | [x] | Exact router and provider constraints. |
| DOCX routes to Docling | [x] | Exact router and provider constraints. |
| XLSX routes to Docling | [x] | Exact router and provider constraints. |
| native Markdown loader | [x] | Deterministic supported subset. |
| native text loader | [x] | Strict UTF-8/BOM and paragraph parsing. |
| `DocumentContainer` | [x] | Immutable schema v1. |
| `ParseInfo` | [x] | Closed outcome/reliability combinations. |
| HEADING/PARAGRAPH/LIST/CODE/TABLE elements | [x] | Closed canonical element enum/models. |
| `TableData` structural model | [x] | Rectangular full-table facts. |
| XLSX `sheet_name` retained | [x] | Canonical metadata/table/anchor coverage. |
| deterministic `element_id` | [x] | Canonical element-fact JSON SHA-256. |
| provider objects normalized away | [x] | Canonical boundary and architecture tests. |
| Parsed Production Validation | [x] | Stable fail-closed validation result. |
| `prepare_candidate` | [x] | Short explicit-commit capability. |
| `PARSING` transition | [x] | Exact `RECEIVED -> PARSING`. |
| atomic PARSED exact-set boundary | [x] | Elements + building metadata + status in one UoW. |
| PARSED rollback and idempotency | [x] | Fake and real PostgreSQL tests. |
| deterministic `DocumentAnalyzer` | [x] | Same-section ordered text runs/tables. |
| deterministic `ChunkRouter` | [x] | Fixed STRUCTURE/RECURSIVE/TABLE routes. |
| `TEXT_PARENT` | [x] | Context-only structural Parent. |
| `TEXT_CHILD` | [x] | Retrievable recursive Child. |
| `TABLE` chunk | [x] | Retrievable typed table slice. |
| Parent/Child lineage proof | [x] | Typed element spans and Parent offsets. |
| no retrievable `TEXT_PARENT` | [x] | Domain and production validation invariant. |
| no vector data in `ChunkRecord` | [x] | Contract/static scope tests. |
| no silent truncation | [x] | Lossless coverage or explicit failure. |
| table slicing and repeated headers | [x] | FULL/ROW_GROUP exact row coverage. |
| deterministic chunk hash/ID/order | [x] | Canonical content/lineage identities and repeated-build tests. |
| Chunk Production Validation | [x] | Cross-document/source/lineage/table checks. |
| `CHUNKING` transition | [x] | Exact `PARSED -> CHUNKING`. |
| atomic CHUNKED exact-set boundary | [x] | Persisted PARSED authority + chunks/status in one UoW. |
| CHUNKED rollback and idempotency | [x] | Fake and real PostgreSQL tests. |
| current R1 preserved while building R2 | [x] | Current triple/slot isolation tests. |
| new document remains unpublished | [x] | Stage 3 never assigns a current revision. |
| candidate discard | [x] | Deletes candidate sets and building slot only. |
| source mutation does not become FAILED | [x] | Controlled mutation orchestration tests. |
| explicit failure-recording capability | [x] | PARSE/CHUNK-only state capability. |
| crash/unexpected exception does not automatically become FAILED | [x] | No catch-all failure conversion. |
| real Docling DOCX smoke | [x] | Passed within final `make stage3-verify`. |
| real Docling XLSX smoke | [x] | Passed within final `make stage3-verify`. |
| real MinerU PDF flash smoke locally | [x] | Real MinerU 4.0.11 flash gate passed. |
| Markdown/text fixtures | [x] | Included in 529-test focused offline evidence. |
| repeated canonical-build determinism | [x] | Byte-identical model/ID tests. |
| no Milvus calls | [x] | Stage scope/import gates. |
| no Redis calls | [x] | Stage scope/import gates. |
| no embedding model | [x] | Stage scope/import gates. |
| no LangGraph graph | [x] | Stage scope/import gates. |
| final `make verify` | [x] | 2,273 passed, 47 deselected. |
| final `make stage2-verify` | [x] | Six services, migration, 28 original integration tests, and cleanup passed. |
| final `make stage3-verify` | [x] | Offline/focused suites plus 13 PostgreSQL and 5 Docling tests passed. |
| final `make stage3-parser-smoke` | [x] | One real MinerU smoke test passed. |
| Stage report complete | [x] | Independent final completeness review is `REPORT READY`. |
| Architecture deviations = NONE | [x] | Independent final architecture review is READY. |
| branch committed | [ ] | Selector/CI-pin fixes and final report/update are not yet committed. |
| branch pushed normally | [ ] | PENDING; no force push permitted. |
| remote CI green | [ ] | PENDING after push. |
| applicable Stage 3 Integration CI green | [ ] | PENDING after push. |

Stage 3 must not be changed to COMPLETE until every unchecked item is closed
with fresh evidence.

## Final architecture review questions

These answers describe the implemented contracts. Independent architecture
review is `ARCHITECTURE READY`, and independent security review is
`SECURITY RELEASE READY`.

| Question | Answer |
| --- | --- |
| Does the same source path produce the same `document_id`? | Yes. The canonical relative POSIX path is the sole document-ID payload. |
| Do the same source bytes and pipeline produce the same `revision_id`? | Yes, for the same document identity; exact bytes and pipeline fingerprint are canonical inputs. |
| Can parser runtime metadata accidentally change element IDs? | No. IDs use normalized stable canonical facts only; incidental provider/runtime metadata is discarded. |
| Can a source mutate during parsing and still become `PARSED`? | No. The authoritative post-parse rehash must match or the candidate is discarded. |
| Can `PARSED` contain a partial element set? | No. Exact-set replacement, metadata, and status commit atomically or roll back. |
| Can `CHUNKED` contain a partial chunk set? | No. Persisted PARSED authority is checked first; exact-set chunks and status commit atomically or roll back. |
| Can a Child point to another revision's Parent? | No. Canonical validation, mapper scope, and the Stage 2 same-revision database constraint reject it. |
| Can a Child exist without a Parent? | No. Domain/production validation and the database relationship reject it. |
| Can `TEXT_PARENT` accidentally become retrievable? | No. Its only valid role is `CONTEXT_ONLY`. |
| Can table slicing lose headers or rows? | No. Every slice repeats headers and row ranges cover all rows exactly once, otherwise validation fails. |
| Can a loader silently truncate content? | No. Accepted canonical content is retained; unsupported cases fail closed instead of truncating. |
| Can current R1 change before a later publish? | No. Stage 3 writes only the building candidate and contains no publish capability. |
| Can Stage 3 touch Milvus? | No. No Stage 3 domain/provider/service path imports or calls it. |
| Can Stage 3 touch Redis? | No. No Stage 3 domain/provider/service path imports or calls it. |
| Can parser/model download occur during ordinary `make verify`? | No. Optional groups are excluded and provider SDK loading is lazy. |
| Does any Stage 4/5 business graph exist accidentally? | No. No LangGraph build graph, directory sync, reconciliation, index, or publish behavior was added. |

## Known deferred work

The following are intentionally absent and are not Stage 3 defects:

- no representation construction;
- no Qwen embedding;
- no dense vectors;
- no Milvus write;
- no index candidate verification;
- no publication;
- no Query Cache invalidation-marker mutation from publish;
- no `document_build_subgraph` or top-level `index_graph`;
- no outer-runner acquisition of the existing Stage 2 advisory-lock lease;
  Stage 4 must acquire it outside the build subgraph and retain it across the
  complete prepare-through-publish document build, rather than releasing a
  partial Stage 3-only lock at `CHUNKED`;
- no Directory `SyncPlan`;
- no Startup Reconciliation;
- no directory-wide scan/deletion/convergence;
- no runtime maintenance/serving barrier;
- no Part II; and
- no benchmark harness or benchmark-aware production field.

These belong to Stage 4, Stage 5+, or their explicitly assigned later stages.

## Architecture deviations

**NONE**

The implementation uses explicit typed/versioned mappings into the frozen
Stage 2 records, introduces no database migration, keeps provider SDKs behind
lazy adapters, stores no dependency in State, adds no graph, preserves
repository commit ownership in the Unit of Work, and does not import tests or
benchmarks from production.

## Git history and publication

Committed Stage 3 work through Task 12:

```text
1c17137 docs(stage-03): record approved parse canonical design
d08b97a docs(stage-03): add implementation plan
55635f8 build(stage-03): isolate parser dependencies and boundaries
a3246ff feat(stage-03): add secure source identity
657b809 feat(stage-03): define canonical parsed document
7e45bdc feat(stage-03): parse native text and markdown
3a28dc7 feat(stage-03): normalize MinerU PDF output
52799cd feat(stage-03): parse Office sources with Docling
8837cc5 feat(stage-03): build deterministic canonical chunks
0d0f7c8 feat(stage-03): validate canonical production output
df0a177 feat(stage-03): map canonical records to frozen storage
35c5767 docs(stage-03): approve parse metadata ownership amendment
f0c8d81 feat(stage-03): bind parse metadata to revision slots
f54b2bd feat(stage-03): persist candidate parse and chunks
42c4635 test(stage-03): verify PostgreSQL candidate transactions
60e36e3 ci(stage-03): add parser and integration gates
```

The review-driven exact Stage 2 selector fix, immutable ordinary-CI action
pins, parser exception-boundary correction, and final documentation are present
in the verified working tree but remain uncommitted pending the final closeout
commit.

Publication placeholders:

- Final documentation commit: **PENDING**
- Final current HEAD: **PENDING**
- Normal push to `origin/stage/03-part-i-parse-canonical-build`:
  **PENDING**
- Force push: **NOT USED; PROHIBITED**
- Remote CI: **PENDING**
- Remote Integration CI: **PENDING**
- Pull request number/status: **PENDING**
- Merge into `main`: **NOT PERFORMED; MUST REMAIN MANUAL**

## Next

Stage 4 — Part I: Index & Publish — **NOT STARTED**.

Do not begin representation construction, embeddings, Milvus candidate writes,
index verification, publication, directory synchronization, reconciliation,
or any Part II work as part of Stage 3 closeout.
