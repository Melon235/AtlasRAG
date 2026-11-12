# AtlasRAG Stage 6 — Part II: Local Evidence

## 1. Status

**IMPLEMENTATION COMPLETE — PUBLICATION PENDING.**

Stage 5 is closed at `82739b5d0311c4e121d8897a9d1b207f243646cc`. Stage 6
baseline on a fresh worktree: **3112 passed, 92 deselected** (`make verify`,
137.63s).

The user approved the two narrow Stage 1 amendments described in §4. The design
was implemented, independently audited, corrected, and verified; the
evidence-bearing report is
[the Stage 6 report](../../stages/stage-06-part-ii-local-evidence.md).

## 2. Authority and scope

Authority: tracked Architecture Manual, frozen Stage 1 Part II DTO/State
contracts (§46A), frozen Stage 2–5 storage/index/publish/maintenance contracts,
and this Stage's approved scope.

Stage 6 implements exactly one `local_evidence_subgraph` invocation:

```text
LocalRetrievalRequest
    ↓
ResolvedRetrievalScope
    ↓
Dense + BM25 (same scope)
    ↓
RRF / surviving-branch ordering
    ↓
CandidatePool
    ↓
Reranking strategy (baseline CrossEncoder)
    ↓
Unique Evidence Selection
    ↓
EvidenceRef
    ↓
Parent/Table expansion from PostgreSQL
    ↓
LocalEvidence
    ↓
Sufficiency strategy
    ↓
LocalEvidenceResult
```

No Query Construction, RagCore routing/retry, Web, Answer, citation resolution,
Query Cache, RuntimeGate, CLI, benchmark harness, or Stage 7 work.

## 3. Reused frozen contracts

The Stage 1 contracts already exist and must be reused unchanged:

```text
LocalRetrievalRequest, QueryFilters, ResolvedRetrievalScope
RetrievalCandidate, CandidateScores, RetrievalBranchResult, CandidatePool
EvidenceRef, LocalEvidence, LocalEvidenceResult
LocalEvidenceSubgraphInput/State/Output
RetrievalBranch, RetrievalBranchStatus, RetrievalMode, CandidateOrdering
LocalExecutionStatus, LocalEvidenceStatus, EvidenceType
```

No second candidate/evidence DTO will be introduced.

## 4. Resolved frozen-contract conflicts (approved amendments)

Stage 3 legally produces canonical `TEXT_CHILD` and `TEXT_PARENT` values whose
`raw_content` is whitespace-only but nonempty, with complete source-backed
lineage. The approved Stage 4 amendment indexes Children losslessly; the real
Stage 4 Milvus test proves admission. Stage 6 initially hit two frozen Stage 1
admission rules that rejected these legal values:

1. `RetrievalCandidate.retrieval_text` used `value.strip() != ""`.
2. `LocalEvidence.content` used `value.strip() != ""`, which Parent expansion
   would hit for a legal whitespace-only Parent.

Dropping, trimming, substituting Parent/Child text, sentinel injection, or
bypassing validation were all rejected as violations of canonical facts and the
Child-retrieval/Parent-evidence boundary.

The user approved two narrow Stage 1 amendments:

```text
RetrievalCandidate.retrieval_text: nonempty + whitespace-preserving (len > 0)
LocalEvidence.content:             nonempty + whitespace-preserving (len > 0)
```

No other DTO or field changed. All identity fields remain nonblank; Stage 3
canonical facts and Stage 4 index semantics are unchanged. Regressions:

- `tests/stage6/test_whitespace_cross_stage.py` — exact Child text preserved;
- `tests/integration/test_stage6_local_evidence.py` — real whitespace Parent
  expands without truncation;
- updated Stage 1 contract tests for both fields.

## 5. Implemented architecture

### 5.1 Module layout

```text
services/local_evidence/
  __init__.py
  profile.py          immutable top-k/RRF/reranker/sufficiency profiles
  ports.py            search, query-embedding, UoW protocols
  scope.py            ResolvedRetrievalScope builder, safe filter compiler,
                      PublishedRevisionSnapshot
  retrieval.py        DenseRetriever, BM25Retriever, shared-scope hybrid service
  fusion.py           ReciprocalRankFusion
  reranking.py        RerankerService
  selection.py        UniqueEvidenceSelector
  parent_context.py   ParentContextService
  sufficiency.py      deterministic baseline strategy
  service.py          injected Stage 6 business services

providers/reranker/
  __init__.py
  base.py             RerankerProvider protocol + identity
  bge.py              pinned BAAI/bge-reranker-v2-m3 provider
providers/index/milvus.py  additive dense/BM25 search methods
repositories/postgres/chunks.py  additive batch chunk lookup
graphs/local_evidence/
  __init__.py
  subgraph.py         thin six-node local_evidence_subgraph
application/local_evidence.py  injected runner for tests/composition
```

### 5.2 Scope and filter safety

`RetrievalScopeBuilder` flattens frozen `QueryFilters` plus internal chunk
types. `FilterCompiler` accepts only:

