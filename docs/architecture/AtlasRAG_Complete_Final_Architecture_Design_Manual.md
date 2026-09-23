# AtlasRAG — Complete Final Architecture Design Manual

> **Project:** AtlasRAG — A Production-Oriented Multi-Source RAG System  
> **Document status:** Final frozen architecture baseline for AtlasRAG v1 (Part I–IV)  
> **Scope:** Architecture and contracts only; Stage breakdown and implementation details are intentionally deferred to a separate implementation-design conversation.  
> **Primary stack direction:** Python, LangGraph, PostgreSQL, Milvus, Redis, DeepSeek API, SearXNG MCP, MinerU, Docling, FlagEmbedding, Qwen3-Embedding, pytest, Vectara Open RAGBench, optional pinned secondary evaluation tooling  
> **Local deployment target:** Windows + WSL2 Ubuntu, RTX 3070 8GB, LangGraph local server, Docker Compose for infrastructure  
> **Core project emphasis:** Part I and Part II are the technical core. Part III proves correctness/quality. Part IV proves the system can be run and used locally.

---

# 1. Project Positioning

AtlasRAG is a domain-agnostic multi-source knowledge-augmentation QA system.

Its source priority is:

1. **Managed local knowledge directory / KB** — primary source.
2. **External Web search** — corrective or supplementary only.

The high-level flow is:

```text
Knowledge Directory
        ↓
Part I — Knowledge Preparation & Index Construction
        ↓
PostgreSQL + Milvus
        ↓
Part II — Retrieval-Augmented Generation
        ↓
Evidence-grounded Answer + Citation + Trace
        ↓
Part III — External Verification & Benchmark Harness
        ↓
Part IV — Local Deployment & Operations
```

The project is intentionally not designed as a generic multi-agent platform, GraphRAG system, evaluation platform, or distributed GPU serving system.

---

# 2. Global Architecture Principles

## 2.1 Evidence First

The core runtime abstraction is:

```text
Query
→ Evidence
→ Answer
```

Generation is not allowed to retrieve, browse, or mutate knowledge on its own.

---

## 2.2 Local First, Web Corrective

The managed local KB is the preferred source.

Web search is used only when local evidence is not sufficient or when the bounded corrective local retry still fails.

---

## 2.3 Experiment Isolation Rule — FROZEN

> Production modules provide reusable capabilities and deterministic policies. Experiments and benchmarks are external consumers of those capabilities. Experiment metadata, benchmark IDs, candidate strategy sets, evaluation databases, qrels and metric objects must never enter production domain models or online execution paths.

Dependency direction:

```text
tests/   ─┐
evals/   ─┼──→ src/atlasrag/
tools/   ─┘
```

Never:

```text
src/atlasrag/
    ↓
tests / evals / benchmark code
```

Production objects must not contain fields such as:

```text
experiment_id
benchmark_case_id
qrels_id
benchmark_mode
candidate_strategy_set
leaderboard_score
```

---

## 2.4 Minimal State / Explicit Ownership

Every piece of state must have an owner.

```text
Knowledge Directory
= Knowledge Authority

PostgreSQL
= Canonical Runtime Store

Milvus
= Derived Retrieval Index

Redis
= Disposable Runtime Cache

LangGraph State
= Current request / current execution state

Trace
= Historical explanation of how a request executed
```

---

## 2.5 Decision Plane vs Execution Plane

Policy decisions and low-level execution should remain separable.

Examples:

```text
ChunkRouter
→ decides strategy

Chunker
→ executes strategy
```

```text
Local Evidence Sufficiency Strategy
→ produces normalized local evidence status

RagCore Corrective Router
→ decides ANSWER / RETRY_LOCAL / WEB

Retriever / WebSearchProvider
→ executes retrieval
```

---

## 2.6 No Free-Form Chain-of-Thought Persistence

AtlasRAG may persist:

```text
status
decision
reason_code
score
rank
attempt
latency
error_code
IDs
```

It does not persist hidden model reasoning or free-form Chain-of-Thought.

---

## 2.7 Quality / Latency / Cost / Complexity

Every optional component must justify itself across:

```text
quality
latency
token cost
memory cost
operational complexity
```

A component may be disabled even if it improves quality slightly when its production cost is not worthwhile.

---

## 2.8 Model Provider Independence

Model-facing code is provider-abstracted.

DeepSeek is the default model endpoint for the current implementation, but production contracts must not depend on DeepSeek-specific response formats beyond the provider adapter.

---

# PART I — Knowledge Preparation & Index Construction

# 3. Part I Responsibility

Part I transforms source files into validated, traceable, retrieval-ready knowledge.

```text
Knowledge Directory
    ↓
Scan
    ↓
Source Mutation Guard
    ↓
Parse / Normalize
    ↓
DocumentContainer
    ↓
Document Analysis
    ↓
Chunk Routing
    ↓
Chunking
    ↓
Production Validation
    ↓
Representation Construction
    ↓
Embedding / Sparse Text
    ↓
PostgreSQL + Milvus Candidate Revision
    ↓
Index Verification
    ↓
Publish
    ↓
READY
```

Part I is a write lifecycle and is intentionally separate from Part II's read/query lifecycle.

---

# 4. Source Loading

## 4.1 Loader Routing — FROZEN

```text
PDF   → MinerU
DOCX  → Docling
XLSX  → Docling
MD    → AtlasRAG native loader
TXT   → AtlasRAG native loader

SQL   → interface reserved only, not implemented in v1
Web   → not Data Loading; belongs to Part II corrective retrieval
```

Loader selection is deterministic by source type.

---

# 5. Canonical Parsed Document Model

## 5.1 DocumentContainer

```text
DocumentContainer
├── schema_version
├── document_id
├── revision_id
├── source
├── metadata
├── parse_info
└── elements[]
```

The normalized parser output is not a single text blob and is not coupled to a parser-specific tree.

---

## 5.2 Element Types

```text
HEADING
PARAGRAPH
LIST
CODE
TABLE
```

Elements are ordered and may contain structural references such as:

```text
section_path
source_anchor
order
```

---

## 5.3 TableData

Tables are first-class parser outputs.

`TableData` belongs to Data Loading and represents the normalized full table.

Later `TableSlice` objects belong to Chunking.

---

## 5.4 ParseInfo

Parse outcome:

```text
SUCCESS
DEGRADED
FAILED
```

Parse reliability:

```text
RELIABLE
PARTIAL
UNAVAILABLE
```

The parser never silently pretends a degraded/failed parse is reliable.

---

# 6. PostgreSQL Canonical Runtime Store

Only three core production tables are required in v1:

```text
documents
document_elements
chunks
```

No historical revision archive system, experiment tables, chunk-set tables or benchmark tables are introduced.

---

## 6.1 documents — FINAL

Conceptual fields include:

```text
document_id
file_name
relative_source_path
content_hash
current_revision_id
building_revision_id
ingestion_status
failed_stage?
parse/status metadata
created_at / updated_at
```

### Why `file_name` is persisted

Earlier designs derived filename from `relative_source_path`. The final architecture persists it because Part II accepts explicit user file-name selectors and final citations frequently display filenames.

```text
file_name
= manual.pdf

relative_source_path
= productA/manual.pdf
```

`relative_source_path` remains authoritative for source disambiguation and provenance.

---

## 6.2 document_elements

Stores normalized parser facts so re-chunking does not require re-running expensive parsers.

It preserves element type, order, structural path, normalized content and source anchoring.

---

## 6.3 chunks

Stores canonical runtime chunks for the current knowledge build.

PostgreSQL remains the authority for chunk content and lineage.

---

# 7. Chunking Architecture

```text
DocumentContainer
    ↓
DocumentAnalyzer
    ↓
deterministic features
    ↓
ChunkRouter
    ↓
selected Chunking Strategy
    ↓
Chunk Records
    ↓
Production Validation
```

`DocumentAnalyzer` and `ChunkRouter` are deterministic and do not call an LLM.

---

## 7.1 Supported Strategies

```text
RECURSIVE
SEMANTIC
STRUCTURE
STRUCTURE_SEMANTIC_CHILD
TABLE
```

Implementation direction:

```text
Recursive / Semantic
→ Chonkie

Structure-aware
→ Docling Hybrid / Hierarchical capabilities
```

Exact applicability regions can be tuned through offline experimentation without changing production domain models.

---

# 8. Parent / Child Chunk Semantics — FROZEN

Chunk types:

```text
TEXT_PARENT
TEXT_CHILD
TABLE
```

Retrieval roles:

```text
TEXT_PARENT = CONTEXT_ONLY
TEXT_CHILD  = RETRIEVABLE
TABLE       = RETRIEVABLE
```

The cross-Part invariant is:

> **TEXT_CHILD is the text retrieval/ranking unit. TEXT_PARENT is the LLM evidence/context unit. TABLE is both a retrieval unit and an evidence unit.**

Therefore:

```text
Dense / BM25
→ TEXT_CHILD / TABLE

Local Evidence Sufficiency Strategy / Generation
→ TEXT_PARENT / TABLE
```

`TEXT_PARENT` is not inserted into Milvus as a searchable vector record.

---

## 8.1 Parent-Child Integrity — FROZEN

For every `TEXT_CHILD`:

```text
parent_id must reference an existing TEXT_PARENT
same document_id
same revision_id
child source range must belong to the parent's structural range
```

This is a Part I Production Validation invariant.

Exact string containment is not required because normalization/chunking may transform formatting, but lineage must prove that the child belongs to the parent.

---

# 9. Table Chunking

```text
TABLE element / TableData
    ↓
TableAnalyzer
    ↓
TableChunkPlan
    ↓
TableChunker
    ↓
TableSlice
    ↓
ChunkRecord(TABLE)
```

Supported conceptual modes:

```text
FULL
ROW
ROW_GROUP
```

Exact rules are tunable.

A `TableSlice` is a usable subtable containing relevant headers/rows/ranges and source-element lineage.

---

# 10. ChunkRecord Contract — FROZEN

```text
ChunkRecord
├── chunk_id
├── document_id
├── revision_id
├── chunk_type
├── retrieval_role
├── chunking_strategy
├── raw_content
├── content_hash
├── parent_id?
├── order
├── section_path
├── provenance
└── table_slice?
```

Chunk IDs remain the primary retrieval lineage identity.

---

# 11. Representation Construction

```text
ChunkRecord
    ↓
RepresentationBuilder
    ↓
dense_text
sparse_text
```

Representation construction is separate from the EmbeddingProvider.

Candidate dense representations may include:

```text
raw content
hierarchy + raw
title + hierarchy + raw
```

Raw content remains highest priority.

No silent truncation is allowed. If raw content cannot fit the configured representation/token constraints, the system reports a chunking/integrity problem rather than silently losing information.

For tables, the baseline representation contains headers plus slice rows.

---

# 12. Dense / Sparse Retrieval Representation

## 12.1 Dense

Baseline:

```text
Qwen3-Embedding-0.6B
```

Alternative provider support may include BGE-M3, but production code only knows an `EmbeddingProvider`.

The embedding model is loaded once per application/runtime process and used with bounded batch inference.

Required checks:

```text
dimension
NaN / Inf
model revision
tokenizer revision
input token limits
```

No silent CPU fallback unless explicitly configured.

---

## 12.2 Sparse

Baseline:

```text
Milvus built-in BM25
```

Analyzer selection remains tunable.

No separate sparse-vector service is required.

---

# 13. RetrievalRecord → Milvus

Transient DTO:

```text
RetrievalRecord
├── chunk_id
├── document_id
├── revision_id
├── file_name
├── parent_id?
├── chunk_type
├── source_type
├── content
├── sparse_text
├── dense_vector
└── structured_metadata
```

Milvus stores only retrievable children and tables.

---

# 14. Milvus Collection — FROZEN BASELINE

Single collection:

```text
atlas_chunks
```

Dense:

```text
HNSW
COSINE
baseline M = 32
baseline efConstruction = 200
runtime ef configurable
```

Sparse:

```text
Milvus BM25 Function
SPARSE_INVERTED_INDEX
baseline k1 = 1.2
baseline b = 0.75
DAAT_MAXSCORE
```

Scalar indexes are added only where real filter/query demand exists.

Milvus contains a derived copy of searchable content/metadata. PostgreSQL remains canonical.

---

# 15. Stable Identity

## 15.1 document_id

Stable identity of the current logical knowledge document.

---

## 15.2 content_hash

Hash of source content.

---

## 15.3 pipeline_fingerprint

Captures Part I build semantics such as:

```text
loader + version
chunking/router configuration
representation configuration
embedding model/revision/tokenizer
BM25 analyzer
Milvus schema/index configuration
```

---

## 15.4 revision_id

```text
revision_id =
H(
  document_id
  + content_hash
  + pipeline_fingerprint
)
```

A revision represents one production knowledge build, not a historical version-control archive.

---

## 15.5 element_id

Deterministic from revision, order/type and canonical content/source anchoring.

---

## 15.6 chunk_id

Conceptually derived from:

```text
document_id
revision_id
chunk_type
chunking_strategy
source_anchor
content_hash
```

The exact hash serialization may be refined during implementation but must remain deterministic.

---

# 16. Ingestion Lifecycle

Single authoritative build state:

```text
RECEIVED
→ PARSING
→ PARSED
→ CHUNKING
→ CHUNKED
→ INDEXING
→ READY
```

Failure:

```text
FAILED
+
failed_stage
```

Allowed failure stages include:

```text
PARSE
CHUNK
REPRESENTATION
EMBEDDING
INDEX_WRITE
INDEX_VERIFY
```

Reliable persisted boundaries:

```text
PARSED
CHUNKED
READY
```

---

# 17. Revision Publish / Failure Semantics

A document may have:

```text
current_revision_id
building_revision_id
```

Example:

