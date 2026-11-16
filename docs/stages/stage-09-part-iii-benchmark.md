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
- `qrels` unique positive documents: **396**
- Negative documents implied by 1,000-document corpus: **604**

The Architecture Manual itself states 1000 documents and 2062 supported
queries; it does not state 400/600. The 400/600 split appears in the Stage 9
design plan and must be treated as a profile assumption. The actual frozen
metadata yields 396/604. This is a dataset-identity conflict against that
assumption, not a result to round or hide. Until the user accepts the corrected
profile or supplies the original snapshot, a formal `FULL_CORPUS` scorecard is
blocked.

The dataset is licensed CC-BY-NC-4.0. Dataset files, corpus content, PDFs, and
restricted reference answers are not committed or redistributed by this
repository. No claim is made that the project MIT license applies to the data.

Full 1,000-PDF ingestion and thousands of LLM-backed cases also require a
separate resource/cost approval on the WSL2/RTX 3070 profile. No full run was
started.

## 3. Implemented harness

- Benchmark-only contracts: documents, queries, qrels, modalities, corpus
  profiles, alignment, ranked units, snapshot manifest.
- Open RAGBench adapter with strict query/qrel/answer identity checks and
  modality filtering; profile counts are derived from qrels and the manifest.
  It does not yet materialize a complete document identity/membership set.
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
- `make verify`: **3243 passed, 106 deselected**.
- `make test-stage9`: **17 passed**.
- Local metadata snapshot (not committed; CC-BY-NC-4.0) was checksum-validated
  locally. CLI output:
  - revision `63f6b052ff83508b08e242db42263ee708815c26`
  - 1000 documents, 3045 queries, 2062 supported
  - positive documents 396, negative documents 604
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

The 396/604 dataset conflict and uncapped full-corpus resource cost are stop
conditions. No benchmark quality/latency numbers are claimed.

## 6. Safety

- No production import of `benchmarks`.
- No benchmark/gold fields in production State/DTOs.
- No gold/reference answers passed to online generation.
- No production data, user knowledge directory, containers, volumes, or Redis
  data modified.
- No Stage 10 RuntimeGate/MaintenanceCoordinator implementation.
- Stage 9 PR remains open and unmerged.

## 7. Next acceptance

1. Approve or provide the frozen 400/600 corpus reference (or explicitly accept
   396/604 as the corrected official snapshot identity).
2. Approve resource budget for pilot ingestion and LLM-backed evaluation.
3. Implement/run real Part I ingestion and readiness gate.
4. Build the full observation adapter and quality/negative/performance runners.
5. Execute a real Open RAGBench pilot, then formal runs and experiments.
