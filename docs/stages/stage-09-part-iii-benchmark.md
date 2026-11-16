# Stage 9 — Part III: Benchmark Harness

## 1. Status

**HARNESS IMPLEMENTATION: COMPLETE (core static/snapshot/alignment/metrics/run
harness). FORMAL EXTERNAL BENCHMARK: BLOCKED.**

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

The Architecture Manual reference profile states 400 positive and 600 hard
negative documents. The actual frozen metadata yields 396/604. This is a
dataset-identity conflict, not a result to round or hide. Until the user accepts
this reference correction or supplies the original 400/600 snapshot, a formal
`FULL_CORPUS` scorecard is blocked.

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
  modality filtering; corpus profiles retain all reference documents and derive
  negative-only membership from qrels.
- Immutable snapshot manifest: SHA256/size per file, dataset revision, license,
  counts; rejects missing/extra files, checksum/size mismatch, duplicate paths,
  unsafe paths, and invalid qrel mappings.
- Deterministic metrics: Hit@K, Recall@K, MRR@K, nDCG@K, binary/graded
  relevance, deduplicated ranked identity, document-level collapsing.
- Gold evidence alignment: document identity gate, deterministic token-overlap
  score, HIGH/MEDIUM/UNRESOLVED, one-to-many Parent/Table alignment, coverage.
- Physical-isolation checks: benchmark database/collection naming, Redis DB not
  0, ownership markers, production knowledge-path overlap rejection, port
  availability helper.
- Quality-case adapter around an injected real production `retrieval_graph`
  callable, with independent benchmark sessions and no gold in production input.
- Append-only case ledger, validity assessment, run manifest, run comparison
  validation with declared-factor checks.
- Minimal report renderer and non-interactive CLI:
  `validate-snapshot`, `profile`.

## 4. Verification

- Baseline before Stage 9 implementation: 3226 passed / 106 deselected.
- `make verify`: **3236 passed, 106 deselected**.
- `make test-stage9`: **10 passed**.
- Actual metadata snapshot: checksum-validated; CLI output:
  - revision `63f6b052ff83508b08e242db42263ee708815c26`
  - 1000 documents, 3045 queries, 2062 supported
  - positive documents 396, negative documents 604
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