```text
R1 = current good revision

Build R2
→ parse
→ chunk
→ embed
→ write candidate
→ verify candidate

only after successful verification:
current_revision_id = R2
```

If R2 fails:

```text
ingestion_status = FAILED
```

describes the latest candidate build failure.

`current_revision_id = R1` still represents the last-good knowledge available for queries.

---

# 18. Exact-Set Retry Semantics

Candidate revision writes are replace/exact-set operations.

For PostgreSQL candidate outputs:

```text
delete candidate-revision outputs
→ write complete candidate set
```

For Milvus candidate records:

```text
delete candidate revision records
→ write complete candidate set
→ flush / visibility barrier
→ verify exact set
```

Retries must not leave stale extra records.

---

# 19. Part I Concurrency / Maintenance

Per-document ingestion uses a PostgreSQL advisory lock.

Process death releases the lock when the database connection closes.

A process interruption is not automatically a business `FAILED` state. Startup reconciliation inspects persisted boundaries and repairs/continues appropriately.

Directory-level convergence is eventual, while per-document publishing is atomic at the architecture level.

---

# 20. Source Mutation Guard

```text
pre-parse fingerprint
    ↓
parse
    ↓
post-parse fingerprint
```

If the source changed during parsing:

```text
discard candidate
do not modify current revision
next sync handles the new source
```

A staging snapshot is not required in v1.

---

# 21. Missing Source Deletion

A document may be considered missing only after a complete successful directory scan.

Deletion order:

```text
Milvus delete
→ visibility / verification
→ PostgreSQL delete
```

This prevents searchable orphan records.

---

# 22. Part I Startup Reconciliation

Before normal query serving:

```text
detect unfinished stages
detect abandoned building_revision_id
detect non-current Milvus residual records
detect missing records / mismatches
repair or clean
```

Part I v1 intentionally does not provide online blue/green index serving.

---

# 23. Part I / Part II Concurrency Boundary — FROZEN

Part I Sync/Reindex does not run concurrently with Part II Retrieval in v1.

Operationally:

```text
SERVING
    ↓
enter MAINTENANCE
    ↓
stop accepting new query runs
    ↓
wait for in-flight requests
    ↓
Part I Sync/Reindex
    ↓
Startup/Reconciliation verification
    ↓
READY
    ↓
resume SERVING
```

This avoids hot-revision routing and simplifies single-machine GPU/resource usage.

---

# 24. Part I Production Validation

Validation is production runtime protection, not an experiment.

Outcome:

```text
PASS
WARN
FAIL
+
stable error_code
```

Validation areas include:

```text
parse integrity
element integrity
chunk integrity
parent-child integrity
table integrity
representation/token limits
embedding dimension / NaN / Inf
PostgreSQL candidate exact-set
Milvus candidate exact-set
index visibility
revision lineage consistency
```

Validation detects problems. It does not silently use an LLM to guess/fix corrupted production data.

---


# 24A. Part I Implementation Architecture — FROZEN

This section freezes the implementation-level Part I graph boundaries agreed after the baseline architecture was written.

## 24A.1 Graph Composition

Part I uses one top-level maintenance graph and one reusable per-document subgraph:

```text
index_graph
└── document_build_subgraph
```

Stable top-level topology:

```text
enter_maintenance
      ↓
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
      ↓
resume_serving
```

Any unrecoverable safety/invariant failure routes to `abort_sync` and serving remains disabled.

Builds occur before deletions. This avoids deleting currently usable knowledge before replacement builds have completed and been independently published.

## 24A.2 Maintenance / Serving Barrier

Part II supports concurrent online requests, but Part I maintenance and Part II serving are mutually exclusive in v1:

```text
SERVING
   ↓ atomic drain/admission barrier
DRAINING
   ↓ active Part II requests drained
MAINTENANCE
   ↓ successful final_verify
Query Cache freshness barrier
   ↓
SERVING
```

A maintenance cycle may be `SUCCESS`, `PARTIAL_SUCCESS`, or `FAILED` independently from runtime serving safety. Serving resumes only when the published current knowledge is globally safe.

Document build failure does not necessarily make serving unsafe if the previous `current_revision_id` remains valid and globally consistent. Missing-source deletion failure is stricter: serving does not resume while knowledge known to be removed may still be searchable.

## 24A.3 Startup Reconciliation

Reconciliation runs before a new scan/plan. It validates and normalizes persisted Part I runtime after interruption; it is not Sync Planning and does not invent new document content.

Output classification:

```text
CLEAN
REPAIRED
BLOCKED
```

Recovery follows the last durable boundary:

```text
PARSING
→ clean candidate and replan

PARSED
→ resume from PARSED when candidate identity remains valid

CHUNKING
→ clear partial chunks and roll back logically to PARSED

CHUNKED
→ resume from CHUNKED

INDEXING
→ delete candidate-revision Milvus records
→ roll back logically to CHUNKED
→ fully reindex

FAILED
→ do not auto-resume
→ clean residuals as needed
→ next SyncPlanner may issue RETRY_FAILED_BUILD
```

If source hash or `pipeline_fingerprint` changed, the old candidate is discarded and replanned. Searchable non-current Milvus residue is removed. A current-revision Milvus index may be rebuilt only when the exact original revision representation/index pipeline can be reproduced. Canonical PostgreSQL corruption, impossible state, or non-reproducible current index yields `BLOCKED`.

## 24A.4 Directory Scan and SyncPlan

Directory scan is observation only and must complete successfully before an executable plan is created.

```text
Directory
  ↓
ScanSnapshot
  +
Canonical DB snapshot
  +
pipeline_fingerprint
  ↓
SyncPlanner (pure deterministic)
  ↓
immutable SyncPlan
```

v1 source matching identity is `relative_source_path`. Rename/move detection is out of scope; a rename is modeled as DELETE old + NEW new.

Plan operations:

```text
BUILD
├── NEW_SOURCE
├── CONTENT_CHANGED
├── PIPELINE_CHANGED
└── RETRY_FAILED_BUILD

DELETE
UNCHANGED
```

Missing-source deletion is planned only after a complete successful scan.

## 24A.5 document_build_subgraph

Stable per-document lifecycle:

```text
prepare_candidate
      ↓
parse_source
      ↓
persist_parsed
      ↓
build_chunks
      ↓
persist_chunked
      ↓
index_candidate
      ↓
verify_candidate
      ↓
publish_revision
```

Special exits:

```text
source mutation
→ discard_candidate
→ END

controlled business build error
→ record_failure
→ END
```

A PostgreSQL per-document session-level advisory lock is acquired outside the subgraph runner and held across the complete document build. Database transactions remain short; expensive parser/chunking/embedding/Milvus work occurs outside PostgreSQL transactions.

Representation construction, embedding, and Milvus writes remain internal to `index_candidate` service/batches/spans rather than becoming separate graph nodes. Large vectors never enter Graph State.

Durable boundaries:

```text
PARSED
CHUNKED
READY
```

`persist_parsed` atomically establishes the exact candidate element set plus `PARSED`. `persist_chunked` atomically establishes the exact candidate chunk set plus `CHUNKED`. Publish atomically sets `current_revision_id = candidate`, clears `building_revision_id`, sets `READY`, clears failure metadata, and marks the persisted Query Cache freshness state invalid whenever the published knowledge view changes.

## 24A.6 Candidate Revision and Milvus Publish

While building `R2`, the published `R1` remains last-good:

```text
current_revision_id  = R1
building_revision_id = R2
```

Candidate indexing sequence:

```text
canonical candidate rows in PostgreSQL
        ↓
representation construction
        ↓
embedding
        ↓
delete candidate revision from Milvus (exact-set retry)
        ↓
write complete candidate retrievable set
        ↓
wait/verify visibility
        ↓
verify exact-set + lineage
        ↓
PostgreSQL publish transaction
        ↓
current_revision_id = R2
```

Milvus is derived state and is never the authority for canonical candidate contents.

## 24A.7 Build Concurrency and Resource Guards

Different documents may build concurrently with bounded document-level concurrency. The same document is serialized by advisory lock.

GPU-heavy providers are process-shared singletons and are protected below graph orchestration by a bounded `ResourceGuard`/semaphore. Document concurrency and GPU concurrency are separate tunable controls.

One document build failure does not cancel independent document builds.

## 24A.8 Final Global Serving Safety

`execute_builds` may produce partial build success. Each successful candidate may publish independently. `execute_deletions` then applies missing-source removals. `final_verify` checks the published global serving view.

Unsafe conditions include:

```text
current PostgreSQL/Milvus inconsistency
searchable orphan/half-published revision
failed required deletion
final global verification failure
```

Only after global safety passes does the runtime cross the Part IV Query Cache freshness barrier and return to `SERVING`. If Redis is unavailable, serving may resume only with Session Query Cache bypassed while the persisted invalidation-required marker remains set; stale cache data must never become visible again merely because Redis later recovers.


# PART II — Retrieval-Augmented Generation

# 25. Part II Responsibility

Part II converts one user turn into a traceable evidence-grounded response.

The stable semantic architecture is:

```text
Query Construction
    ↓
Local Evidence Acquisition
    ↓
RagCore Corrective Routing
    ├── ANSWER
    ├── RETRY_LOCAL ──→ Local Evidence Acquisition
    └── WEB ──────────→ Web Evidence Acquisition
                           ↓
                    Evidence → Answer Boundary
                           ↓
                      Answer Subgraph
                           ↓
                      Final Response
```

Session Query Cache is a Part IV runtime wrapper around this graph and is described later.

## 25.1 Part II Graph Composition — FROZEN

Part II uses the following graph boundaries:

```text
retrieval_graph
└── rag_core_subgraph
    ├── local_evidence_subgraph
    ├── web_evidence_subgraph
    └── answer_subgraph
```

Responsibilities:

```text
retrieval_graph
= online request entry + session-scoped Query Cache wrapper

rag_core_subgraph
= Query Construction + Local/Web/Answer orchestration

local_evidence_subgraph
= one complete local evidence attempt

web_evidence_subgraph
= one complete Web evidence acquisition attempt

answer_subgraph
= already-selected Evidence → validated FinalResponse
```

Subgraphs exchange explicit domain contracts. A subgraph returns business results, not its internal State dump.

There is no shared AtlasRAG mega-state.

Each graph/subgraph owns a minimal TypedDict State; canonical business contracts crossing boundaries are Pydantic models.

## 25.2 rag_core_subgraph — FROZEN

Stable topology:

```text
START
  ↓
construct_query
  ↓
local_evidence_subgraph
  ↓
route_local_evidence
  ├── ANSWER ─────────────────────┐
  ├── RETRY_LOCAL ────────────────┤→ local_evidence_subgraph
  └── WEB → web_evidence_subgraph │
                                  ↓
                         build_answer_input
                                  ↓
                          answer_subgraph
                                  ↓
                            FinalResponse
```

The graph intentionally does not contain `round0` / `round1` specific nodes.

`route_local_evidence` owns the bounded local corrective policy. Its node hyperparameter is:

```text
max_retries = 1   # default
```

`max_retries` is not stored in Graph State and is not exposed as a normal per-request user option.

State stores only the execution fact:

```text
local_attempt_index
```

Changing `max_retries` does not require changing graph topology.

The router may retry only when it can produce a meaningful LocalRetrievalRequest different from the current request. Repeating an identical request is forbidden.

Conceptual minimal RagCore State:

```text
RagCoreState
├── request
├── base_local_request?
├── current_local_request?
├── local_attempt_index
├── local_result?
├── web_result?
├── answer_input?
├── final_response?
├── answer_outcome?
└── result?
```

`AnswerExecutionOutcome` is a small control enum used only between `answer_subgraph` and `rag_core_subgraph`:

```text
SUCCESS
SAFE_FAILURE
```

Unhandled internal exceptions are not converted into `SAFE_FAILURE`; they remain failed invocations.

`base_local_request` is the immutable Query Construction baseline. `current_local_request` may be replaced by corrective routing without mutating the baseline.

Corrective reasons belong primarily in Trace rather than long-lived Graph State.

## 25.3 local_evidence_subgraph — FROZEN BOUNDARY / BENCHMARK-SELECTED STRATEGIES

One invocation means one complete local evidence attempt:

```text
LocalRetrievalRequest
        ↓
Dense ─┐
       ├→ RRF / surviving branch ordering
BM25 ──┘
        ↓
Reranking Strategy
        ↓
Unique Evidence Selection
        ↓
Parent Expansion
        ↓
Local Evidence Sufficiency Strategy
        ↓
LocalEvidenceResult
```

The subgraph has no retry awareness. It does not know attempt index, retry budget, previous attempts, or whether Web will be used later.

Dense and BM25 share the same resolved retrieval scope. Scope normalization is implemented by a shared RetrievalScope builder/service, not as a separate LangGraph node.

Conceptual minimal Local Evidence State:

```text
LocalEvidenceState
├── request
├── dense_branch?
├── bm25_branch?
├── candidate_pool?
├── evidence_refs?
├── local_evidence?
└── result?
```

Large vectors, model instances, clients, repositories, Trace history, previous-attempt State, and retry controls are not stored in this State.

## 25.4 web_evidence_subgraph — FROZEN BOUNDARY

Stable topology:

```text
WebEvidenceRequest(original_query)
        ↓
search_web
        ↓
prepare_web_sources
(fetch + sanitize + budget)
        ↓
build_web_evidence
        ↓
WebEvidenceResult
```

`fetch + sanitize + budget` is one node so large raw page bodies do not cross graph-node boundaries.

Raw HTML is temporary implementation data and must not be stored as long-lived Graph State.

`WebEvidence` does not carry runtime citation IDs. Citation IDs are assigned later by `build_answer_input`, after the final surviving Local/Web evidence set is known. Original Web search rank is preserved separately.

Conceptual minimal Web Evidence State:

```text
WebEvidenceState
├── request
├── search_results?
├── prepared_sources?
└── result?
```

