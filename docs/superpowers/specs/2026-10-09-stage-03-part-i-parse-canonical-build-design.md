# AtlasRAG Stage 3 Part I Parse & Canonical Build Design

**Date:** 2026-10-09

**Status:** APPROVED

**Base:** `origin/main@7be89c84ae4fc5766914cf987c5c625cd82c9319`

**Branch:** `stage/03-part-i-parse-canonical-build`

## 1. Purpose and authority

Stage 3 turns one supported source file into deterministic canonical elements
and Parent/Child/Table chunks, then persists the `PARSED` and `CHUNKED`
boundaries atomically in PostgreSQL.

The authority order is:

1. `docs/architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md`
2. frozen Stage 1 contracts
3. frozen Stage 2 persistence contracts
4. the Stage 3 execution specification
5. this approved implementation design

The implementation must stop and report an architecture conflict if the
existing Stage 2 schema cannot losslessly round-trip a frozen canonical
invariant. It must not add a migration or change a frozen contract as an
unreviewed workaround.

## 2. Scope boundary

Stage 3 implements:

- canonical source and candidate identity;
- streaming source hashing and a source-mutation guard;
- deterministic routing for PDF, DOCX, XLSX, Markdown, and text;
- MinerU, Docling, Markdown, and text parser providers;
- immutable canonical parsed-document, element, table, analysis, lineage, and
  chunk models;
- deterministic validation, analysis, Parent grouping, Child splitting, and
  table slicing;
- typed, versioned canonical-to-persistence mappings;
- short Unit-of-Work-owned candidate transitions through `CHUNKED`;
- candidate discard and explicit PARSE/CHUNK business-failure recording; and
- offline, parser, persistence, integration, determinism, and architecture
  verification.

Stage 3 does not implement representation construction, embeddings, Milvus
writes or verification, publication, query-cache invalidation, directory sync,
startup reconciliation, LangGraph graphs, Part II, or benchmarks.

## 3. Selected architecture

The approved persistence strategy is Option 1:

```text
parser provider
    -> AtlasRAG canonical models
    -> explicit typed/versioned mapper
    -> frozen Stage 2 Stored* records
    -> frozen Stage 2 repositories/UoW
```

There is no provider-neutral intermediate AST in addition to AtlasRAG's
canonical models, and there is no Stage 3 database migration.

`DocumentContainer`, `CanonicalElement`, and `ChunkRecord` own Stage 3 business
semantics. `StoredDocument`, `StoredElement`, and `StoredChunk` remain
persistence representations only. Neither representation aliases, subclasses,
nor replaces the other.

## 4. Package topology and dependencies

The incremental package layout is:

```text
src/atlasrag/
  domain/ingestion/
    enums.py
    source.py
    document.py
    elements.py
    chunks.py
    validation.py
  providers/parsing/
    base.py
    routing.py
    mineru.py
    mineru_normalizer.py
    docling.py
    markdown.py
    text.py
  services/ingestion/
    identity.py
    source_guard.py
    analysis.py
    chunking.py
    validation.py
    mapping.py
    candidate.py
    persistence.py
```

Files may be combined where that improves cohesion, but dependency direction
must remain:

```text
service -> parser protocol
service -> Stage 2 repositories/UoW
provider -> parser SDK
provider -> canonical ingestion domain
mapper -> canonical ingestion domain + Stored*
```

Forbidden directions include domain-to-parser-SDK, provider-to-repository,
repository-to-parser, Stage 3-to-LangGraph, and Stage 3-to-Redis/Milvus.

Two non-default locked dependency groups are used:

```text
parsing-docling -> docling-slim[format-office]
parsing-mineru  -> MinerU 4 base package
```

Ordinary `uv sync`, `make verify`, and regular CI exclude both groups. Provider
SDK imports and initialization are lazy. The lock contains both complete
environments, and the Stage report records exact resolved versions. DOCX/XLSX
remain Docling-only even though MinerU supports other source types.

The native Markdown parser is implemented locally for the frozen Stage 3
subset, so no Markdown dependency is added unless implementation evidence
shows that a small dependency is necessary.

## 5. Source models and path security

`SourceFile` is immutable execution data:

```text
absolute_path
knowledge_directory
relative_source_path
file_name
source_type
```

The resolved absolute path and resolved knowledge root are execution-only.
They never enter canonical persistence, IDs, or provenance.