```text
file_name    EQ
source_type  EQ
sheet_name   EQ (JSON provenance path)
chunk_type   IN
```

and internally published revision pairs `(document_id, revision_id)` only for
published-current isolation. No raw user string is concatenated. Values are
validated against frozen types and safely escaped (JSON-style literal). Dense
and BM25 consume the same compiled expression/published set.

`PublishedRevisionSnapshotService` reads PostgreSQL current revisions and
validates the current metadata slot/triple. Search results must match that
snapshot; non-current/building/orphan hits are a canonical isolation failure,
never silently returned.

### 5.3 Dense/BM25/query embedding

`DenseRetriever` uses `Qwen3-Embedding-0.6B` query embedding with the pinned
model/tokenizer revision and the official query instruction template; document
embeddings remain unchanged. Query vectors are transient and never enter State.

`BM25Retriever` uses Milvus built-in BM25 with the same scope. Whitespace-only
canonical Children remain legal and are never dropped/token-faked; the BM25
analyzer may yield no lexical terms, which is an EMPTY-capability outcome for
that record, not a canonical change.

Branch results use the frozen `RetrievalBranchResult`:
`OK`/`EMPTY`/`UNAVAILABLE`. Dependency/SDK failures map to `UNAVAILABLE`;
canonical corruption and malformed published state fail closed.

### 5.4 Fusion and reranking

RRF is deterministic over `chunk_id`, profile-bound `k`, with stable tie-break
by branch rank then chunk_id. Both branches available → HYBRID/RRF; one
unavailable → surviving branch ordering/mode; both unavailable → UNAVAILABLE.
No fake RRF scores.

`RerankerProvider` abstracts CrossEncoder/ColBERT/RankLLM. Baseline candidate:
the FlagEmbedding `BAAI/bge-reranker-v2-m3` model at the resolved revision
`953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`, loaded through the already-locked
Transformers/torch runtime (no extra FlagEmbedding runtime package), explicit
CUDA device, float16, no silent CPU fallback. Query is `original_query`;
passages are candidate retrieval texts. Failure preserves previous ordering
and sets `degraded=True`. Reranker selection remains benchmark-selected; Stage
9 owns formal comparison.

### 5.5 Evidence and sufficiency

Unique selection walks all ranked candidates, maps TEXT_CHILD → Parent and
TABLE → itself, deduplicates by
`(document_id, revision_id, evidence_type, context_id)`, and stops after N
unique evidence units. No Top-N child truncation before deduplication.

ParentContextService batch-reads canonical Parent/Table rows from PostgreSQL,
validates published-current revision/type/ownership/lineage, and returns
complete `LocalEvidence` content/provenance. No Redis, no truncation, no
runtime citation IDs.

Sufficiency baseline is deterministic and configurable, using reranker score
bands when available and RRF signals otherwise. The default RRF threshold is
0.03, so a single top-ranked branch (1/61 ≈ 0.0164) is INSUFFICIENT rather than
SUFFICIENT; never merely `len(evidence)>0`. It emits
SUFFICIENT/INSUFFICIENT/AMBIGUOUS and records an ablation requirement for a
future LLM strategy; Stage 9 owns selection.

### 5.6 Graph and tests

Six thin nodes:

```text
retrieve_candidates → fuse_candidates → rerank_candidates
→ select_evidence → expand_context → assess_sufficiency
```

State stays the frozen minimal `LocalEvidenceState`; no providers, vectors,
connections, reranker objects, retries, Web, or generation.

### 5.7 Independent audit corrections

The read-only Stage 6 audit found and the implementation now fixes:

- legal whitespace-only Parent expansion (approved `LocalEvidence.content`
  amendment above);
- real BGE runtime/tokenizer/OOM failures normalized to
  `RerankerUnavailableError`, reaching the explicit degraded fallback;
- real Qwen query-embedding output/runtime failures degrade Dense to
  `UNAVAILABLE` so BM25 can survive;
- RRF retains both dense and BM25 scores for overlapping candidates;
- RRF sufficiency threshold 0.03 (single top rank is insufficient);
- tests for corrupt current parse metadata, whitespace Parent expansion,
  provider failure normalization, BM25 query usage, and original-query reranking.

Tests cover all branch failure/degradation combinations, filter injection,
same-scope use, published-current isolation, whitespace Child admission (after
the amendment), RRF identity/tie determinism, original-query reranking,
reranker fallback, unique parent diversity, TABLE preservation, canonical
Parent failures, complete evidence content, sufficiency semantics, and no
dependencies in State. Real integration uses PostgreSQL + Milvus + real
Stage 3–5 ingestion with fake embedding/reranker; separate model smokes run
real Qwen query embedding and the real baseline reranker on CUDA.

## 6. Decision resolution

The user approved option 1: the narrow `RetrievalCandidate.retrieval_text`
amendment (nonempty + whitespace-preserving, no other DTO/field changes).
Implementation proceeded and all Stage 6/local/regression/model gates pass.