## 25.5 answer_subgraph — FROZEN

Stable topology:

```text
AnswerInput
    ↓
optional compression
    ↓
generate_answer
    ↓
deterministic review_output
    ├── ACCEPT → resolve_citations → FinalResponse
    ├── REGENERATE → generate_answer
    └── BLOCK → safe terminal failure
```

The answer subgraph receives Evidence-layer objects only. It may not call Dense/BM25 retrieval, Milvus search, reranking, Parent fetch, SearXNG, or discover new evidence.

Regeneration is independent from local corrective retrieval.

Conceptual minimal Answer State:

```text
AnswerState
├── input
├── working_evidence?
├── generation_attempt_index
├── generation_result?
├── review_result?
├── final_response?
└── outcome?
```

The output-review router owns:

```text
max_regenerations = 1   # default
```

The limit is a router/node hyperparameter, not a global retry field.

# 26. Query Construction — FROZEN

Query Construction is one graph node with one output.

Every user turn is LLM rewritten.

Reasons include:

```text
noisy user language
search-oriented normalization
relative date interpretation
preserving important lexical identifiers
```

The model receives current/system date/time and timezone where needed.

---

## 26.1 original_query

Immutable raw user question for this turn.

It is preserved throughout Part II and used by:

```text
reranking
local evidence sufficiency strategy
corrective routing
Web search
generation
```

---

## 26.2 search_query

The rewritten retrieval query.

It is used for initial Dense/BM25 retrieval.

---

## 26.3 Protected Terms

Rewrite must preserve critical lexical tokens such as:

```text
error codes
API / class / function names
C++
file names
sheet names
versions
product IDs
numeric IDs
proper nouns
```

---

## 26.4 LocalRetrievalRequest

```text
LocalRetrievalRequest
├── original_query
├── search_query
├── protected_terms[]
└── metadata_filters[]
    ├── field
    ├── operator
    └── value / values
```

No `MODEL_INFERRED` metadata filter exists.

Only user-explicit constraints may become filters.

---

# 27. Metadata Filters — FROZEN

User-facing allowlist:

```text
file_name
source_type
chunk_type
sheet_name
```

Internal IDs such as:

```text
document_id
revision_id
parent_id
```

are not invented by the LLM as user query semantics.

Operators:

```text
EQ
IN
```

Different fields combine with `AND`.

Multiple values for one field use `IN`.

No arbitrary Milvus expression language is exposed to Query Construction.

---

## 27.1 file_name Is a Soft Locator

`file_name` is intentionally softer than the other user-explicit filters.

Users may mistype or misremember a filename.

The initial local attempt may scope retrieval to `file_name`.

If evidence is not sufficient, bounded corrective routing may remove `file_name` while retry budget remains.

Final citations tell the user which source actually supported the answer.

Other explicit filters remain hard in v1:

```text
source_type
chunk_type
sheet_name
```

---

## 27.2 Query Construction Failure

Maximum:

```text
3 total attempts
```

If structured rewrite repeatedly fails:

```text
search_query = original_query
```

and retrieval continues.

The system favors providing a traceable answer rather than hard-failing on a recoverable rewrite issue.

---

# 28. Local Retrieval — FROZEN

```text
LocalRetrievalRequest
        ↓
 ┌───────────────┐
 │ Dense         │
 │ BM25          │
 └───────┬───────┘
         ↓
        RRF
         ↓
CandidatePool
```

Dense and BM25 use the same:

```text
search_query
resolved metadata scope
```

Candidate generation is high recall. No early hard relevance threshold is required.

---

## 28.1 Branch Failure

```text
Dense fail
→ BM25_ONLY

BM25 fail
→ DENSE_ONLY

both fail
→ local retrieval unavailable
→ corrective Web path
```

If only one branch survives, its own ranking is used; fake RRF is not required.

Transient dependency failures may use bounded retries.

Deterministic invalid filters must be handled by validation rather than blind retry.

---

# 29. RRF — FROZEN

RRF is fusion, not reranking.

Identity merge:

```text
chunk_id
```

Raw branch ranks/scores are retained in Trace.

RRF consumes ranks to avoid assuming Dense and BM25 scores are directly comparable.

---

# 30. CandidatePool Contract — FINAL

```text
CandidatePool
├── query_context
│   ├── original_query
│   ├── search_query
│   └── protected_terms
└── candidates[]
    └── RetrievalCandidate
        ├── chunk_id
        ├── document_id
        ├── revision_id
        ├── file_name
        ├── parent_id?
        ├── content
        ├── source_type
        ├── dense_rank?
        ├── dense_score?
        ├── bm25_rank?
        ├── bm25_score?
        ├── rrf_rank?
        ├── rrf_score?
        ├── rerank_rank?
        └── rerank_score?
```

`chunk_id` remains the primary local evidence lineage root.

---

# 31. Reranking Strategy — BENCHMARK-SELECTED

Production uses the abstraction:

```text
RerankerProvider
```

The implementation is selected through an external benchmark rather than permanently hard-coded in the online graph.

Candidate strategies:

```text
CrossEncoder
ColBERT-style late interaction
RankLLM
```

Initial implementation / baseline candidate:

```text
CrossEncoder
FlagEmbedding
BAAI/bge-reranker-v2-m3
```

Primary challenger:

```text
ColBERT-style late interaction
```

RankLLM is retained as a latency-heavier quality challenger/reference.

The selected production strategy must be chosen using the same benchmark corpus and must report both quality and engineering cost, including at least:

```text
Recall / nDCG / MRR effects
P50 / P95 latency
GPU memory
throughput
LLM calls/tokens when applicable
```

The online graph contract does not change when the reranking implementation changes.

Reranker query semantics remain:

```text
original_query
```

not the rewritten retrieval query.

Reason:

```text
search_query
→ retrieval recall

original_query
→ restore ranking toward true user intent
```

If the selected reranker is unavailable at runtime:

```text
fallback = RRF ordering
status = DEGRADED
```

The reranker experiment remains external to production State/domain models.

# 32. Unique Evidence Selection — FROZEN

Do not simply take Top-N Child chunks and then deduplicate parents.

That can collapse diversity:

```text
C1 → P1
C2 → P1
C3 → P1
C4 → P1
C5 → P1
C6 → P2
```

If Top-5 were selected first, the LLM would receive only P1.

Instead:

```text
reranked Child/Table list
    ↓
walk in rank order
    ↓
collect unique Evidence Units
    ↓
stop when N unique Parent/Table units are collected
```

For multiple children that map to the same parent, Evidence identity is deduplicated by Parent identity. Child-level retrieval details do not cross into the Evidence-layer business contract.

```text
C1 → P1
C2 → P1
C4 → P1

Unique Evidence order:
P1
```

The first occurrence in the ranked candidate sequence determines Parent/Table Evidence ordering. Child→Parent mapping details may be recorded in Trace for debugging, but `representative_child_id`, `retrieval_chunk_ids`, Child text and Child scores are not retained in `EvidenceRef` / `LocalEvidence`.

TABLE is already an evidence unit and does not require parent expansion.

---

# 33. Parent Context Expansion — FROZEN

For text:

```text
reranked child
    ↓
parent_id
    ↓
Parent Context Service
    ↓
TEXT_PARENT
```

Final semantic rule:

> **Child is the retrieval/ranking unit. Parent is the LLM evidence unit.**

The Local Evidence Sufficiency Strategy and Generation receive Parent text, not Child text.

---

## 33.1 Parent Fetch

Parent IDs are deduplicated and fetched in batches.

Part IV may resolve them using Redis cache-aside before PostgreSQL.

---

## 33.2 Canonical Evidence and Token Budgets

`LocalEvidence.content` keeps the complete canonical Parent/Table context loaded from PostgreSQL. Canonical Evidence is not permanently truncated to fit a particular model call.

Part II may create temporary bounded views for sufficiency assessment or Generation:

```text
canonical LocalEvidence
    ↓
SufficiencyInputBuilder / GenerationInputBuilder
    ↓
model-specific bounded view
```

Relevant limits remain tunable and benchmarked later:

```text
per_evidence_token_limit
total_evidence_evaluation_budget
total_generation_evidence_budget
```

Bounded views are temporary node-local/provider-input representations and do not replace canonical `LocalEvidence` in Graph State.

---

# 34. Evidence Identity and Lineage

The Evidence-layer identity is independent from retrieval-child identity.

For local text:

```text
TEXT_CHILD candidate
    ↓ parent_id
EvidenceRef(TEXT_PARENT, parent_id)
    ↓ canonical fetch
LocalEvidence(TEXT_PARENT)
```

For TABLE:

```text
TABLE candidate
    ↓
EvidenceRef(TABLE, table_chunk_id)
    ↓ canonical fetch
LocalEvidence(TABLE)
```

Conceptual Evidence identity:

```text
(document_id, revision_id, evidence_type, context_id)
```

`EvidenceRef` and `LocalEvidence` do not retain Child IDs, Child text, retrieval scores, or representative-child fields. Child→Parent hit lineage belongs to Trace/observability only.

Runtime citation IDs (`L1`, `W1`, ...) are assigned later at the Evidence→Answer boundary and are not canonical Evidence identity.

---

# 35. Local Evidence Sufficiency Strategy — BENCHMARK-SELECTED

This is an online routing signal, not Part III benchmark evaluation.

Its purpose is to normalize one local evidence attempt into a stable business status consumed by `rag_core_subgraph`:

```text
SUFFICIENT
INSUFFICIENT
AMBIGUOUS
```

The strategy implementation is benchmark-selected.

At minimum compare:

```text
A. LLM-based evidence sufficiency assessment
B. deterministic score/rule-based routing using retrieval/reranking signals
```

The objective is to determine whether the extra LLM call materially improves answer quality enough to justify its latency and token cost.

Regardless of implementation, the parent graph sees the same conceptual contract:

```text
LocalEvidenceResult
├── execution_status
│   ├── OK
│   ├── DEGRADED
│   └── UNAVAILABLE
├── evidence_status
│   ├── SUFFICIENT
│   ├── INSUFFICIENT
│   └── AMBIGUOUS
├── selected_evidence[]
└── best_available_evidence?
```

`best_available_evidence` allows Web fallback to keep only the strongest local Parent/Table evidence when the local result is `INSUFFICIENT` without exposing the full internal ranked list to `rag_core_subgraph`.

If an LLM strategy is used, it consumes bounded Parent/Table evidence and must not persist free-form hidden reasoning.

Retrieval scores remain Trace data unless required by the selected deterministic routing strategy.

## 35.1 Status Semantics

### SUFFICIENT

Current local evidence is adequate to reliably answer the original user question.

### INSUFFICIENT

Evidence is missing key facts or is not relevant enough to reliably answer.

### AMBIGUOUS

Evidence is meaningfully related but there is a plausible Query Rewrite / Query Interpretation drift and the current evidence should not be trusted as a stable answer basis.

`AMBIGUOUS` is intentionally narrow.

When the terminal local result remains `AMBIGUOUS` and routing proceeds to Web, ambiguous local evidence is not passed into final Generation; the Web evidence path starts without that potentially drifted local context.

# 36. Bounded Local Corrective Loop — FROZEN

Corrective local retrieval is expressed as one reusable loop in `rag_core_subgraph`, not as fixed `Round 0` / `Round 1` graph nodes.

Current router default:

```text
max_retries = 1
```

This is a `route_local_evidence` node hyperparameter, not a State field and not a graph-topology assumption.

The State stores only:

```text
local_attempt_index
```

Current baseline routing policy:

### SUFFICIENT

```text
→ build_answer_input
→ answer_subgraph
```

### Local retrieval UNAVAILABLE

```text
→ Web
```

### AMBIGUOUS with retry available

Create a new LocalRetrievalRequest:

```text
query = original_query
drop file_name if present
keep other explicit hard metadata
```

Then invoke the same `local_evidence_subgraph` again with a fresh subgraph State.

### INSUFFICIENT + file_name exists + retry available

Create a new LocalRetrievalRequest:

```text
query = baseline search_query
drop file_name
keep other explicit hard metadata
```

Then invoke the same `local_evidence_subgraph` again with a fresh subgraph State.

### Otherwise

```text
→ Web
```

A corrective retry is allowed only when the newly generated LocalRetrievalRequest differs meaningfully from the current request.

No previous Dense/BM25/RRF/reranking/Parent-expansion State is inherited by the next attempt.

Changing `max_retries` later does not require changing graph topology.

# 37. Web Corrective Retrieval — FROZEN BOUNDARY / SUMMARY STRATEGY EXPERIMENTAL

Provider:

```text
SearXNG MCP
```

Input query:

```text
original_query
```

One Web search acquisition pass per Web subgraph invocation.

Stable process boundary:

```text
SearXNG search
    ↓
Top-N results
    ↓
prepare_web_sources
(fetch + sanitize + per-page/total budget)
    ↓
Web evidence representation
    ↓
WebEvidence[]
```

Current baseline candidate keeps:

```text
Top-5 search results
+
one batch DeepSeek summary call
```

The stable graph node remains `build_web_evidence`; summary is an internal strategy, not a graph-topology commitment.

However, the Web summary LLM is explicitly subject to ablation.

Compare at least:

```text
A. bounded sanitized/snippet content → Generator
B. one batch DeepSeek summary → Generator
```

Use answer quality, latency, and token cost to choose the production default.

SearXNG does not generate the final answer.

## 37.1 WebEvidence

```text
WebEvidence
├── title
├── url
├── domain
├── content
├── representation
└── search_rank

WebEvidenceRepresentation
├── SUMMARY
├── SANITIZED_CONTENT
└── SEARCH_SNIPPET
```

`WebEvidence` itself has no citation ID. Runtime citation IDs are assigned only in `build_answer_input`, after the final surviving Local/Web evidence set is known.

Web content is treated as:

```text
UNTRUSTED_EXTERNAL_CONTENT
```