`CanonicalSource` is the serializable source projection stored inside a
`DocumentContainer`:

```text
relative_source_path
file_name
source_type
```

Source construction performs all path checks before parser selection:

- the relative path is canonical POSIX text;
- it is not absolute, blank, dotted, traversal-bearing, NUL-bearing, or
  control-bearing;
- its suffix maps case-insensitively to exactly one supported `SourceType`;
- `file_name` equals the final canonical path component;
- the resolved source exists as a regular file; and
- a symlink is accepted only when its resolved target remains below the
  resolved knowledge directory.

Document content and parser metadata are data. They cannot select modules,
commands, configuration paths, or arbitrary files.

## 6. Identity algorithms

All identities use the existing canonical JSON SHA-256 primitive and lowercase
hex output. Prefixes are part of the public identity shape.

```text
document_id = "doc_" + SHA256({
  "relative_source_path": canonical_relative_source_path
})

content_hash = streaming SHA-256(source bytes)

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

chunk_content_hash = SHA256(canonical raw_content or table rendering)

chunk_id = "chunk_" + SHA256({
  "document_id": document_id,
  "revision_id": revision_id,
  "chunk_type": chunk_type,
  "chunking_strategy": chunking_strategy,
  "provenance": typed_structural_lineage,
  "content_hash": chunk_content_hash
})
```

Source hashing is streaming and bounded-memory. `CandidateBuildContext` contains
only `SourceFile`, `document_id`, `content_hash`, supplied
`pipeline_fingerprint`, and `revision_id`. It contains no dependency, State,
repository, trace, or provider object.

## 7. Canonical parse contracts

All canonical contracts extend the shared frozen Pydantic base, forbid unknown
fields, validate nested instances, reject non-finite values, and serialize to
stable JSON.

### 7.1 Parse information

```text
ParseInfo
  outcome: SUCCESS | DEGRADED | FAILED
  reliability: RELIABLE | PARTIAL | UNAVAILABLE
  parser_name
  parser_version
  warning_codes[]
  failure_code?
```

Allowed usable combinations are `SUCCESS/RELIABLE`, `SUCCESS/PARTIAL`, and
`DEGRADED/PARTIAL`. `FAILED` requires `UNAVAILABLE`, carries no elements, and
cannot cross the `PARSED` boundary. Tracebacks and vendor messages never enter
`ParseInfo`.

An empty source and an undecodable native text source produce a stable failed
parse result, not an empty successful document.

### 7.2 Document and elements

```text
DocumentContainer
  schema_version: 1
  document_id
  revision_id
  source: CanonicalSource
  metadata: DocumentMetadata
  parse_info: ParseInfo
  elements: tuple[CanonicalElement, ...]

CanonicalElement
  element_id
  document_id
  revision_id
  element_type: HEADING | PARAGRAPH | LIST | CODE | TABLE
  order
  content
  section_path
  source_anchor?
  metadata: CanonicalElementMetadata
  table_data?
```

`CanonicalElementMetadata` is a typed model containing only stable AtlasRAG
facts such as heading level, list kind, and code language. Provider timestamps,
temporary paths, random IDs, object dumps, and other incidental fields are
discarded.

Orders are nonnegative, unique, and strictly increasing. Providers generate a
contiguous sequence starting at zero, while validation relies on the frozen
strictly-increasing invariant rather than incidental collection ordering.

`TABLE` requires `table_data`; every other type forbids it.

### 7.3 Tables

```text
TableData
  headers: tuple[str, ...]
  rows: tuple[tuple[str, ...], ...]
  caption?
  sheet_name?
  source_anchor?
```

The logical grid is rectangular. Missing cells and covered portions of merged
cells become empty strings. A merged cell's text is retained only in its
top-left logical cell, avoiding invented duplication. Provider-declared header
cells determine headers; if no header exists, an equal-width tuple of empty
header cells represents that fact without promoting a data row.

Text normalization uses Unicode NFC and `\n` line endings. Ordinary prose may
normalize deterministic line structure but retains punctuation and case. Code
preserves whitespace semantics. Table cells are normalized individually and
never reduced to a provider Markdown blob.

## 8. Parser boundary and routing

`DocumentParser` is a minimal async protocol returning a
`DocumentContainer`. Parser providers own SDK invocation, extraction,
normalization, version discovery, and explicit vendor error translation. They
own no database or candidate status logic.

Routing is exact and case-insensitive:

```text
.pdf  -> MinerUParser
.docx -> DoclingParser
.xlsx -> DoclingParser
.md   -> MarkdownParser
.txt  -> TextParser
```

Unsupported suffixes fail explicitly and are never treated as text.

### 8.1 MinerU

The production PDF route invokes the MinerU 4 local Python SDK with the
simplest stable local `flash` tier and full-page selection. It never falls back
to PyPDF or Docling. A versioned normalizer consumes `ParseResult.to_dict()`
data and fails closed on unsupported schema shape/version.

Normal CI tests the normalizer against a small, license-safe export produced
from the repository fixture. Local completion additionally runs:

```text
simple.pdf -> real MinerU flash -> DocumentContainer
```

The real smoke cannot be replaced by a fake or frozen output.

### 8.2 Docling

The production Office route uses `docling-slim[format-office]` and permits only
DOCX/XLSX. It iterates Docling content in reading order and maps headings,
paragraphs, lists, code, and tables to canonical elements. Table grids are
extracted structurally; XLSX sheet names are preserved. Docling objects never
cross the provider boundary.

Real tiny DOCX and XLSX fixtures execute in Stage 3 integration CI.

### 8.3 Native text and Markdown

Text supports UTF-8, optional UTF-8 BOM, Unicode, CRLF/CR normalization, and
explicit invalid-UTF-8 failure.

The Markdown subset recognizes nested ATX headings, paragraphs, ordered and
unordered lists, fenced code, and pipe tables. It preserves section paths and
source order deterministically. Unsupported Markdown syntax remains paragraph
content rather than triggering semantic guesses.

## 9. Source mutation guard

The candidate's `content_hash` is the pre-parse authority. Parsing runs outside
all database transactions. The guard streams the source again after parsing.

```text
pre_hash == post_hash -> parsed output may be validated
pre_hash != post_hash -> SOURCE_MUTATED
```

On mutation, the coordinator explicitly invokes `discard_candidate`. It does
not persist parsed/chunked output and does not invoke `record_failure`.
Deterministic tests mutate the file through a controlled parser callback; they
do not depend on timing races.

## 10. Analysis, chunk routing, and profile

`DocumentAnalyzer` is pure and deterministic. It identifies section-scoped
ordered text ranges, element features, and table positions. `ChunkRouter` is a
separate pure component selecting only:

```text
STRUCTURE -> text Parent construction
RECURSIVE -> text Child construction
TABLE     -> table slicing
```

It never selects `SEMANTIC` or `STRUCTURE_SEMANTIC_CHILD` in Stage 3.

`ChunkingProfile` is an immutable internal contract. It centralizes all
output-affecting limits and the canonical inter-element join policy. The
initial profile uses:

```text
profile_schema_version = 1
parent_max_chars = 8_000
child_max_chars = 1_500
table_slice_max_chars = 8_000
child_overlap_chars = 0
```

Changing any output-affecting profile value or algorithm requires a
corresponding bump to `PartIConfig.chunker_behavior_version`. Tests and the
Stage report make this requirement explicit; Stage 3 does not add fields to
the frozen `RuntimeConfig`.

## 11. Typed structural lineage

String containment is not a lineage proof. Canonical lineage is represented by
immutable typed models:

```text
ElementSpan
  element_id
  element_order
  char_start       # offset in canonical element content
  char_end         # exclusive offset

TextParentProvenance
  source_spans: nonempty ordered tuple[ElementSpan, ...]

TextChildProvenance
  source_spans: nonempty ordered tuple[ElementSpan, ...]
  parent_char_start
  parent_char_end

TableProvenance
  source_element_id
  source_element_order
  row_start
  row_end           # exclusive data-row range
  sheet_name?
```

Ranges are nonempty, ordered, in bounds, and non-overlapping except where an
explicit future overlap policy would permit it. A Child's parent character
range and every source span must be contained in its referenced Parent. Parent
and Child must share document and revision identity.

## 12. Chunk construction

```text
ChunkRecord
  chunk_id
  document_id
  revision_id
  chunk_type: TEXT_PARENT | TEXT_CHILD | TABLE
  retrieval_role: CONTEXT_ONLY | RETRIEVABLE
  chunking_strategy: STRUCTURE | RECURSIVE | TABLE
  raw_content
  content_hash
  parent_id?
  order
  section_path
  provenance
  table_slice?
```

Cross-invariants are exact:

- `TEXT_PARENT` is `CONTEXT_ONLY`, uses `STRUCTURE`, has no parent, and has
  `TextParentProvenance`;
- `TEXT_CHILD` is `RETRIEVABLE`, uses `RECURSIVE`, requires a Parent, and has
  `TextChildProvenance`;
- `TABLE` is `RETRIEVABLE`, uses `TABLE`, has no parent, and requires a
  `TableSlice`; and
- no chunk contains vectors, sparse representations, model tokens, scores, or
  Milvus fields.

### 12.1 Parent and Child text

Text elements are processed in source order within one section. A lossless
recursive splitter first creates bounded structural atoms when an individual
element exceeds the Parent limit. Boundary priority is paragraph, newline,
sentence boundary, whitespace, then Unicode scalar boundary. Split positions
retain every character exactly once; no trimming or truncation occurs.

Contiguous atoms in one section form Parents up to `parent_max_chars`. Parent
content is the complete canonical rendering of its assigned source range.
Each Parent is then split by the same order-preserving policy into zero-overlap
Children up to `child_max_chars`. Validation reconstructs Parent coverage from
the ordered Child character ranges, allowing only canonical join whitespace
owned by the Parent renderer.

Each Parent is emitted immediately before its Children. Tables remain in source
order relative to text groups. Final `order` values are a deterministic
contiguous sequence independent of IDs, dictionaries, sets, or filesystem
enumeration.

### 12.2 Tables

```text
TableSlice
  mode: FULL | ROW_GROUP
  source_element_id
  headers
  rows
  row_start
  row_end
  caption?
  sheet_name?
  source_anchor?
```

A complete rendered table at or below `table_slice_max_chars` uses `FULL`.
Larger tables use ordered `ROW_GROUP` slices. Every slice repeats all headers
and contains complete rows. Row ranges cover every source data row exactly
once and preserve order. A header, cell, or single rendered row that cannot fit
without data loss causes validation failure.

## 13. Production validation

`ValidationResult` is immutable and contains only:

```text
outcome: PASS | WARN | FAIL
reason_codes: ordered unique tuple[stable code, ...]
```

It contains no free-form reasoning, benchmark score, or provider payload.
Validation is deterministic and fail-closed.

Parsed validation covers identity consistency, supported source type, usable
`ParseInfo`, nonempty usable documents, unique IDs, element order, Unicode/JSON
safety, section paths, anchors, and table cross-invariants.

Chunk validation covers unique IDs/order, document/revision consistency,
roles, strategies, content hashes, Parent references, typed lineage bounds and
containment, table coverage, repeated headers, nonempty content, JSON safety,
and absence of truncation state.

Duplicate IDs, bad content hashes, cross-revision lineage, broken Parent
references, malformed typed persistence payloads, or invalid tables are always
`FAIL`, never warnings.

## 14. Typed, versioned persistence mapping

JSON storage is not an untyped escape hatch. Every JSON column touched by the
Stage 3 mapper has an immutable versioned Pydantic contract and is revalidated
on read.

### 14.1 Document parse metadata

```text
DocumentParseMetadataSlotV1
  revision_id
  parse_info: ParseInfo
  document_metadata: DocumentMetadata

DocumentParseMetadataEnvelopeV1
  schema_version: 1
  current: DocumentParseMetadataSlotV1?
  building: DocumentParseMetadataSlotV1?
```

The envelope maps to `StoredDocument.parse_metadata` without a migration. A
present slot is bound to exactly one revision: its `revision_id` must match the
corresponding current or building revision triple on the document row. A slot
is absent when that revision has no persisted parse result yet. Unknown fields,
unknown versions, a slot without its corresponding triple, a revision mismatch,
or using one revision as both current and building fail closed.

Candidate work only writes the `building` slot and never overwrites `current`.
Discard removes only `building`. A future publish operation atomically promotes
`building` to `current` while updating the document triples; it does not copy
metadata between unrelated revisions.

### 14.2 Element payloads

```text
ElementStructuredContentV1
  schema_version: 1
  table_data: TableData?

ElementPersistenceMetadataV1
  schema_version: 1
  canonical_metadata: CanonicalElementMetadata
```

They map to `StoredElement.structured_content` and `StoredElement.metadata`.
The TABLE/non-TABLE invariant is validated both before write and after read.

### 14.3 Chunk payloads

