# Stage 9 — Part III: Benchmark Harness

## 1. Status

**PARTIAL — core dataset, snapshot, alignment, metric, manifest, and quality
adapter scaffolding implemented and tested. FORMAL EXTERNAL BENCHMARK: BLOCKED.**

Base: `bda75fefb70fb1dc61a527d988e3b362bd329d01` (Stage 8 merge).

Stage 8 PR #9 was merged as `bda75fefb70fb1dc61a527d988e3b362bd329d01`;
main CI and Stage 2–8 Integration passed; local main synchronized. Stage 8 is
CLOSED. Stage 9 is external tooling under `benchmarks/`; production never imports
it, and no benchmark/gold fields were added to production DTOs or State.

## 2. Dataset identity and formal blocker

Verified from the official metadata at HF revision
`63f6b052ff83508b08e242db42263ee708815c26`:

- Dataset: `vectara/open_ragbench`
- License: **CC-BY-NC-4.0**
- Total queries: 3,045
- Supported `text` + `text-table`: **2,062**
- Excluded `text-image` + `text-table-image`: 983
- Exact corpus identities in `pdf_urls.json`: **1,000 unique**
- `qrels` distinct referenced document IDs: **396**
- Corpus documents not referenced by qrels: **604**
- Verified hard-negative IDs: **UNRESOLVED / NOT AVAILABLE**

The upstream documentation describes **400 positive and 600 hard-negative
documents**. The pinned release contains no authoritative positive/
hard-negative partition file. It is therefore not established that the 604
unreferenced documents are exactly the 600 documented hard negatives, nor which
four documents account for the difference. The official NEGATIVE_ONLY_CORPUS
profile fails closed instead of treating unreferenced documents as certified
negatives. This conflict remains unresolved; no formal FULL_CORPUS or
NEGATIVE_ONLY scorecard is claimed.

The dataset is licensed CC-BY-NC-4.0. Dataset files, corpus content, PDFs, and
restricted reference answers are not committed or redistributed by this
repository. No claim is made that the project MIT license applies to the data.

Full 1,000-PDF ingestion and thousands of LLM-backed cases also require a
separate resource/cost approval on the WSL2/RTX 3070 profile. No full run was
started.

## 3. Implemented harness

- Benchmark-only contracts: documents, queries, qrels, modalities, corpus
  profiles, alignment, ranked units, snapshot manifest.
- Open RAGBench adapter with strict query/qrel/answer identity checks,
  exact 1,000-document `pdf_urls.json` identity validation, and modality
  filtering. `DocumentPartition` explicitly separates qrel-referenced IDs,
  unreferenced IDs, and verified hard-negative IDs; the last is unavailable and
  NEGATIVE_ONLY is blocked.
- Immutable snapshot manifest: SHA256/size per file, dataset revision, license,
  counts; rejects missing/extra files, checksum/size mismatch, duplicate paths,
  unsafe paths, unexpected/symlinked files, manifest count mismatches, and
  invalid qrel mappings.
- Deterministic metrics: Hit@K, Recall@K, MRR@K, nDCG@K, binary/graded
  relevance, deduplicated ranked identity, document-level collapsing.
- Gold evidence alignment: document identity gate, deterministic token-overlap
  score, HIGH/MEDIUM/UNRESOLVED, one-to-many Parent/Table alignment, coverage.
- Static physical-isolation checks: benchmark database/collection naming,
  Redis DB not 0, production knowledge/artifact path overlap rejection, and port
  availability helper. These are preflight checks only; actual database,
  collection, Compose ownership, and no-concurrent-mutation checks are not yet
  implemented or verified.
- Quality-case adapter around an injected retrieval callable, with independent
  benchmark sessions and no gold in production input. It is not yet wired to the
  real `retrieval_graph`, Web/cache policy enforcement, or production trace
  observation; tests use an injected fake callable.
- Append-only case ledger, validity assessment, run manifest, run comparison
  validation with declared-factor checks.
- Minimal report renderer and non-interactive CLI:
  `validate-snapshot`, `profile`.

## 4. Verification

- Baseline before Stage 9 implementation: 3226 passed / 106 deselected.
- `make verify`: **3244 passed, 106 deselected**.
- `make test-stage9`: **18 passed**.
- Local metadata snapshot (not committed; CC-BY-NC-4.0) was checksum-validated
  locally. CLI output:
  - revision `63f6b052ff83508b08e242db42263ee708815c26`
  - 1,000 exact corpus identities, 3,045 queries, 2,062 supported
  - 396 qrel-referenced documents, 604 unreferenced documents
  - verified hard-negative IDs: unavailable
  - This local snapshot is not independently reproducible from the repository
    without downloading the licensed data.
- `make architecture-check`, Ruff, mypy, git diff checks: PASS.
- Stage 9 CI job added for synthetic harness tests; no dataset/API/model download
  in ordinary CI.

## 5. Not completed / blocked

The following are **not implemented or not accepted** and are required before a
formal Stage 9 benchmark claim:

- Real Part I ingestion runner/readiness gate using MinerU/Qwen/Milvus for the
  Open RAGBench corpus.
- Production trace observation capture for dense/BM25/fusion/rerank/evidence/
  routing facts.
- Full quality/negative/performance runners and artifact schemas.
- BEIR auxiliary evaluation and DeepEval secondary evaluator execution.
- Reranker, sufficiency, Web representation, and compression experiments.
- Real Open RAGBench pilot, formal FULL/NEGATIVE runs, performance runs, and
  strategy comparisons.
- Approved baseline/regression thresholds.

The 396/604 unreferenced partition conflict and uncapped full-corpus resource
cost are stop conditions. No benchmark quality/latency numbers are claimed.

## 6. Safety

- No production import of `benchmarks`.
- No benchmark/gold fields in production State/DTOs.
- No gold/reference answers passed to online generation.
- No production data, user knowledge directory, containers, volumes, or Redis
  data modified.
- No Stage 10 RuntimeGate/MaintenanceCoordinator implementation.
- Stage 9 PR remains open and unmerged.

## 7. Publication and CI

- Implementation HEAD: `36d1f0f45481a05ef2d6a8711ba21fd3ff29257e`
- Push CI `44069058171`: PASS
- Push Integration `44069058185`: PASS
- PR CI `44069065725`: PASS
- PR Integration `44069065746`: PASS
- PR: open / unmerged.
- This report is a documentation-only successor; its final HEAD checks are
  recorded in the PR/final delivery.
- Independent read-only review found no production-coupling or Stage-scope
  blocker; its snapshot/comparison/isolation/validity/metrics findings were
  fixed or the claims were corrected to partial.

## 8. Next acceptance

1. Provide the authoritative 400/600 positive/hard-negative ID partition, or
   explicitly approve a different profile derived and documented from the
   pinned release. Do not silently relabel the 604 unreferenced IDs.
2. Approve resource budget for pilot ingestion and LLM-backed evaluation.
3. Implement/run real Part I ingestion and readiness gate.
4. Build the full observation adapter and quality/negative/performance runners.
5. Execute a real Open RAGBench pilot, then formal runs and experiments.