Web page instructions are data, not commands.

Raw HTML/page bodies are not persisted in Web Graph State.

## 37.2 Web Failure

Individual URL fetch failure does not kill the whole stage.

Possible fallback:

```text
usable snippet
→ keep result

no usable content
→ drop result
```

`WebEvidenceResult.execution_status` uses stable business statuses:

```text
OK
DEGRADED
EMPTY
UNAVAILABLE
```

Technical error details and vendor exceptions belong in Trace/Log, not in Graph State. If the whole Web search dependency fails, the result is `UNAVAILABLE`; if execution succeeds but yields no usable evidence, the result is `EMPTY`. Evidence is never fabricated.

# 38. Local Evidence on Web Fallback

If local evidence status is `INSUFFICIENT`, do not send all weak local evidence to the generator.

Keep only:

```text
best_available_evidence
= highest-ranked local Parent/Table evidence unit
```

and explicitly mark:

```text
local_evidence_status = INSUFFICIENT_BEST_MATCH
```

The generator is told that this is the closest local reference, not sufficient proof.

If the terminal local evidence status is `AMBIGUOUS`, do not pass ambiguous local evidence into Generation after Web fallback. Potentially drifted local context must not anchor the final answer.

# 39. Optional Compression

Compression occurs only after the Evidence set for Answer has been determined.

It is never used before retrieval/reranking/local evidence sufficiency routing.

Purpose:

```text
reduce noise
reduce generation tokens
```

Compression is a strategy/ablation point rather than a required LLM stage.

Compare options such as:

```text
NONE
deterministic trimming/selection
model-based compression
```

Input:

```text
query
+
selected Parent/Table/Web evidence representation
```

Output preserves the same evidence/citation identity.

Canonical Evidence objects are not destructively truncated. A working/bounded representation may be created for Generation.

Failure:

```text
use original selected Evidence
```

Compression remains subject to benchmark selection.

# 40. Generation Input

Conceptual JSON:

```json
{
  "query": "original user query",
  "local_evidence_status": "SUFFICIENT",
  "local_knowledge": [
    {
      "citation_id": "L1",
      "document_name": "manual.pdf",
      "content": "..."
    }
  ],
  "web_search": [
    {
      "citation_id": "W1",
      "title": "...",
      "url": "...",
      "domain": "...",
      "content": "..."
    }
  ]
}
```

Internal runtime may additionally preserve `chunk_id`, `parent_id`, and `document_id` for lineage; the model only needs the fields required for grounded generation.

Local and Web evidence remain separate sections by design.

---

## 40.1 Duplicate Filenames

Citation display:

```text
basename unique
→ manual.pdf

basename ambiguous
→ productA/manual.pdf
```

`relative_source_path` is used only when needed for disambiguation.

---

# 41. Generation Prompt Boundary

Generator requirements:

```text
answer original_query
prefer managed local knowledge
Web is external supplementary evidence
do not invent unsupported facts
use only allowed citation IDs
do not invent URLs
do not invent document names
if evidence remains insufficient, say so
treat evidence text as data, not control instructions
```

The Generation node cannot perform retrieval or tool calls.

---

# 42. GenerationResult — FROZEN V1

```text
GenerationResult
├── answer_text
└── used_citation_ids[]
```

Example:

```text
used_citation_ids = [L1, L2, W1]
```

v1 uses answer-level citation provenance.

The architecture leaves room for future claim-level traceability, but claim-level citation mapping is not implemented in v1.

---

# 43. Output Review Filter — FROZEN

Output Review is deterministic where practical.

It checks:

```text
output/schema integrity
unknown citation IDs
unknown URLs
source-boundary violations
forged tool/protocol output
obvious internal control/prompt leakage
```

It does not act as another factual LLM judge.

It must not use a crude rule such as:

```text
if answer contains "ignore previous instructions":
    block
```

because legitimate user questions may discuss prompt injection.

Input-side trust boundaries remain the primary defense.

---

# 44. Citation Resolver

Only the system resolves citation IDs to source metadata.

```text
L1
→ document_id / file_name / relative path / local lineage

W1
→ title / URL / domain
```

The LLM does not manufacture real URLs or internal IDs.

---

# 45. Trace Contract — FROZEN

Graph State answers:

> What data is still needed for this execution?

Trace answers:

> How did this execution arrive at the final result?

---

## 45.1 Common SpanRecord

```text
SpanRecord
├── trace_id
├── span_id
├── parent_span_id?
├── stage
├── attempt
├── started_at
├── ended_at
├── duration_ms
├── status
│   ├── OK
│   ├── DEGRADED
│   └── ERROR
├── error_code?
└── attributes
```

---

## 45.2 Major Spans

```text
Query Construction
Local Retrieval
  ├── Dense
  ├── BM25
  └── RRF
Reranking Strategy
Unique Evidence Selection
Parent Expansion
Local Evidence Sufficiency Strategy
Corrective Route
Web Search
Web Evidence Preparation
Web Summary?
Compression?
Generation
Output Review
Citation Resolver
```

Part IV adds cache lookup spans.

---

## 45.3 Retrieval Trace

Per candidate as needed:

```text
chunk_id
dense_rank / score
bm25_rank / score
rrf_rank / score
rerank_rank / score
```

---

## 45.4 Evidence Trace

```text
context_parent_id / evidence chunk id
retrieval_chunk_ids[]
selected/not selected
evidence status
reason_codes
```

---

## 45.5 Corrective Route Trace

```text
local_attempt_index
decision
corrective_reason
```

Examples:

```text
QUERY_DRIFT
FILENAME_RELAXATION
QUERY_DRIFT_AND_FILENAME_RELAXATION
LOCAL_EVIDENCE_INSUFFICIENT
```

---

## 45.6 Query Logging Privacy

Development may allow full original/search query logging.

Production-like deployment must allow query logging to be configured or redacted.

Full user queries are not required to be permanently persisted.

---

# 46. Trace / Log / Metrics Separation

## Trace

One request's execution path.

## Log

Program/runtime events such as:

```text
Milvus timeout
Redis failure
CUDA OOM
MCP connection reset
```

## Metrics

Aggregated system health/performance:

```text
P50 / P95
cache hit ratio
Web fallback rate
corrective retry rate
LLM calls/query
tokens/query
error rate
```

---

# 47. Error Contract

Caller-safe response:

```text
error_code
trace_id
safe_message
```

Internal logs may additionally contain:

```text
exception_type
exception_message
stack trace
dependency
stage
attempt
```

Example code families:

```text
QUERY_*
RETRIEVAL_*
RERANK_*
PARENT_*
EVIDENCE_*
WEB_*
GENERATION_*
SECURITY_*
CITATION_*
CACHE_*
```

Detailed internal exception text must not be exposed directly to the user.

---

# 48. Retry / Degradation Policy

Baseline direction:

```text
Query Construction
→ max 3 structured-output attempts
→ fallback original_query

Dense failure
→ BM25_ONLY

BM25 failure
→ DENSE_ONLY

both local branches fail
→ Web

Selected reranker failure
→ RRF / surviving branch ordering

LLM-based local sufficiency strategy, if selected
→ bounded structured-output retry
→ then conservative normalized status / Web route

Deterministic local sufficiency strategy, if selected
→ no LLM retry semantics

Compression failure
→ original selected evidence

Web URL fetch failure
→ snippet/drop

Web batch summary failure, if that strategy is enabled
→ bounded prepared/snippet representation or drop according to the selected Web policy

Generation provider transient failure
→ bounded provider/service retry

Output Review severe issue
→ bounded answer regeneration; default max_regenerations = 1, then block
```

Local corrective retrieval is a separate RagCore orchestration loop:

```text
route_local_evidence.max_retries = 1  # default
```

Provider technical retry, local corrective retry, and answer regeneration are distinct mechanisms and do not share a global retry counter.

Exact timeout values remain tunable.

Optional optimization failure should degrade where possible instead of failing the entire request.

---

# 49. Cancellation

Request cancellation should stop scheduling later nodes and attempt to cancel expensive outstanding operations such as:

```text
DeepSeek HTTP
SearXNG fetch
Web summary
later GPU work
```

A canceled request is never cached as a successful Query Cache result.

---

# 50. No Durable Part II Request Recovery

Part II is request-scoped.

If the application process crashes:

```text
current request fails
caller retries
```

The system does not attempt to resume a request from the reranker or Web-search stage.

This is intentionally different from Part I ingestion lifecycle recovery.

---

# 51. Session and Multi-Turn Semantics — FINAL

Multi-session and multi-turn are distinct concepts.

## 51.1 Session

A `session_id` defines:

```text
UI/CLI conversation container
request isolation namespace
Session Query Cache namespace
multi-session concurrency boundary
```

Different sessions must never share Session Query Cache entries.

---

## 51.2 One Session May Have Multiple Turns

A session may contain:

```text
Q1 → A1
Q2 → A2
Q3 → A3
```

The Web UI and CLI may continue using the same `session_id`.

However, v1 does **not** require history-aware semantic retrieval.

By default, each user turn executes Part II using its own raw request:

```text
current user turn
→ Query Construction
→ Retrieval
```

Conversation history is not automatically injected into Query Construction, retrieval, or generation for pronoun/coreference resolution.

Therefore:

```text
session can be multi-turn
≠
RAG pipeline has conversational memory
```

Future support may add a Conversation Resolution stage before Query Construction without changing the rest of the architecture.

This distinction is intentional.

---

# 52. Local vs Web Conflict Policy

No conflict-resolution agent in v1.

Policy:

```text
managed local knowledge preferred
Web remains external/supplementary
if Web differs, answer may state the difference
preserve Web URL for verification
```

---

# 53. Part II Latency Guardrail

Permanent LLM calls in the online path are minimized and benchmark-justified.

The stable required LLM work is:

```text
Query Construction LLM
Final Generation LLM
```

Additional model-dependent stages are optional/benchmark-selected:

```text
RankLLM reranking, if selected
LLM-based local evidence sufficiency strategy, if selected
Web batch summarization, if selected
model-based compression, if selected
```

Corrective local attempts repeat retrieval/reranking and any selected local sufficiency strategy.

Therefore:

> Do not add permanent LLM nodes without clear benchmark evidence that the quality gain justifies latency, token cost, and operational complexity.

Part III must report Avg LLM calls/query and latency for each selected strategy.


# 46A. Part II Domain Contracts and State Model — FROZEN IMPLEMENTATION ARCHITECTURE

This section is authoritative for the implementation-level Part II contracts agreed after the baseline architecture was written. Where an earlier conceptual example uses different field names, this section takes precedence.

## 46A.1 User Request and Session Boundary

```text
UserTurnRequest
├── session_id
├── query
└── filters?
```

`session_id` is the LangGraph `thread_id`, UI/CLI conversation container, and Session Query Cache namespace. Thread continuity does not inject transcript/history into Query Construction, Retrieval, Evidence Sufficiency, routing, or Generation in v1.

`QueryFilters` is an explicit user-facing whitelist:

```text
file_name?    # soft locator
source_type?  # hard filter
sheet_name?   # hard filter
```

Advanced constraints such as `chunk_type` belong to internal retrieval filters, not the normal user-facing API.

`request_hash` is computed from deterministic canonical serialization of answer-affecting raw request fields (`query + filters`). `session_id` is already represented by the cache namespace and is not duplicated inside the hash.

## 46A.2 Local Retrieval Contracts

```text
LocalRetrievalRequest
├── original_query
├── retrieval_query
└── filters?
```

Corrective local retrieval may change only `retrieval_query` and soft locator fields such as `file_name`. `original_query` and hard filters remain unchanged.

Dense/BM25 consume the same deterministic `ResolvedRetrievalScope`, produced by a shared builder/service rather than a graph node.

```text
RetrievalCandidate
├── chunk_id
├── document_id
├── revision_id
├── chunk_type        # TEXT_CHILD | TABLE
├── parent_id?        # required for TEXT_CHILD
├── retrieval_text
└── scores
    ├── dense_score?
    ├── bm25_score?
    ├── fusion_score?
    └── rerank_score?
```

The list order represents current rank. Rank history is Trace data, not DTO fields.

```text
RetrievalBranchResult
├── branch            # DENSE | BM25
├── status            # OK | EMPTY | UNAVAILABLE
└── candidates[]
```

`EMPTY` means the branch executed normally but found no hits. `UNAVAILABLE` means the capability did not execute successfully.

```text
CandidatePool
├── candidates[]
├── retrieval_mode    # HYBRID | DENSE_ONLY | BM25_ONLY | UNAVAILABLE
├── ordering          # RRF | DENSE | BM25 | RERANKER
└── degraded
```

Reranker failure preserves the previous ordering and marks the pool degraded. The same contract supports CrossEncoder, ColBERT-style late interaction, and RankLLM; `rerank_score` is optional because an ordering-only reranker is valid.

## 46A.3 EvidenceRef and LocalEvidence

`EvidenceRef` is the bridge from Retrieval Layer to Evidence Layer:

```text
EvidenceRef
├── evidence_type     # TEXT_PARENT | TABLE
├── context_id        # parent_id or table_chunk_id
├── document_id
└── revision_id
```

After this conversion, Child IDs/text/scores no longer belong to the business Evidence contract.

```text
LocalEvidence
├── evidence_type
├── context_id
├── document_id
├── revision_id
├── content           # complete canonical Parent/Table context
└── provenance
```

`LocalEvidence` contains enough canonical provenance for Citation Resolver without returning to Retrieval Layer. Source-specific anchors (page, heading, sheet/cell range, etc.) are represented through the provenance contract.

`LocalEvidence` has no runtime `citation_id`.

```text
LocalEvidenceResult
├── execution_status  # OK | DEGRADED | UNAVAILABLE
├── evidence_status?  # SUFFICIENT | INSUFFICIENT | AMBIGUOUS
├── selected_evidence[]
└── best_available_evidence?
```