```text
ChunkPersistenceMetadataV1
  schema_version: 1
  order
  retrieval_role
  content_hash
  provenance
  table_slice?

ChunkStrategyMetadataV1
  schema_version: 1
  chunking_strategy
  chunker_behavior_version
  profile_fingerprint
```

They map to `StoredChunk.metadata` and `StoredChunk.strategy_metadata`.
Existing first-class Stage 2 columns continue to store chunk ID, document ID,
revision ID, type, parent ID, raw content, section path, source anchor, and
sheet name.

Mapping is always:

```text
ChunkRecord
  -> ChunkPersistenceMetadataV1 / ChunkStrategyMetadataV1
  -> JSON-mode typed serialization
  -> StoredChunk
  -> typed model validation
  -> ChunkRecord
```

Canonical-to-Stored-to-canonical semantic round trips are required for every
element and chunk kind. Reads sort chunks by the validated embedded canonical
`order`, not the Stage 2 repository's identity order. Unknown schema versions,
unknown fields, missing fields, contradictory first-class/JSON values, and
invalid nested data raise a canonical-data failure.

Timestamps are persistence facts supplied through an injected UTC clock. They
do not enter canonical models or identities.

## 15. Candidate persistence services

Services receive a Unit-of-Work factory; repositories remain unchanged and
never commit. Every transition opens one short UoW and explicitly commits.
Parser, hashing, analysis, chunking, and validation run outside transactions.

### 15.1 Prepare and parse

`prepare_candidate` finds or creates the document by relative path, verifies
the deterministic document ID, preserves the current triple, installs the
building triple, updates the observed hash, sets `RECEIVED`, and clears stale
failure state. A different existing building revision fails closed. Stage 3
does not adopt or reconcile it. The building revision must differ from the
current revision. Preparation preserves the `current` parse-metadata slot and
clears the `building` slot for the new attempt.

`mark_parsing` verifies the candidate and transitions `RECEIVED -> PARSING`.

`persist_parsed` accepts only validated usable canonical output. In one
transaction it verifies the building triple, exact-set replaces candidate
elements, writes the revision-bound `building` slot in
`DocumentParseMetadataEnvelopeV1` without changing `current`, clears failure
state, and sets `PARSED`. A retry for the same `PARSED` candidate performs the
same exact-set replacement. Any failure rolls back elements, metadata, and
status together.

### 15.2 Chunking

`mark_chunking` verifies the candidate and transitions `PARSED -> CHUNKING`.

`persist_chunked` does not trust a caller-only `DocumentContainer`. Before any
chunk write, it reads and reconstructs the persisted `PARSED` exact-set,
requires complete semantic equality with the caller document, and validates
the chunks against that persisted canonical source. In one transaction it then
exact-set replaces candidate chunks, clears failure state, and sets `CHUNKED`.
A retry for the same `CHUNKED` candidate replaces the exact set. A mismatch or
any write failure rolls back and fails closed.

### 15.3 Failure and discard

`record_failure` is an explicit capability for controlled PARSE or CHUNK
business failures. It preserves the current and building triples, sets
`FAILED`, and sets the corresponding `failed_stage`. No catch-all wrapper calls
it for unexpected exceptions, cancellation, or process interruption.

`discard_candidate` verifies the candidate and atomically deletes its elements
and chunks, clears the building triple and failure state, and preserves the
current triple. It removes only the envelope's `building` slot; `current` is
preserved unchanged. The approved restored status is:

```text
current revision exists -> READY
no current revision      -> RECEIVED
```

The empty identity row for a never-published document is retained. Source
mutation always uses discard and never becomes `FAILED`.

No Stage 3 service changes `current_revision_id`, the current content hash, the
current pipeline fingerprint, or the query-cache invalidation marker.

## 16. Error boundary

The frozen Stage 1 error vocabulary is reused without semantic changes:

```text
missing parser dependency -> DependencyUnavailableError
documented parser timeout -> DependencyTimeoutError
malformed provider output -> ProviderResponseError
invalid canonical output  -> InvariantViolationError
invalid persisted payload -> CanonicalDataError
source hash changed        -> SourceMutationError / discard route
```

