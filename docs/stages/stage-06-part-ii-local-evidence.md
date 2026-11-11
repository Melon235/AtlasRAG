# Stage 6 — Part II: Local Evidence

## 1. Status

**IMPLEMENTATION COMPLETE — PUBLICATION AND FINAL-HEAD REMOTE GATES PENDING.**

Stage 5 was merged and closed at
`82739b5d0311c4e121d8897a9d1b207f243646cc` (PR #6). Stage 6 is based on that
exact `origin/main` commit.

| Item | Value |
| --- | --- |
| Stage | Stage 6 — Part II: Local Evidence |
| Base | `origin/main` @ `82739b5d0311c4e121d8897a9d1b207f243646cc` |
| Branch | `stage/06-part-ii-local-evidence` |
| Database migration | **NONE** |
| RuntimeGate / SERVING lifecycle | **NOT IMPLEMENTED; Stage 10 owns it** |
| Stage 7 | **NOT STARTED** |

## 2. Delivered scope

One complete local evidence attempt:

```text
LocalRetrievalRequest
    ↓
ResolvedRetrievalScope (shared by both branches)
    ↓
Dense (retrieval_query) + BM25 (same retrieval_query/scope)
    ↓
RRF / surviving branch ordering
    ↓
CandidatePool
    ↓
Reranker (original_query; BGE baseline candidate)
    ↓
Unique Evidence Selection (rank-first child→parent/table identity)
    ↓
EvidenceRef
    ↓
PostgreSQL canonical Parent/Table expansion
    ↓
LocalEvidence
    ↓
Deterministic sufficiency baseline
    ↓
LocalEvidenceResult
```

Implemented modules:

```text
services/local_evidence/
  profile.py, ports.py, scope.py, retrieval.py, fusion.py,
  reranking.py, selection.py, parent_context.py, sufficiency.py, service.py
providers/reranker/
  base.py, bge.py
providers/index/milvus.py       additive dense/BM25 search
providers/index/models.py       validated scalar search hits
repositories/postgres/chunks.py additive batch canonical lookup
graphs/local_evidence/subgraph.py
application/local_evidence.py
```

The graph has exactly six thin nodes:

```text
retrieve_candidates → fuse_candidates → rerank_candidates
→ select_evidence → expand_context → assess_sufficiency
```

State remains the frozen minimal `LocalEvidenceState`. No clients, model
objects, vectors, repositories, retry controls, Web/Answer objects, or
dependencies are stored in State.

## 3. Approved frozen-contract amendment

Stage 3 legally produces nonempty whitespace-only canonical Children, and the
approved Stage 4 amendment indexes them losslessly. Frozen
`RetrievalCandidate.retrieval_text` originally required `value.strip() != ""`,
which made that legal Child unrepresentable at the retrieval boundary.

The user approved two narrow Stage 1 amendments:

```text
RetrievalCandidate.retrieval_text: nonempty + whitespace-preserving (len > 0)
LocalEvidence.content:             nonempty + whitespace-preserving (len > 0)
```

No other DTO or field changed; IDs remain nonblank, no trimming/substitution/
sentinel was added, and Stage 3 canonical facts and Stage 4 indexed
representation are unchanged. Regressions:
`tests/stage6/test_whitespace_cross_stage.py`, the real whitespace-Parent
expansion integration test, plus the existing Stage 3/4 whitespace tests.

## 4. Retrieval scope and published-current safety

`RetrievalScopeBuilder` flattens the frozen user filters and internal chunk
types into one `ResolvedRetrievalScope`. Both Dense and BM25 receive the exact
same compiled expression.

Allowed user fields:

```text
file_name    EQ
source_type  EQ
sheet_name   EQ via structured_metadata["provenance"]["sheet_name"]
chunk_type   IN (internal only)
```

Values are type-validated and JSON-literal escaped. No raw user text is
concatenated into a Milvus expression. The compiler is in
`services/local_evidence/scope.py`; tests cover quote/boolean injection.

`PublishedRevisionSnapshotService` reads PostgreSQL current revisions, validates
the current metadata slot/triple/revision derivation, and the compiler adds only
published `(document_id, revision_id)` pairs to the search filter. Search hits
are checked again against that snapshot; a non-current hit raises
`CanonicalDataError` rather than entering the candidate pool.

Stage 10 RuntimeGate/MaintenanceCoordinator is not implemented. Stage 6 does
not claim concurrent SERVING/MAINTENANCE isolation; real integration runs on a
safe published snapshot without concurrent maintenance. Global exact-set
verification remains Stage 5's responsibility and is not re-faked per query.

## 5. Dense / BM25 / RRF

- Dense uses the pinned Qwen3-Embedding-0.6B model and an instruction-aware
  query embedding (`Instruct: ...\nQuery: ...`), while document vectors remain
  the already-published raw-text embeddings. Query vectors are transient and
  never enter State.
- BM25 uses Milvus's built-in BM25 field/index; there is no second sparse
  provider. Whitespace-only Children are not dropped or token-faked.
- Branch contract is the frozen `RetrievalBranchResult`
  (`OK`/`EMPTY`/`UNAVAILABLE`). Technical provider unavailability is normalized
  at the provider boundary; malformed/canonical responses fail closed.
- RRF is deterministic over `chunk_id`, consumes ranks only, merges duplicate
  candidates after identity consistency checks, and tie-breaks by best branch
  rank then chunk_id. One surviving branch keeps its own ordering and does not
  receive a fake RRF score. Both unavailable → `UNAVAILABLE`; both empty →
  `EMPTY`/`INSUFFICIENT`, not unavailable.

## 6. Reranker baseline

`RerankerProvider` is provider-neutral for CrossEncoder/ColBERT/RankLLM.
The Stage 6 baseline candidate is the FlagEmbedding model
`BAAI/bge-reranker-v2-m3`:

```text
model revision:     953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e
tokenizer revision: 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e
runtime:            locked Transformers/torch, explicit cuda:0, float16, SDPA
output:             bounded sigmoid score, finite check, no CPU fallback
```

It is loaded through the existing locked model runtime rather than adding a
second FlagEmbedding package. `original_query` is used for reranking, not
`retrieval_query`. Runtime/tokenizer/OOM/non-finite failures are normalized to
`RerankerUnavailableError` at the provider boundary; the service preserves the
previous ordering and sets `degraded=True`. It is not a whole-attempt failure.
Reranking remains benchmark-selected work for Stage 9.

## 7. Evidence selection and expansion

Unique selection walks ranked candidates, maps TEXT_CHILD → Parent and TABLE →
itself, and deduplicates by:

```text
(document_id, revision_id, evidence_type, context_id)
```

It stops only after N unique evidence units; it never truncates the first N
Children before deduplication. `EvidenceRef` contains only the frozen identity
fields (no child ID, content, dense/BM25/RRF/rerank score, or citation ID).

`ParentContextService` batch-reads canonical chunks from PostgreSQL and validates
document/revision/current, chunk type, parent ownership, and canonical mapping.
Missing, wrong-type, cross-revision, or non-current Parents raise
`CanonicalDataError`; nothing is silently substituted. `LocalEvidence.content`
is the complete canonical Parent/Table content. No Redis/cache/citation ID is
introduced.

## 8. Sufficiency and result semantics

`LocalEvidenceSufficiencyStrategy` is the stable interface. The Stage 6
baseline is deterministic and configurable (`DeterministicEvidenceSufficiency`):

- reranker ordering: score ≥ sufficient threshold → SUFFICIENT; score ≤
  ambiguous threshold → INSUFFICIENT with one best available; between → AMBIGUOUS;
- no successful reranker ordering: RRF score ≥ 0.03 → SUFFICIENT; otherwise
  conservative INSUFFICIENT with best available. A single top-ranked branch
  (1/61 ≈ 0.0164) is not enough; never merely `len(evidence) > 0`;
- no branches → UNAVAILABLE with no evidence/status.

`execution_status` and `evidence_status` remain independent: e.g. one branch
unavailable with sufficient evidence is `DEGRADED + SUFFICIENT`. LLM-based
sufficiency is intentionally deferred to a future benchmarked comparison; Stage
9 owns strategy selection.

## 9. Failure/degradation matrix

| Situation | CandidatePool | LocalEvidenceResult |
| --- | --- | --- |
| Dense OK + BM25 OK | HYBRID/RRF | OK + evidence status |
| Dense UNAVAILABLE + BM25 OK | BM25_ONLY/degraded | DEGRADED + evidence status |
| BM25 UNAVAILABLE + Dense OK | DENSE_ONLY/degraded | DEGRADED + evidence status |
| both UNAVAILABLE | UNAVAILABLE | UNAVAILABLE, no evidence |
| both EMPTY | HYBRID/empty | OK + INSUFFICIENT |
| one UNAVAILABLE + other EMPTY | surviving mode/degraded | DEGRADED + INSUFFICIENT |
| reranker failure | previous ordering/degraded | DEGRADED + evidence status |
| published/canonical mismatch | fail closed | technical error, not a normal branch |

## 10. Verification

Final local gates on the Stage 6 tree:

| Gate | Result |
| --- | --- |
| `make verify` | **PASS** — 3144 passed, 101 deselected in 135.79s; Ruff, mypy 256 files, architecture checks clean |
| `make test-stage6` | **PASS** — 24 focused tests |
| `make stage6-verify` | **PASS** — 3144 offline + 24 focused + 8 real PostgreSQL/Milvus tests in 217.68s |
| `make stage2-verify` | **PASS** — 28 real integration tests in 152.45s |
| `make stage3-verify` | **PASS** — 529 focused in 5.92s + 18 real PostgreSQL/Docling tests in 8.20s |
| `make stage3-parser-smoke` | **PASS** — real MinerU 4.0.11, 1 page / 7 elements in 3.12s |
| `make stage4-verify` | **PASS** — 654 focused in 28.73s + 36 real PostgreSQL/Milvus tests in 848.83s |
| `make stage5-verify` | **PASS** — 75 focused in 1.74s + 8 real PostgreSQL/Milvus tests in 574.53s |
| `make stage4-model-smoke` | **PASS** — real Qwen, 1 passed in 32.78s |
| `make stage6-model-smoke` | **PASS** — real Qwen query + BGE reranker, 1 passed in 40.92s |
| `make architecture-check` / `git diff --check` | **PASS** |

Baseline before implementation: **3112 passed, 92 deselected** at `82739b5`.

Real Stage 6 integration covers: hybrid Dense+BM25; Parent evidence; TABLE
evidence and file_name scope; missing file → INSUFFICIENT; source_type and
sheet_name filter compilation; reranker fallback; missing canonical Parent →
fail closed; whitespace-only Parent expansion; corrupt current parse metadata
→ fail closed. Unit tests cover every branch failure combination, injection,
RRF identity/tie determinism, whitespace preservation, reranker query/fallback,
unique-parent diversity, EvidenceRef shape, sufficiency bands, frozen State,
and graph topology.

## 11. Model verification

```text
query embedding:
  model          Qwen/Qwen3-Embedding-0.6B
  revision       97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3
  tokenizer      same pinned snapshot
  device         cuda:0
  dtype          float16 / SDPA
  dimension      1024
  checks         finite, L2-normalized, instruction-aware query path

reranker:
  model          BAAI/bge-reranker-v2-m3
  revision       953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e
  tokenizer      same pinned snapshot
  device         cuda:0
  dtype          float16
  checks         finite scores, relevant > irrelevant, no CPU fallback
```

No model weights, cached paths, `.env` values, or sensitive query content are
tracked.

## 12. Reviews

- **Architecture review:** PASS after the approved narrow amendment. Dense and
  BM25 share one scope; query roles are correct; EvidenceRef has no child
  fields; Parent expansion is canonical; the graph has no retry/Web/Answer/
  RuntimeGate behavior; Stage 7/8 are absent.
- **Security/release review:** PASS. Allowlisted filters with escaped literals;
  published-current/filter isolation fail-closed; no arbitrary Milvus
  expression; no silent evidence truncation; no citation IDs; no dependencies
  in State; no secrets/model files.
- Independent read-only audit result: see §14.

## 13. Deferred work

No Query Construction, RagCore retry/routing, Web evidence, Answer generation,
citation resolution, Query Cache, RuntimeGate/MAINTENANCE lifecycle, health
registry, production scheduler, CLI/server, Stage 7/8 orchestration, or
benchmark harness. Reranker/sufficiency strategy comparison and LLM
sufficiency remain Stage 9 work.

## 14. Task ledger

| Task | Status |
| --- | --- |
| T0 baseline, scope, design, approved amendments | COMPLETE |
| T1 ResolvedRetrievalScope + safe filter compiler | COMPLETE |
| T2 Dense retrieval + Qwen query embedding | COMPLETE |
| T3 BM25 retrieval + shared scope | COMPLETE |
| T4 branch results, CandidatePool, RRF | COMPLETE |
| T5 RerankerProvider + pinned BGE baseline | COMPLETE |
| T6 Unique Evidence Selection | COMPLETE |
| T7 ParentContextService + LocalEvidence | COMPLETE |
| T8 sufficiency strategy + LocalEvidenceResult | COMPLETE |
| T9 local_evidence_subgraph + runner | COMPLETE |
| T10 real Milvus/model integration + fault tests | COMPLETE |
| T11 CI, final gates, report, push, PR | IN PROGRESS |

## 15. Independent audit

A read-only audit found three blockers and two important issues before
publication. All were corrected:

- **B1 — whitespace Parent:** the audit reproduced a legal Stage 3
  whitespace-only Parent (real chunker, 8000 spaces) that `LocalEvidence.content`
  rejected. The user approved the narrow content amendment above; real
  whitespace-Parent expansion now passes.
- **B2 — real reranker fallback:** BGE runtime/tokenizer/OOM errors escaped as
  `RuntimeError`. They are now normalized to `RerankerUnavailableError` and the
  explicit degraded fallback is exercised by tests.
- **B3 — real Dense degradation:** Qwen runtime/output errors aborted the
  attempt. Dense now returns `UNAVAILABLE` and BM25-only survives; tested with
  a real `EmbeddingOutputError`.
- **I1 — score loss:** RRF now retains both dense and BM25 scores for
  overlapping candidates; test asserts both.
- **I2 — unreachable RRF threshold:** default threshold is now 0.03; a single
  top-ranked branch (1/61) is INSUFFICIENT. Test added.
- **Coverage:** corrupt current parse metadata, whitespace Parent expansion,
  provider failure normalization, BM25 query usage, and original-query
  reranking tests were added.

**Architecture verdict: READY.** **Security/release verdict: READY** — no
filter injection, published-current bypass, child-field leakage, silent
evidence truncation, citation ID, secret, or arbitrary expression remains.

## 16. Git and publication

_To be completed after the final commits, normal push, PR creation, and
final-HEAD remote CI/Integration checks. Stage 6 PR must remain unmerged; Stage
7 must not start._