Rules:

```text
SUFFICIENT
→ selected_evidence non-empty

INSUFFICIENT
→ selected_evidence empty
→ best_available_evidence may contain one useful best match

AMBIGUOUS
→ selected_evidence empty
→ ambiguous evidence is not carried into final Generation after Web fallback

UNAVAILABLE
→ evidence_status = None
→ no selected/best evidence
```

`execution_status` and `evidence_status` are independent. For example, Dense may fail while BM25 still yields `DEGRADED + SUFFICIENT`.

## 46A.4 Web Evidence Contracts

```text
WebEvidenceRequest
└── original_query
```

```text
WebSearchResult
├── title
├── url
├── domain
├── snippet?
└── search_rank
```

```text
PreparedWebSource
├── title
├── url
├── domain
├── search_rank
├── content
└── content_origin     # FETCHED_PAGE | SEARCH_SNIPPET
```

Raw HTML and raw HTTP responses never enter Graph State.

```text
WebEvidence
├── title
├── url
├── domain
├── search_rank
├── content
└── representation    # SUMMARY | SANITIZED_CONTENT | SEARCH_SNIPPET
```

`build_web_evidence` is strategy-stable: the production experiment may select one batch DeepSeek summary or direct sanitized/budgeted content without changing the graph contract.

```text
WebEvidenceResult
├── execution_status  # OK | DEGRADED | EMPTY | UNAVAILABLE
└── evidence[]
```

`WebEvidence` has no runtime citation ID. Runtime IDs are assigned only at the Evidence→Answer boundary.

## 46A.5 AnswerInput — Evidence → Answer Boundary

Local and Web Evidence remain separate types rather than being flattened into one nullable mega-DTO.

```text
AnswerLocalEvidence
├── citation_id       # L1, L2, ...
└── evidence          # LocalEvidence

AnswerWebEvidence
├── citation_id       # W1, W2, ...
└── evidence          # WebEvidence
```

```text
LocalAnswerEvidenceStatus
├── SUFFICIENT
├── INSUFFICIENT_BEST_MATCH
└── NONE
```

```text
AnswerInput
├── original_query
├── local_evidence_status
├── local_evidence[]
└── web_evidence[]
```

Citation IDs are assigned deterministically in `build_answer_input` after the final surviving evidence set is known:

```text
Local → L1, L2, ...
Web   → W1, W2, ...
```

`AMBIGUOUS` is a retrieval-routing state and does not cross into `AnswerInput`. `AnswerInput` is immutable and is the authoritative runtime mapping from citation IDs to evidence/provenance.

Web execution diagnostics (`OK/DEGRADED/...`) do not enter normal `AnswerInput`; they remain orchestration/Trace data.

## 46A.6 Answer Output Contracts

```text
GenerationResult
├── answer_text
└── used_citation_ids[]
```

Output Review is deterministic/protocol-oriented:

```text
OutputReviewResult
├── decision          # ACCEPT | REGENERATE | BLOCK
└── reason_codes[]
```

It validates schema, allowed citation IDs, consistency between answer text and declared citations, forged source references, source-boundary violations, protocol/control leakage, and similar deterministic checks. It is not a factual LLM judge.

Citation Resolver consumes only the reviewed `GenerationResult` plus immutable `AnswerInput`.

```text
FinalCitation
= FinalLocalCitation | FinalWebCitation
```

```text
FinalResponse
├── answer_text
└── citations[]       # only citations actually used in the answer
```

Local citations expose authoritative file/provenance display information; Web citations expose title/url/domain. Citation order follows first appearance in `answer_text`.

```text
RagCompletionStatus
├── SUCCESS
├── DEGRADED_SUCCESS
└── SAFE_FAILURE
```

```text
RagCoreResult
├── final_response
└── completion_status
```

`SAFE_FAILURE` is a controlled, normally completed RAG outcome (for example insufficient evidence after Web is unavailable). Unexpected invariant/internal exceptions are failed invocations and must not be disguised as `SAFE_FAILURE`.

`cache_eligible` is not stored in `RagCoreResult`; `QueryCachePolicy` derives cache eligibility from the result and current policy.

## 46A.7 Final TypedDict State Schemas

```text
RetrievalGraphInput
└── request: UserTurnRequest

RetrievalGraphState
├── request
├── rag_result?
└── final_response?

RetrievalGraphOutput
└── final_response
```

```text
RagCoreInput
└── request

RagCoreState
├── request
├── base_local_request?
├── current_local_request?
├── local_attempt_index
├── local_result?
├── web_result?
├── answer_input?
├── final_response?
├── answer_outcome?
└── result?

RagCoreOutput
└── result: RagCoreResult
```

```text
LocalEvidenceSubgraphInput
└── request: LocalRetrievalRequest

LocalEvidenceState
├── request
├── dense_branch?
├── bm25_branch?
├── candidate_pool?
├── evidence_refs?
├── local_evidence?
└── result?

LocalEvidenceSubgraphOutput
└── result: LocalEvidenceResult
```

```text
WebEvidenceSubgraphInput
└── request: WebEvidenceRequest

WebEvidenceState
├── request
├── search_results?
├── prepared_sources?
└── result?

WebEvidenceSubgraphOutput
└── result: WebEvidenceResult
```

```text
AnswerSubgraphInput
└── input: AnswerInput

AnswerState
├── input
├── working_evidence?
├── generation_attempt_index
├── generation_result?
├── review_result?
├── final_response?
└── outcome?

AnswerSubgraphOutput
├── final_response
└── outcome            # SUCCESS | SAFE_FAILURE
```

State is execution data only. It must not be used as dependency container, configuration store, trace/log accumulator, exception transport, cache-metadata container, provider-response dump, previous-attempt archive, raw-HTML/vector transport, or service locator.

The only graph-semantic counters are:

```text
RagCoreState.local_attempt_index
AnswerState.generation_attempt_index
```

Technical provider retries stay below the graph boundary.

## 46A.8 Error / Failure / Degradation Contract — FROZEN

Normal negative outcomes are not Exceptions:

```text
EMPTY
INSUFFICIENT
AMBIGUOUS
```

Provider/Repository adapters normalize external/vendor failures into typed AtlasRAG technical exceptions. Services translate a technical exception into a Domain degradation/status only when the architecture explicitly defines a safe fallback.

Examples of allowed degradation:

```text
Dense unavailable → BM25-only
Reranker unavailable → keep pre-rerank ordering
Web URL fetch fails → use safe snippet when available, otherwise drop URL
Redis Parent Cache unavailable → PostgreSQL canonical fetch
Query Cache unavailable → cache miss / skip write; request continues
```

Fail-closed examples:

```text
canonical PostgreSQL corruption
published-current revision inconsistency
citation mapping inconsistency
impossible State transition
boundary contract/invariant violation
```

Node functions remain thin State↔Service adapters and do not implement generic catch-all `try/except` policies.

Error detail ownership:

```text
Domain Result → stable statuses required by parent business logic
Trace         → stable error_code + degradation/fallback facts
Log           → stacktrace/vendor technical detail
Metrics       → aggregated operational counts/latency/error rates
```

Technical retry is below Graph; semantic/business retry is represented explicitly in Graph State.

Process crashes are not rewritten as business failures. Part I recovers persisted candidate work through Startup Reconciliation; Part II produces no partial Session Query Cache entry because only complete validated results are cacheable.

# PART III — External Verification & Benchmark Harness

# 54. Part III Responsibility — FROZEN

Part III is an **external verification and benchmark harness**, not an online AtlasRAG runtime module.

Its responsibility is to prove that:

```text
Part I produces consistent, reproducible retrieval-ready knowledge
Part II retrieves and ranks useful evidence
Evidence routing behaves correctly
Generation remains grounded in allowed evidence
Citations remain valid and resolvable
Failure/degradation paths behave as designed
Quality/performance changes can be compared reproducibly
```

The defining dependency rule is:

```text
Part III ─────→ Part I / Part II     allowed

Part I / Part II ─────→ Part III     forbidden
```

Deleting the complete benchmark package must not break production application startup, Part I, Part II, CLI/Web query serving, or maintenance behavior.

Production code must never contain benchmark-aware fields or branches such as:

```text
benchmark_mode
benchmark_case_id
qrel
gold_label
expected_answer
leaderboard_score
```

Part III may consume production entrypoints, normal dependency injection/configuration, and production observability events. It must not change production business contracts merely to make evaluation easier.

---

# 55. Primary External Benchmark — FROZEN

AtlasRAG v1 uses:

```text
Vectara Open RAGBench
```

as the primary external benchmark dataset.

The benchmark remains replaceable through a dataset adapter. It is not part of the AtlasRAG core domain and is not bundled into the production runtime.

Current v1 modality scope:

```text
INCLUDED
text
text + table

EXCLUDED
image-dependent queries
text + image
text + table + image
```

The v1 query profile therefore uses the supported text/text-table cases while keeping the full retrieval corpus intact.

Reference benchmark profile:

```text
FULL_CORPUS
= all 1000 benchmark documents
= positive documents + hard-negative documents

V1_QUERY_SET
= text + text-table queries
= 2062 cases in the current dataset release
```

Critical rule:

> Filter the query set for v1 modality support; do not shrink the retrieval corpus to only the documents containing answers.

Otherwise hard negatives disappear and retrieval quality becomes artificially inflated.

Open RAGBench currently carries a non-commercial dataset license. Dataset licensing is therefore treated as an external constraint: the benchmark adapter/code belongs to AtlasRAG, but the dataset itself is not assumed to be distributable as part of a commercial product bundle.

---

# 56. Benchmark Physical Isolation — FROZEN

Part III uses an isolated runtime environment rather than adding benchmark namespaces to production domain tables.

Conceptually:

```text
Production
──────────
PostgreSQL     atlasrag
Milvus         production collection
Redis          production prefix
Knowledge      production knowledge directory

Benchmark
─────────
PostgreSQL     atlasrag_bench
Milvus         benchmark collection
Redis          benchmark prefix/instance
Knowledge      benchmark snapshot directory
```

The exact Docker project/connection names are implementation details, but the isolation rule is architectural.

Benchmark code lives outside the production package, conceptually:

```text
src/atlasrag/        # production only
benchmarks/          # Part III only
tests/               # deterministic production tests
benchmark_data/      # local snapshot, not production knowledge
benchmark_runs/      # generated artifacts
```

Allowed import direction:

```text
benchmarks → atlasrag
```

Forbidden:

```text
atlasrag → benchmarks
```

Benchmark-only dependencies belong to an optional development/benchmark dependency group rather than the production runtime dependency set.

---

# 57. Dataset Snapshot and Reproducibility — FROZEN

Formal benchmark runs must use a frozen local dataset snapshot.

Initial preparation:

```text
benchmark source URLs / dataset files
        ↓
download once
        ↓
local immutable snapshot
        ↓
checksums / manifest
```

The snapshot manifest records at least:

```text
dataset name/version
source identifiers / URLs
PDF SHA256 values
query file hash
qrels file hash
answer file hash
processed corpus file hash, when used
snapshot creation timestamp
```

Comparable runs must not silently re-download newer source documents.

This prevents changing arXiv/PDF content, redirects, missing files, or dataset updates from being confused with AtlasRAG quality changes.

---

# 58. Benchmark Corpus Ingestion — FROZEN

The formal end-to-end benchmark must ingest the benchmark source documents through the **real Part I production entrypoint**.

```text
Open RAGBench source documents
        ↓
Benchmark Knowledge Directory
        ↓
production index_graph
        ↓
MinerU / Docling
        ↓
Elements
        ↓
Parent / Child / Table chunks
        ↓
Embeddings / Milvus
        ↓
Published benchmark revisions
```

Forbidden shortcuts for the formal end-to-end run:

```text
writing benchmark chunks directly into PostgreSQL
writing vectors directly into Milvus
constructing fake production ChunkRecords from qrels
bypassing parser/chunker/validation/publish lifecycle
```

Such shortcuts may exist only as isolated algorithm/unit experiments and may never be reported as the AtlasRAG end-to-end benchmark.

Before a formal quality run, a **Corpus Readiness Gate** verifies:

```text
expected source exact set exists
Part I build/publish completed
published PostgreSQL canonical state is valid
Milvus published exact set is valid
final global verification is SAFE_TO_SERVE
```

If the corpus is incomplete, the run is not comparable and cannot produce an official quality scorecard.

---

# 59. Benchmark Gold Alignment — FROZEN

Open RAGBench qrels and AtlasRAG canonical evidence do not share the same segmentation identity.

The benchmark may define:

```text
query_id
→ benchmark document
→ benchmark section
```

while AtlasRAG produces:

```text
document_id
revision_id
TEXT_PARENT parent_id
TABLE chunk_id
```

Therefore Part III must not assume:

```text
benchmark section_id == AtlasRAG parent_id
```

Part III owns a benchmark-only one-to-many alignment contract:

```text
GoldEvidenceAlignment
├── query_id
├── gold_document_id
├── gold_section_id
├── aligned_evidence_ids[]
└── alignment_confidence
    ├── HIGH
    ├── MEDIUM
    └── UNRESOLVED
```

One gold section may legitimately align to multiple AtlasRAG Parents/Tables.

Alignment uses benchmark source/corpus content plus AtlasRAG canonical evidence content and source identity. Gold/qrel metadata never enters production PostgreSQL, production DTOs, Graph State, or production Trace.

Formal evidence-level metrics use only the confidence class permitted by the metric policy, with **HIGH confidence** as the default strict set.

Every official report must state:

```text
alignment coverage
HIGH count/rate
MEDIUM count/rate
UNRESOLVED count/rate
```

Dropped/unresolved cases must never be silently hidden behind a high retrieval score.

---

# 60. Text/Table Metric Semantics — FROZEN