Corrupt user documents are represented by a failed parse business result when
the provider can classify them. Unclassified provider `Exception` instances
propagate as the fixed, caller-safe `ProviderResponseError`; vendor exception
types, messages, paths, payloads, and exception chains do not cross the adapter
boundary. AtlasRAG canonical validation failures retain the frozen
`InvariantViolationError` type. Cancellation and other non-`Exception`
`BaseException` control signals are not caught, including when exposed as an
input-rejection cause. Provider exception classification metadata is itself
untrusted: MinerU error codes are accepted only as exact strings and malformed
access fails closed to `ProviderResponseError`. No exception is converted into
empty successful output, `None`, or an automatic business `FAILED` state.

## 17. Test and CI design

Ordinary `make verify` remains network-free, Docker-free, model-free, and
parser-SDK-free. It covers:

- model invariants and JSON round trips;
- path traversal and symlink escape policy;
- all identity algorithms and streaming hash behavior;
- native text and Markdown fixtures;
- frozen MinerU-output normalization;
- parser routing and lazy imports;
- deterministic analysis and chunking;
- Parent/Child typed lineage and lossless coverage;
- FULL/ROW_GROUP tables, repeated headers, and oversized-row failure;
- typed persistence payload round trips and fail-closed schema versions;
- candidate services with fake UoWs;
- source-mutation orchestration with a controlled parser; and
- Stage 3 architecture and forbidden-field checks.

`make test-stage3-integration` starts PostgreSQL only and covers real candidate
transactions plus real Docling DOCX/XLSX fixtures. It does not require Redis,
Milvus, SearXNG, or MinerU.

`make stage3-parser-smoke` installs/runs the MinerU group explicitly and parses
the tiny real PDF through MinerU flash. It is a local completion gate.

`make stage3-verify` composes the offline gate, PostgreSQL Stage 3 integration,
real Docling fixture smoke, and native parser smoke. The separate MinerU smoke
remains mandatory at closeout.

Stage 2's complete `make stage2-verify` remains a closeout regression gate.
Existing CI and Integration workflows stay valid. A Stage 3 integration job
installs only `parsing-docling`, starts only PostgreSQL, and runs the Stage 3
integration command. It never downloads MinerU models.

## 18. Architecture checks

Static checks enforce:

- ingestion domain cannot import parser SDKs, repositories, providers,
  LangGraph, tests, or benchmarks;
- parser providers cannot import repositories, Redis, Milvus, graphs, tests,
  or benchmarks;
- Stage 3 services cannot import Redis, Milvus, LangGraph, tests, or
  benchmarks;
- parser SDK imports remain confined to parsing provider modules and lazy
  runtime boundaries;
- canonical models contain no provider-native type, vector, score, query,
  session, citation, benchmark, or experiment field;
- repositories still contain no commit outside the UoW; and
- no new migration is introduced.

## 19. Implementation sequence

Implementation proceeds with red-green-refactor checkpoints in this order:

1. dependency and architecture gates;
2. source models, path validation, and identities;
3. canonical parse/element/table models;
4. native text and Markdown providers;
5. MinerU normalizer and lazy adapter;
6. Docling adapter and real fixtures;
7. analysis, profile, lineage, Parent/Child/Table chunking;
8. parsed/chunk production validation;
9. typed/versioned persistence mappings;
10. candidate transitions and PostgreSQL integration;
11. commands, CI separation, documentation, full verification, publication.

Each module begins with focused failing tests. Production code is added only
to satisfy the active test slice. No Stage 4 implementation is started.

## 20. Stop conditions

Work stops for a human decision if:

- Stage 2 is no longer an ancestor of `origin/main`;
- a frozen Stage 1 or Stage 2 contract must change;
- typed semantic round-trip cannot be represented in current Stage 2 fields;
- the required PDF path cannot use real MinerU;
- DOCX/XLSX cannot use real Docling;
- real local MinerU PDF smoke cannot pass;
- a required implementation pulls embeddings, Milvus, Redis, or LangGraph into
  Stage 3; or
- regression, security, secret, or publication gates cannot be made green
  within the approved scope.

## 21. Approved decisions summary

```text
Persistence strategy: explicit typed/versioned mappers
Database migration: none
Additional intermediate AST: none
PDF: MinerU 4 local flash
DOCX/XLSX: docling-slim[format-office]
Default parser dependency installation: excluded
Parent: STRUCTURE, same-section contiguous source ranges
Child: deterministic lossless zero-overlap RECURSIVE
Table: FULL / ROW_GROUP with repeated headers
Lineage: typed element and character spans
Source mutation: discard, never FAILED
Publication: deferred beyond Stage 3
Architecture deviation: none
```