Open RAGBench text-table cases are useful for exercising AtlasRAG TABLE handling, but the public qrel does not necessarily provide a gold AtlasRAG table identity.

Therefore v1 distinguishes:

```text
Gold-grade metrics
→ document / aligned section-evidence relevance

Table diagnostics
→ whether TABLE evidence participated
→ TABLE rank / selection behavior
→ table-related latency and representation diagnostics
```

AtlasRAG v1 must not claim a precise `Table Recall@K` unless Part III can establish a reliable benchmark-only gold mapping to the required table.

Reports must be sliced at least by:

```text
OVERALL
TEXT
TEXT_TABLE
```

and should additionally report available extractive/abstractive or equivalent dataset categories when useful.

Overall averages must not hide weak table performance.

---

# 61. Positive and Negative Corpus Profiles — FROZEN

Part III uses at least two corpus profiles.

## 61.1 FULL_CORPUS

```text
positive documents
+
hard-negative documents
```

Used for normal retrieval and end-to-end quality measurement.

## 61.2 NEGATIVE_ONLY_CORPUS

Uses the benchmark hard-negative document set without the positive answer-bearing documents.

The same supported queries are executed against this corpus to measure whether AtlasRAG incorrectly declares sufficient evidence or answers from model memory.

Expected architectural behavior:

```text
no adequate local evidence
Web = OFF
→ Local INSUFFICIENT / appropriate controlled result
→ no unsupported answer from parametric memory
→ SAFE_FAILURE when no valid evidence path remains
```

Negative-corpus metrics include at least:

```text
False Sufficiency Rate
Unsupported Answer Rate
Safe Failure Rate
```

A model producing a factually correct answer from pretraining memory without allowed Evidence is still a protocol failure for AtlasRAG.

Gold-document-removal experiments may be used as diagnostics, but `NEGATIVE_ONLY_CORPUS` is the primary negative benchmark.

---

# 62. Benchmark Execution Modes — FROZEN

Quality and performance are separate run classes.

## 62.1 Quality Run

Mandatory policy:

```text
Web Search              OFF
Session Query Cache     OFF
Parent Context Cache    OFF
fixed dataset snapshot
fixed query set
fixed strategy/configuration
```

The benchmark harness configures these through normal dependency injection/configuration. Production code does **not** implement an `if benchmark_mode` branch.

Each case receives an independent session/thread namespace such as conceptually:

```text
benchmark:{run_id}:{query_id}:{repetition}
```

This prevents checkpointer/session contamination even though v1 RAG semantics do not consume chat history.

## 62.2 Performance Run

Performance is measured separately with declared cache/environment conditions, for example:

```text
COLD
WARM
```

and reports:

```text
P50 latency
P95 latency
throughput
peak VRAM
GPU OOM count
GPU queue/wait time, where available
```

Quality comparisons must not be mixed with hidden warm-cache advantages.

---

# 63. Retrieval Evaluation — FROZEN

Core retrieval metrics are deterministic and implemented/owned by Part III rather than delegated as the sole authority to a changing external evaluation package.

At minimum:

```text
Hit / Recall@5
Hit / Recall@10
MRR@10
nDCG@10
```

Report both where supported:

```text
strict aligned section/evidence relevance
relaxed document relevance
```

The production retrieval DTOs are not expanded for benchmark use.

Retrieval-stage ranking observations come from normal production observability events such as:

```text
Dense candidate IDs/ranks/scores
BM25 candidate IDs/ranks/scores
Fusion ordering
Reranker ordering
EvidenceRef selection
```

Production Trace must **not** contain:

```text
gold label
qrel
expected answer
benchmark case type
```

Part III joins production Trace with benchmark gold externally.

Benchmark trace collection uses 100% capture; sampling is disabled for benchmark runs.

---

# 64. Answer, Citation and Routing Evaluation — FROZEN

End-to-end evaluation invokes the real production `retrieval_graph` and observes normal outputs:

```text
UserTurnRequest
        ↓
retrieval_graph
        ↓
RagCoreResult / FinalResponse
        ↓
Part III evaluator
```

No simplified benchmark-only RAG pipeline replaces production execution.

Evaluation dimensions include:

```text
reference-answer quality / correctness
source-groundedness / factual consistency
citation validity
citation-to-gold relevance where alignment supports it
completion status behavior
routing behavior
safe-failure behavior
```

Deterministic protocol/invariant metrics remain first-class and should be preferred whenever the expected outcome can be checked without an LLM judge.

External/open-source answer evaluation libraries may be used as **secondary evaluators** only. They must be version/commit pinned and represented by an `evaluator_fingerprint`.

The evaluator fingerprint includes answer-affecting evaluation semantics such as:

```text
evaluator package/version or commit
judge model/version
judge prompt/version
metric configuration
thresholds
```

Scores produced under incompatible evaluator fingerprints are not compared as if they were the same metric.

---

# 65. Strategy Experiments — FROZEN

Part III determines production choices that Part II intentionally leaves strategy-pluggable.

Required experiment families include:

```text
Reranker
├── CrossEncoder baseline
├── ColBERT / late-interaction challenger
└── RankLLM challenger

Local Evidence Sufficiency
├── deterministic strategy
└── LLM-based strategy

Web Evidence Representation
├── direct sanitized/budgeted content
└── one batch grounded summary

Answer Evidence Compression
├── NONE
├── deterministic
└── model-based
```

One factor changes at a time.

Experiments are classified as:

```text
INDEX-AFFECTING
→ embedding model
→ chunking semantics
→ table representation
→ other pipeline_fingerprint changes
→ MUST rebuild Part I benchmark corpus

RUNTIME-ONLY
→ reranker selection
→ sufficiency strategy
→ answer compression strategy
→ other runtime_fingerprint-only changes
→ MAY reuse the same published benchmark corpus
```

A runtime-only experiment must not silently reuse an index built under a different `pipeline_fingerprint` when the changed variable actually affects the index.

---

# 66. Reranker Isolation + E2E Validation — FROZEN

Reranker comparison uses two levels.

## 66.1 Isolation Benchmark

Part III freezes the same pre-rerank candidate sets as benchmark artifacts:

```text
Query Q
→ identical CandidatePool before reranking
   ├── CrossEncoder
   ├── ColBERT / late interaction
   └── RankLLM
```

This measures reranking quality/latency without upstream retrieval variation.

## 66.2 End-to-End Validation

Promising strategies are then re-run through the full production path:

```text
retrieval_graph
```

so the final choice reflects real system behavior rather than isolated ranking quality alone.

Reranker evaluation should consider:

```text
retrieval quality
P50/P95 latency
throughput
VRAM
failure/degradation rate
```

Part III does not define a single weighted “winner score”; trade-offs are reported explicitly.

---

# 67. Sufficiency and Web/Compression Ablations — FROZEN

A sufficiency strategy must be tested on both:

```text
FULL_CORPUS positive cases
NEGATIVE_ONLY_CORPUS cases
```

so a strategy cannot obtain an artificially strong result by labeling everything `SUFFICIENT`.

Relevant metrics include:

```text
False Insufficient Rate
False Sufficient Rate
Unnecessary Web Route Rate
Safe Failure Rate
LLM-call/latency overhead
```

Quality benchmark Web remains OFF.

The Web representation experiment therefore uses fixed benchmark-only `PreparedWebSource` fixtures rather than live internet search:

```text
same prepared sources
   ├── direct sanitized/budgeted content
   └── grounded batch summary
        ↓
     same answer pipeline
```

This isolates the question:

> Is the extra Web summarization call worth its quality/latency/cost?

Compression ablation follows the same one-factor-change rule.

---

# 68. Run Validity, Repetition and Failure Accounting — FROZEN

Part III distinguishes product outcome from benchmark-run validity.

```text
RunValidity
├── VALID
├── PARTIAL
└── INVALID
```

Examples:

```text
AtlasRAG legitimately returns INSUFFICIENT
→ product result

Remote LLM unavailable for a material portion of cases
→ benchmark operational degradation / possibly PARTIAL or INVALID

benchmark process OOM/crash
→ run validity failure
```

The runner must not silently rerun a failed observation and overwrite it with a success.

If a deliberate repeat is executed, it has a distinct:

```text
repetition_index
```

If the benchmark process itself is resumed after interruption, only genuinely unfinished observations may be resumed according to the benchmark artifact state.

Remote/model-based evaluation and generation are not assumed bit-for-bit deterministic even with low/zero temperature. Part III supports multiple repetitions for LLM-sensitive experiments and may report:

```text
mean
standard deviation
confidence interval
```

The exact repetition count is a benchmark configuration decided from observed variance/cost, not hard-coded in the architecture.

---

# 69. Run Manifest and Comparison Safety — FROZEN

Every benchmark run produces an immutable run manifest containing at least:

```text
run_id
benchmark dataset snapshot identity
corpus profile
query-set profile
code git commit
pipeline_fingerprint
runtime_fingerprint
evaluator_fingerprint
strategy configuration
Web/cache benchmark policy
environment information
hardware profile
timestamp
```

Before producing a comparison delta, `RunComparisonValidator` verifies compatibility.

Depending on the metric class, compatibility includes:

```text
same dataset snapshot
same relevant query set
same corpus profile
same metric version
evaluator compatibility
same hardware/environment class for performance comparison
```

Incompatible runs are not presented as meaningful percentage improvements/regressions.

No single weighted “AtlasRAG Score” is defined. Reports keep separate dimensions:

```text
retrieval quality
answer quality
routing/safe-failure quality
reliability
latency
throughput
VRAM/resource usage
```

---

# 70. Benchmark Artifacts and Regression Gate — FROZEN

Part III stores results outside production databases, for example conceptually:

```text
benchmark_runs/<run_id>/
├── manifest.json
├── alignment.parquet
├── cases.parquet
├── retrieval_results.parquet
├── answer_results.parquet
├── metrics.json
└── report.md
```

These are benchmark artifacts, not production runtime state.

The first accepted complete benchmark establishes a baseline.

Regression thresholds for soft quality/performance metrics are **not invented before that baseline exists**.

After baseline approval, automated regression gates compare compatible runs against that baseline using explicit tolerances.

Hard architectural invariants remain 100% requirements where applicable, including examples such as:

```text
citation IDs resolve
output schemas validate
session isolation is preserved
retry bounds are respected
published lineage remains valid
```

---

# 71. Part III Tooling and Execution Model — FROZEN

Part III is ordinary Python tooling/CLI; it is **not** another LangGraph workflow.

Conceptual structure:

```text
BenchmarkCLI
    ↓
BenchmarkRunner
    ↓
Dataset Adapter
    ↓
production Part I / Part II
    ↓
Evaluator
    ↓
Artifacts / Report
```

Possible supporting open-source evaluation tooling may be used, but deterministic AtlasRAG metrics remain owned by Part III.

All formal benchmark work must be runnable non-interactively so Codex/CI can:

```text
prepare/validate isolated benchmark dependencies
run Part I ingestion
run retrieval/E2E/negative/performance experiments
collect 100% benchmark Trace
compute metrics
validate run comparability
generate machine-readable + Markdown reports
```

Codex is an execution/analysis agent, not the semantic gold source.

---

# 72. Public Benchmark Interpretation Limit — FROZEN

Open RAGBench is a synthetic/document-grounded benchmark distribution and is not a substitute for every future enterprise workload.

Therefore AtlasRAG reports must phrase results as benchmark-specific evidence, for example:

```text
AtlasRAG achieved X on the frozen Open RAGBench profile
```

not:

```text
AtlasRAG has X% accuracy for all enterprise RAG workloads
```

The benchmark is used to support:

```text
strategy selection
regression detection
retrieval comparison
routing comparison
Part I/Part II end-to-end reproducibility
```

---

# PART IV — Local Deployment & Operations

# 73. Part IV Responsibility — FROZEN

Part IV defines the reference local deployment and operational lifecycle for AtlasRAG v1.

It is intentionally a **local single-machine deployment**, not a claim of Kubernetes/cloud-scale production operation.

Reference environment:

```text
Windows Host
└── WSL2 Ubuntu
    ├── native AtlasRAG Python runtime
    ├── native CUDA/PyTorch access to RTX 3070 8GB
    ├── local Knowledge Directory / model cache / logs
    └── Docker Compose infrastructure
```

Part IV must preserve all Part I/II ownership and consistency rules rather than introducing deployment shortcuts that weaken them.

---

# 74. Local Deployment Topology — FROZEN

AtlasRAG application/GPU execution runs natively inside WSL2.

Docker Compose runs stateful/supporting infrastructure:

```text
PostgreSQL
Redis
Milvus standalone
  ├── etcd
  └── MinIO
SearXNG
```

Conceptual topology:

```text
Windows Host
│
├── Browser / local clients
│
└── WSL2 Ubuntu
    │
    ├── AtlasRAG Application Runtime
    │   ├── Composition Root
    │   ├── Runtime Ownership Lock
    │   ├── RuntimeGate
    │   ├── MaintenanceCoordinator
    │   ├── HealthRegistry
    │   ├── ResourceGuard
    │   ├── index_graph
    │   ├── retrieval_graph
    │   ├── embedding model
    │   └── selected reranker
    │          ↓
    │       RTX 3070
    │
    ├── Knowledge Directory
    ├── model cache
    ├── logs/traces
    │
    └── Docker Compose
        ├── PostgreSQL
        ├── Redis
        ├── Milvus + etcd + MinIO
        └── SearXNG
```

v1 deliberately does not containerize the GPU-heavy AtlasRAG application process. This avoids unnecessary NVIDIA-container/GPU-passthrough/model-cache complexity in the reference environment.

Default service exposure is localhost-only. PostgreSQL, Redis, Milvus and SearXNG are not intended to be LAN-exposed by default.

No reverse proxy, Kubernetes, GPU cluster, distributed model service or micro-batching platform is required in v1.

---

# 75. Single Application Runtime — FROZEN

Part I and Part II share **one AtlasRAG application runtime**:

```text
ONE composition root
ONE set of shared clients/pools
ONE set of loaded GPU models
ONE ResourceGuard
ONE RuntimeGate
```

Do not create independent indexing and serving Python services in v1.

Part II supports concurrent requests/sessions, while Part I maintenance and Part II serving remain mutually exclusive.

GPU-heavy operations are bounded by a shared `ResourceGuard`. Initial concurrency may be conservative (for example effectively serialized) and is a tunable runtime configuration rather than a graph semantic.

The exact LangGraph launcher/CLI command is an implementation concern. `langgraph dev` must only be described as a development/local-dev mode, not as cloud/production deployment terminology.

---

# 76. Single-Runtime Ownership and Control Boundary — FROZEN

A machine-local ownership mechanism prevents a second AtlasRAG business runtime from starting concurrently against the same local deployment.

Conceptually:

```text
Runtime A acquires ownership lock
Runtime B startup
→ ownership conflict
→ fail startup
```

Port binding may provide additional protection but is not the sole correctness mechanism.

User and maintenance CLI commands are **thin clients** to the already-running AtlasRAG runtime.

```text
atlasrag serve
→ owns business runtime

atlasrag ask
atlasrag sync
atlasrag health/status
atlasrag backup
→ client/control requests to running runtime
```

A maintenance CLI must never create a second composition root and invoke `index_graph` independently, because that would bypass the Part I/Part II mutual-exclusion barrier.

Logical planes remain separate:

```text
User Plane
→ ask / Web UI
→ retrieval_graph

Control Plane
→ sync / backup / operational status
→ application coordinator
```

The exact localhost HTTP/Unix-socket transport is deferred to the Implementation Guide.

Only one maintenance operation may own the maintenance coordinator at a time.

---

# 77. Runtime State Machine — FROZEN

Process-local runtime modes:

```text
STARTING
SERVING
DRAINING
MAINTENANCE
BLOCKED
SHUTTING_DOWN
```

Normal startup:

```text
Process Start
    ↓
STARTING
    ↓
Bootstrap hard dependencies/config/models
    ↓
Startup Reconciliation
    ↓
Global Safety Verification
    ├── SAFE   → SERVING
    └── UNSAFE → BLOCKED
```

A hard bootstrap failure such as invalid configuration, unavailable required PostgreSQL/Milvus after bounded startup retry, or required model initialization failure causes startup to fail/exit non-zero rather than leaving a half-initialized process pretending to be `BLOCKED`.

`BLOCKED` is reserved for a successfully initialized runtime that has identified an unsafe knowledge/canonical consistency condition.

Examples:

```text
published current PG/Milvus mismatch that cannot be safely repaired
canonical data corruption
unsafe deletion residue
impossible persisted state
maintenance final verification failure
```

An empty but internally consistent KB is a valid serving state; emptiness alone is not a readiness failure.

---

# 78. Atomic Query Admission Lease — FROZEN

`RuntimeGate` is not merely an enum variable. Query admission and mode transitions share one atomic synchronization mechanism.

Query lifecycle:

```text
RuntimeGate.acquire_query_lease()
    ├── mode != SERVING → reject/not-ready
    └── mode == SERVING
           ↓
       active_queries += 1
           ↓
       retrieval_graph
           ↓
       finalization/cache-if-eligible
           ↓
       active_queries -= 1
```

The lease covers the complete request lifecycle through final response/cache finalization.

Maintenance starts atomically:

```text
SERVING → DRAINING
and block new query leases
```

under the same lock/condition used for active query accounting.

This prevents a race where maintenance observes `active_queries == 0` while a new query enters simultaneously.

---

# 79. Maintenance Lifecycle — FROZEN

Application-level `MaintenanceCoordinator` owns the lifecycle; it is not a LangGraph Node.

```text
maintenance requested
        ↓
atomic SERVING → DRAINING
        ↓
block new query leases
        ↓
wait active_queries == 0
        ↓
MAINTENANCE
        ↓
index_graph
        ↓
reconciliation/final global verify
        ├── SAFE   → cache freshness barrier → SERVING
        └── UNSAFE → BLOCKED
```

A maintenance drain has a bounded timeout.

If drain times out **before MAINTENANCE begins**:

```text
abort maintenance request
DRAINING → SERVING
```

Do not kill valid user queries merely to force maintenance to start.

Once MAINTENANCE has begun, cancellation/failure cannot jump directly back to SERVING. It must pass through reconciliation and global safety verification.

Document-level build failure does not automatically block serving. If last-good current revisions remain consistent and global verification is safe, the sync may be `PARTIAL_SUCCESS` while runtime returns to `SERVING`.

Missing-source deletion failure is stricter: known-removed knowledge remaining searchable makes global serving unsafe, so serving cannot resume until deletion consistency is restored.

---

# 80. Startup Reconciliation and Crash Recovery — FROZEN

Startup Reconciliation is the single crash/interruption recovery entrypoint for persisted Part I work.

Startup sequence includes:

```text
load/validate config
connect hard dependencies
connect soft dependencies where available
load embedding/reranker resources
construct repositories/services
run Startup Reconciliation
verify published knowledge
build/activate graphs
enter SERVING if safe
```

Startup reconciliation restores consistency only. It does not automatically launch a full new sync or rebuild every `FAILED` document.

Process-level failures such as:

```text
kill -9
Windows/WSL reboot
Linux OOM kill
unexpected process crash
```

are recovered by the same next-startup reconciliation path.

No separate crash-recovery graph is introduced.

---

# 81. Graceful Shutdown — FROZEN

On SIGTERM/Ctrl+C or equivalent shutdown request:

```text
runtime → SHUTTING_DOWN
reject new query/maintenance work
bounded drain of active work
close resources
exit
```

For Part II, unfinished requests canceled after the graceful timeout do not write Session Query Cache entries.

For Part I, shutdown/interruption does **not** fabricate a business `FAILED` state merely because an operational cancellation occurred.

Durable Part I boundaries remain:

```text
PARSED
CHUNKED
READY
```

and the next startup reconciliation restores/cleans partial `PARSING/CHUNKING/INDEXING` work according to the frozen Part I rules.

Maintenance drain timeout and process shutdown timeout are intentionally different:

```text
maintenance drain timeout
→ abort maintenance; preserve user query execution

shutdown timeout
→ cancellation allowed so process can exit
```

`ResourceGuard` acquisition must be cancellation-aware so queued GPU work does not begin after shutdown has made the request ineligible to continue.

---

# 82. Hard vs Soft Runtime Dependencies — FROZEN

Startup/readiness hard dependencies include:

```text
valid runtime configuration
Knowledge Directory accessibility
PostgreSQL canonical store
Milvus published retrieval store
required embedding capability/model
published knowledge safety/compatibility
```

At startup, a missing required Milvus service is treated as a hard readiness failure rather than intentionally booting into a long-lived BM25-only runtime.

Soft/degradable dependencies include:

```text
Redis
SearXNG
reranker capability when fallback ordering is available
observability exporters/sinks
```

Examples:

```text
Redis unavailable
→ Parent Cache bypasses to PostgreSQL
→ Query Cache bypassed
→ serving may continue

SearXNG unavailable
→ local answers still work
→ WebEvidence may become UNAVAILABLE

Reranker unavailable
→ retain previous fusion ordering
→ serving may continue degraded
```

Per-request degradation and startup readiness are distinct concepts. A component may have a valid request-level fallback without being considered an acceptable permanently missing hard startup dependency.

The application reports infrastructure failure; it does not control/restart Docker containers itself.

---

# 83. Health, Liveness and Readiness — FROZEN

Health concepts are separated.

## 83.1 Liveness

Answers whether the application process/event loop is alive.

A `BLOCKED` process may still be live.

## 83.2 Readiness

Answers whether new Part II requests may be accepted.

Conceptually:

```text
ready =
RuntimeMode == SERVING
AND hard dependency probes are healthy
AND last verified knowledge safety state is valid
```

Readiness does not rerun an expensive full-database verification on every request; it uses bounded dependency probes plus the current verified safety state.

Soft dependency failures do not force not-ready.

## 83.3 Health Details

A process-local `HealthRegistry` tracks operational status such as:

```text
Runtime         SERVING
Knowledge       VERIFIED
PostgreSQL      HEALTHY
Milvus          HEALTHY
Redis           DEGRADED
SearXNG         UNAVAILABLE
Embedding       HEALTHY
Reranker        HEALTHY/DEGRADED
QueryCache      ACTIVE/BYPASSED
```

Health/status endpoints and CLI status read this registry; business graphs do not depend on it for their domain decisions.

Sensitive exception/configuration details are not exposed through the simple readiness endpoint.

---

# 84. Session Query Cache — FROZEN V1

Session Query Cache remains a top-level wrapper around `rag_core_subgraph`.

Lookup happens before Query Construction.

Conceptual key:

```text
atlasrag:query:{session_id}:{runtime_fingerprint}:{request_hash}
```

where `request_hash` is deterministic canonical serialization of answer-affecting raw request fields (`query + filters/request options`).

No whole-KB revision identifier appears in the key.

Knowledge freshness is protected by the Part I/Part II maintenance barrier and the persisted cache freshness mechanism below.

A cache hit returns a fully validated/resolved `FinalResponse` while receiving a new request/trace identity.

Cache eligibility remains policy-derived from completed `RagCoreResult`; partial/canceled/invalid/error results are never cached.

v1 does not implement distributed same-key single-flight/cache stampede prevention. Duplicate computation on simultaneous misses is acceptable.

---

# 85. Query Cache Freshness Barrier — FROZEN

Because Redis is a soft dependency and the Session Query Cache key intentionally has no knowledge revision, cache invalidation must survive Redis outages and process crashes.

PostgreSQL owns a persisted correctness marker conceptually equivalent to:

```text
query_cache_invalidation_required
```

Any successful knowledge publication/change that can make existing Session Query Cache entries stale transactionally marks this state `true` as part of the canonical knowledge-change lifecycle.

Before Query Cache can be trusted again:

```text
invalidation_required = true
        ↓
attempt Query Cache invalidation
        ├── Redis healthy + invalidation succeeds
        │       ↓
        │   clear persisted marker
        │       ↓
        │   Query Cache enabled
        │
        └── Redis unavailable/failure
                ↓
            keep marker = true
                ↓
            SERVING may continue
            but Query Cache is BYPASSED
```

When Redis later becomes available, AtlasRAG must invalidate stale query-cache entries **before** clearing the marker/enabling Query Cache.

This prevents old cache data from becoming visible again after a temporary Redis outage.

Parent Context Cache does not need this global invalidation marker because its key is revision-aware:

```text
atlasrag:parent:{revision_id}:{parent_id}
```

Old Parent Cache entries simply become unreachable by current-revision requests and expire naturally.

---

# 86. Configuration and Secrets — FROZEN

Configuration is loaded, validated and frozen at startup.

Conceptual categories:

```text
InfrastructureConfig
ModelConfig
PartIConfig
RetrievalConfig
AnswerConfig
CacheConfig
MaintenanceConfig
ObservabilityConfig
```

Configuration precedence may use:

```text
code defaults
→ checked-in non-secret local config
→ environment variables / .env for local secrets
```

Exact file format is an implementation decision.

Unknown configuration fields should fail validation rather than be silently ignored.

No hot reload is required in v1. Answer-affecting configuration changes require process restart and are represented through the appropriate fingerprints.

Secrets such as API keys/passwords:

```text
are not committed
are not included in fingerprints
are not written to logs/traces/manifests
```

The repository provides example configuration/environment files only.

A redacted runtime manifest may record:

```text
config/schema version
pipeline_fingerprint
runtime_fingerprint
model identities
non-secret operational configuration necessary for reproducibility
```

Deployment-only values such as hostnames/ports/passwords should not unnecessarily change answer-semantics fingerprints.

---

# 87. Runtime Fingerprints — FROZEN

`pipeline_fingerprint` protects Part I build semantics.

`runtime_fingerprint` protects answer-affecting Part II runtime semantics, including examples such as:

```text
query-construction prompt/behavior version
retrieval semantic configuration
selected reranker/model version
local evidence sufficiency strategy version
generation prompt/model version
Web evidence representation strategy
compression strategy when answer-affecting
citation/output policy version
```

Knowledge change is **not** represented by putting a whole-KB revision into the Session Query Cache key. It is handled by maintenance mutual exclusion plus the persisted Query Cache freshness barrier.

---

# 88. Client/API Response Boundary — FROZEN

Part IV does not modify the Part II `FinalResponse` domain contract merely to attach transport/observability metadata.

Conceptually:

```text
QueryResponseEnvelope
├── trace_id
└── response: FinalResponse
```

Transport/application metadata such as:

```text
trace_id
session_id
runtime status
transport timing
```

belongs outside `FinalResponse`.

A query-cache hit also receives a fresh trace identity.

---

# 89. Safe Streaming Policy — FROZEN

v1 supports **stage/status streaming**, not raw unreviewed LLM answer-token streaming.

Safe events may include conceptually:

```text
query_received
retrieval_started
retrieval_completed
rerank_completed
local_evidence_assessed
web_search_started
generation_started
review_started
completed
error/safe failure status
```

Do not stream:

```text
raw model answer tokens before review
Chain-of-Thought
system prompts
hidden trace internals
stack traces
raw private evidence content
```

The final answer is delivered only after:

```text
GenerationResult
→ deterministic Output Review
→ Citation Resolution
→ FinalResponse
```

This preserves Output Review/Citation Resolver as the authoritative publication boundary.

Future buffered/uncommitted token streaming may be designed separately; it is out of scope for v1.

---

# 90. Persistence Ownership and Backup — FROZEN

Storage ownership remains:

```text
Knowledge Directory
= source authority

PostgreSQL
= canonical processed/runtime knowledge

Milvus
= derived retrieval index

Redis
= disposable cache
```

The minimum recoverable backup bundle contains at least:

```text
Knowledge Directory snapshot
PostgreSQL logical/canonical backup
schema/migration version
pipeline_fingerprint
redacted runtime/config manifest
model identities/revisions/checksums needed to reproduce indexes
```

Milvus snapshot is an optional **fast-restore optimization**, not the sole canonical backup.

Redis application caches need not be backed up; restore may start Redis empty.

Model identity must be sufficient to determine whether the original published index can be exactly reproduced. If an original embedding/index configuration cannot be reproduced and the current canonical revision therefore cannot be safely reconstructed, restore must fail closed rather than silently changing semantics.

---

# 91. Coordinated Backup and Restore — FROZEN

Formal v1 backup uses the same operational exclusion machinery rather than inventing online snapshot complexity.

Conceptually:

```text
SERVING
→ DRAINING
→ MAINTENANCE
→ stable backup snapshot
→ verification
→ SERVING
```

The Knowledge Directory may be modified by external user actions, so source snapshot creation must detect mutation (for example via before/after identity/hash checks) and abort an inconsistent backup.

Restore sequence:

```text
restore Knowledge Directory
restore PostgreSQL canonical state
validate schema/config/model compatibility
start Redis empty
restore or rebuild Milvus
Startup Reconciliation
Global Safety Verification
→ SERVING only if safe
```

Restoring a container volume is never by itself proof that the application may serve.

If exact current-index reproduction is impossible or canonical/published consistency cannot be established, runtime remains/enters `BLOCKED`.

---

# 92. Trace / Log / Metrics / Health Separation — FROZEN

Observability has distinct owners.

```text
Trace
= one execution's structured lineage/history

Log
= technical diagnostic detail

Metrics
= low-cardinality aggregated trends

HealthRegistry
= current process-local operational status
```

## 92.1 Trace

Trace may contain structured data such as:

```text
trace_id / span IDs
stage/node/service/provider
stable IDs
candidate ranks/scores
routing/fallback decisions
stable error_code
latency
cache hit/miss/bypass
```

Trace does not persist hidden Chain-of-Thought.

Benchmark runs may use a complete Trace sink, but production Trace schemas do not contain benchmark gold/qrels.

## 92.2 Logs

Logs contain technical detail such as:

```text
timestamp
level
trace_id
component
stable error_code
exception class
stacktrace
diagnostic message
```

Secrets, authorization headers, passwords, private keys and equivalent sensitive values are always redacted.

Raw user/evidence/document content is not placed into ordinary logs by default.

Local log rotation/retention prevents unbounded disk growth.

## 92.3 Metrics

Metrics are low-cardinality and may include:

```text
query count/latency
safe-failure/degraded counts
dense-unavailable count
reranker fallback count
Web fallback count
query/parent cache ratios
maintenance duration/build outcomes
reconciliation repairs
gpu wait/execution time
GPU OOM count
```

High-cardinality values such as:

```text
query text
session_id
document_id
URL
```

are not Metrics labels.

## 92.4 Observability Failure

Telemetry/exporter failure is a soft operational degradation and must not change RAG correctness or block valid serving.

The application should retain a local diagnostic fallback where practical.

---

# 93. Stable Error Vocabulary — FROZEN

Part IV reuses the same AtlasRAG stable error-code registry established by Part I/II.

It does not invent a deployment-specific duplicate taxonomy.

Conceptually the same `error_code` may appear consistently in:

```text
Trace
Log
Metrics
Health diagnostics
```

while stacktrace/vendor raw detail remains Log-only.

Business logic never branches on free-form exception-message text.

---

# 94. Part IV Acceptance — FROZEN

Part IV v1 is complete when the local reference deployment can demonstrate at least:

```text
WSL2 native AtlasRAG runtime starts with one runtime owner
Docker infrastructure starts independently
hard bootstrap failure exits/fails cleanly
safe persisted-state inconsistency enters BLOCKED
Startup Reconciliation runs before readiness
liveness/readiness semantics are distinct
CLI/Web query succeeds through the running runtime
Local/Web citations resolve correctly
transport returns a trace_id outside FinalResponse
two sessions can issue concurrent Part II requests without state leakage
same-session exact request can hit Session Query Cache
different sessions do not share Session Query Cache
Parent Context Cache may be shared across sessions
Redis unavailable → serving continues with caches bypassed/fallback
knowledge change + Redis unavailable → stale Query Cache cannot reappear
SearXNG unavailable → local serving remains possible
reranker failure follows the frozen fallback path
RTX 3070 dual-session test avoids uncontrolled GPU OOM
maintenance transition uses atomic drain/admission lease
maintenance drain timeout aborts maintenance rather than killing queries
Part I/Part II never overlap during maintenance
maintenance success invalidates/bypasses stale Query Cache before serving
maintenance interruption reconciles before returning to serving
missing-source deletion inconsistency blocks serving
raw unreviewed answer tokens are not published
final answer appears only after Review + Citation Resolution
graceful shutdown does not fabricate business FAILED states
restart after interrupted Part I recovers via Startup Reconciliation
backup/restore re-enters serving only after safety verification
```

---

# PART I–IV CROSS-PART CONTRACT REVIEW

# 95. Core Identity and Authority Flow

Source/build identity:

```text
relative_source_path
→ document_id

content_hash
+
pipeline_fingerprint
+
document_id
→ revision_id
```

Evidence identity:

```text
TEXT_CHILD retrieval hit
→ parent_id
→ TEXT_PARENT EvidenceRef/LocalEvidence

TABLE retrieval hit
→ table chunk identity
→ TABLE EvidenceRef/LocalEvidence
```

Session/runtime identity:

```text
session_id
= LangGraph thread/container
= Query Cache namespace
≠ semantic conversational memory
```

Cache identity:

```text
Session Query Cache
= session namespace
+ runtime_fingerprint
+ exact raw request hash

Knowledge freshness
= maintenance exclusion
+ persisted cache invalidation barrier
```

Ownership:

```text
Knowledge Directory → source authority
PostgreSQL          → canonical runtime knowledge
Milvus              → derived retrieval index
Redis               → disposable caches
LangGraph State     → current execution data
Trace               → execution history
Part III artifacts  → external evaluation history
```

---

# 96. End-to-End Maintenance Flow — FINAL

```text
Maintenance request
        ↓
RuntimeGate atomically enters DRAINING
        ↓
block new query leases
        ↓
wait active Part II requests
        ↓
MAINTENANCE
        ↓
reconcile_runtime
        ↓
complete directory scan
        ↓
pure deterministic SyncPlanner
        ↓
execute document builds (bounded parallel)
        ↓
execute deletions
        ↓
final global verification
        ├── unsafe
        │    ↓
        │  BLOCKED
        │
        └── safe
             ↓
        knowledge changed?
             ├── no → SERVING
             └── yes
                  ↓
        mark/query-cache invalidation barrier
                  ↓
          invalidate if Redis available
          otherwise bypass Query Cache
                  ↓
                SERVING
```

Each document build:

```text
prepare_candidate
→ parse_source
→ persist_parsed
→ build_chunks
→ persist_chunked
→ index_candidate
→ verify_candidate
→ publish_revision
```

Current published revision remains last-good until verified candidate publication succeeds.

---

# 97. End-to-End Query Flow — FINAL

```text
UserTurnRequest
    ↓
RuntimeGate.acquire_query_lease()
    ↓
RetrievalGraph
    ↓
Session Query Cache lookup
    ├── trusted HIT
    │     ↓
    │  validated FinalResponse
    │
    └── MISS/BYPASS
          ↓
      rag_core_subgraph
          ↓
      construct_query
          ↓
      local_evidence_subgraph
          ├── Dense
          ├── BM25
          ├── Fusion
          ├── strategy-selected Reranker
          ├── EvidenceRef selection
          ├── Parent/Table context expansion
          └── strategy-selected local sufficiency
          ↓
      route_local_evidence
          ├── ANSWER
          ├── bounded RETRY_LOCAL
          └── WEB
                  ↓
            web_evidence_subgraph
            ├── SearXNG(original_query)
            ├── fetch + sanitize + budget
            └── strategy-stable build_web_evidence
                  ↓
          build_answer_input
          assign L*/W* citation IDs
                  ↓
          answer_subgraph
          ├── optional compression
          ├── DeepSeek generation
          ├── deterministic review
          ├── bounded regeneration if needed
          └── authoritative citation resolution
                  ↓
             FinalResponse
                  ↓
             RagCoreResult
                  ↓
          QueryCachePolicy
             ├── eligible + cache trusted → SET
             └── otherwise → skip
                  ↓
          transport envelope(trace_id, FinalResponse)
                  ↓
          release query lease
```

No raw answer tokens are published before the review/citation boundary.

---

# 98. Final Benchmark Flow — FINAL

```text
Frozen Open RAGBench snapshot
        │
        ├── FULL_CORPUS
        └── NEGATIVE_ONLY_CORPUS
        │
        ▼
Benchmark Adapter
        │
        ├── corpus → production Part I index_graph
        │
        └── cases/qrels/answers → Part III only
        │
        ▼
Isolated benchmark stores
        │
        ▼
production Part II retrieval_graph
        │
        ├── FinalResponse / RagCoreResult
        └── production Trace (100% capture)
                │
                ▼
Part III external join with gold/alignment
                │
        ┌───────┼────────────┐
        ▼       ▼            ▼
   Retrieval   Answer     Routing/Failure
   metrics     metrics       metrics
        │       │            │
        └───────┼────────────┘
                ▼
      validity/comparison checks
                ▼
       manifest + parquet/json
                ↓
             report.md
```

Formal quality mode:

```text
Web OFF
Session Query Cache OFF
Parent Context Cache OFF
```

Performance mode is separate and explicitly declares warm/cold/cache/hardware conditions.

---

# 99. Out of Scope — AtlasRAG v1

Explicitly deferred:

```text
multi-tenant / RBAC
Kubernetes/cloud autoscaling
GPU cluster/distributed model serving
complex micro-batching
GraphRAG / Knowledge Graph
Semantic Query Cache
cross-session Query Cache sharing
hot reindex / online blue-green serving
concurrent Part I + Part II
history-aware conversational retrieval / pronoun resolution
claim-level citation UI
complex Local/Web conflict-resolution agent
SQL/enterprise source connectors beyond current source scope
multimodal/image RAG
live production benchmark/evaluator dependencies
custom benchmark dashboard/platform
raw pre-review answer token streaming
LAN/public deployment/auth/reverse proxy
fully automatic BLOCKED→SERVING recovery controller
```

A session may contain multiple turns, but v1 does not inject prior transcript into RAG semantics.

---

# 100. Architecture Status — FINAL

```text
Part I   Knowledge Preparation & Index Construction
         FROZEN

Part II  Retrieval-Augmented Generation
         FROZEN

Part III External Verification & Benchmark Harness
         FROZEN

Part IV  Local Deployment & Operations
         FROZEN
```

All later implementation work must implement these contracts rather than silently redefining them.

If implementation discovers an architectural contradiction, the change must be handled explicitly as an architecture decision/update rather than hidden inside code.

---

# 101. Intentionally Deferred to the Implementation Guide

The architecture fixes responsibilities, invariants, boundaries and lifecycle semantics. The following belong to the implementation guide / stage plans unless already fixed above:

```text
exact repository/package tree
exact class/module/file names
exact Protocol/ABC class names
exact DI/composition-root implementation
exact localhost control transport
exact Docker Compose YAML/ports/volume names
exact SQL DDL and migration framework
exact Runtime ownership-lock mechanism
exact atomic RuntimeGate primitive
exact cache TTL/memory limits
exact top-k/fusion/reranker hyperparameters
exact GPU semaphore size
exact startup/drain/shutdown timeout values
exact log/tracing backend
exact backup file/container format
exact benchmark alignment algorithm thresholds
exact LLM benchmark repetition count
exact regression tolerances after baseline
exact Stage implementation order and Codex commands
```

These decisions may tune implementation but must preserve the frozen architecture.

---

# 102. Final Architecture Statement

AtlasRAG v1 is a production-oriented local RAG system whose core architecture is:

```text
Part I
Reliable knowledge ingestion
+ canonical PostgreSQL parsed/chunk data
+ hierarchical Child/Parent + Table evidence semantics
+ candidate revision lifecycle
+ exact-set verification
+ startup reconciliation
+ safe publish/delete/serving boundaries

Part II
Session-scoped exact Query Cache wrapper
+ query construction
+ hybrid Dense/BM25 retrieval
+ RRF
+ benchmark-selected reranking
+ Child→Parent/Table evidence conversion
+ benchmark-selected local evidence sufficiency
+ bounded corrective retrieval
+ Web corrective evidence
+ grounded generation
+ deterministic output review
+ authoritative citation resolution
+ explicit degradation/error contracts

Part III
External isolated Open RAGBench harness
+ real Part I/II execution
+ frozen dataset snapshots
+ hard-negative testing
+ benchmark-only gold alignment
+ deterministic retrieval/routing/citation metrics
+ optional pinned secondary answer evaluators
+ reproducible ablation/performance runs
+ run validity/comparison guards
+ baseline-driven regression gates

Part IV
WSL2-native single AtlasRAG runtime
+ Dockerized infrastructure
+ atomic serving/maintenance admission barrier
+ startup/crash reconciliation
+ persisted Query Cache freshness barrier
+ local CLI/Web thin clients
+ safe stage streaming
+ config/secrets/fingerprint discipline
+ canonical backup/restore semantics
+ health/readiness/observability separation
+ bounded RTX 3070 GPU concurrency
```

The defining architecture philosophy remains:

> **Evidence first, explicit ownership, reproducible state, bounded degradation, fail closed on canonical inconsistency, and no benchmark/deployment convenience may weaken Part I/II business correctness.**
