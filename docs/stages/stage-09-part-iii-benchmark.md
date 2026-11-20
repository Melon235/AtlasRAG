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
- Static physical-isolation checks plus PostgreSQL/Redis/Milvus identity probes:
  benchmark database/collection naming, Redis DB not 0, production knowledge/
  artifact overlap rejection, port availability, and live identity comparison
  helpers. These are preflight checks only; Compose-volume ownership and
  no-concurrent-mutation guarantees are not yet implemented or verified.
- Quality-case/run adapters around an injected retrieval callable, independent
  benchmark sessions, and repetitions.
- `benchmarks/wiring/quality.py` composes the real production
  RetrievalRunner/RagCore/LocalEvidence services with a disabled Web boundary
  and disabled Query Cache, and rejects non-bypassed Parent Cache configuration.
  A non-interactive `run-quality` command over this factory is not yet accepted.
- Benchmark-owned ingestion orchestration over an injected production
  `BuildRequest -> BuildResult` backend; it preserves Part I parsing/persistence/
  indexing/publication by delegation and never inserts gold/vectors directly.
  It validates the exact on-disk source set, accounts for per-document
  `READY`/failure/technical-failure outcomes, and never retries silently.
- A production composition helper creates the real `DocumentBuildRunner` with
  caller-supplied initialized Part I resources; the actual synthetic test
  executes the real Stage 5 sync/build entrypoint against PostgreSQL/Milvus.
- Corpus Readiness Gate requiring exact source sets, complete ingestion,
  canonical lineage, Milvus exact-set verification, fresh Stage 5
  `SAFE_TO_SERVE`, compatible pipeline fingerprint, snapshot identity,
  verification execution ID, and evidence digest. The production readiness
  collector performs a fresh authoritative verification call and validates each
  expected document's READY state, current revision identity, content/pipeline
  fingerprints, and exact source set.
- Optional generic production observer boundary with a no-op default:
  Dense/BM25 candidate identities/ranks/stage-specific scores, RRF fusion
  ordering, reranker ordering, selected Parent/Table identities, sufficiency
  status, corrective routing, answer outcome/citations, latency and degradation.
  Events carry a context-local production invocation ID and local attempt index;
  no benchmark IDs, qrels, gold labels, or reference answers are present.
  Dense and BM25 timings are branch-measured, not copied from a shared hybrid
  duration. Fusion uses the authoritative fusion score; reranker uses the
  rerank score or rank-only `None`. Terminal SAFE_FAILURE records the RagCore
  state's terminal local attempt. Observer delivery failures are non-fatal to
  the product and emit a diagnostic warning.
- A forced Web-fallback regression exercises the disabled Web boundary and
  disabled Query Cache with zero external HTTP and zero Redis cache calls.
- Synthetic integration now binds each `run_quality()` case by its returned
  invocation ID, writes sealed per-case artifacts under true exclusive
  `flock` check-and-append (with a multiprocessing same-key regression),
  rejects duplicate case keys, validates causal stage order and terminal attempt
  consistency, and replays Dense/BM25/Fusion/Reranker document metrics from
  reloaded artifacts without production calls.
- Replay supports FIRST_LOCAL_ATTEMPT, FINAL_LOCAL_ATTEMPT and PER_ATTEMPT
  selection and records the selected view. Multi-attempt tests reject silent
  merging.
- Artifact validation enforces causal stage order and routing semantics:
  RETRY_LOCAL only on non-terminal attempts, terminal routing cannot retry,
  terminal ANSWER follows the final routing observation, and attempt view is
  validated against the supported enum. Replay rejects a conflicting declared
  view unless alternate analysis is explicitly requested.
- Atomic writer validates the new artifact before sealing, holds an exclusive
  file lock while checking duplicate case keys, validates existing records,
  flushes/fsyncs append, and releases the lock reliably.
- Synthetic real-storage integration (`SYNTHETIC_INTEGRATION`, not Open
  RAGBench): real PostgreSQL/Milvus/Redis, real Stage 5 sync/readiness, real
  local retrieval and `retrieval_graph`, disabled Query Cache with zero GET/SET,
  fake Web with zero calls, fake generation, gold alignment, deterministic
  metrics, and case/trace artifacts. PASS.
- Append-only case ledger, validity assessment, run manifest, run comparison
  validation with declared-factor checks.
- Minimal report renderer and non-interactive CLI:
  `validate-snapshot`, `profile`.

## 4. Verification

- Baseline before Stage 9 implementation: 3226 passed / 106 deselected.
- `make verify`: **3270 passed, 107 deselected**.
- `make test-stage9`: **44 passed**.
- Local metadata snapshot (not committed; CC-BY-NC-4.0) was checksum-validated
  locally. CLI output:
  - revision `63f6b052ff83508b08e242db42263ee708815c26`
  - 1,000 exact corpus identities, 3,045 queries, 2,062 supported
  - 396 qrel-referenced documents, 604 unreferenced documents
  - verified hard-negative IDs: unavailable
  - This local snapshot is not independently reproducible from the repository
    without downloading the licensed data.
- `make architecture-check`, Ruff, mypy, git diff checks: PASS.
- Synthetic isolated storage integration: **1 passed** (real PostgreSQL/Milvus/
  Redis; labeled SYNTHETIC_INTEGRATION), including metric replay from stored
  ranked observations.
- Stage 9 CI job added for synthetic harness tests; no dataset/API/model download
  in ordinary CI.

## 4A. Resource/cost preflight proposal

No Open RAGBench ingestion or paid evaluation has started. A bounded pilot would
be selected deterministically from the pinned metadata: the first five sorted
qrel-positive document/query pairs and five sorted unreferenced documents, with
the associated supported queries only; this is a pilot, never a scorecard.

Planning caps to require approval before execution:

```text
documents: <= 10 PDFs
queries:   <= 10 supported queries
DeepSeek:  <= 40 total calls (rewrite + generation + bounded review)
tokens:    <= 100,000 input+output combined
wall time: <= 2 hours
storage:   measured after pilot, not assumed
```

Model/device proposal: existing pinned Qwen3-Embedding-0.6B, BGE reranker, and
DeepSeek provider on the local RTX 3070 8GB profile; no challenger downloads.
Estimated disk, VRAM, and provider cost must be measured from this pilot before
any full-corpus or paid run. Isolated alternate host ports, dedicated
PostgreSQL/Milvus/Redis identities, and benchmark-only directories are required.
Full 1,000-PDF and 2,062-query execution is explicitly not approved.

## 5. Not completed / blocked

The following are **not implemented or not accepted** and are required before a
formal Stage 9 benchmark claim:

- Real Open RAGBench Part I ingestion runner/readiness execution using
  MinerU/Qwen/Milvus; only synthetic real-storage ingestion is accepted.
- Production observation is implemented, but durable formal Benchmark
  orchestration and missing/duplicate observation validation are incomplete.
- `run-performance`, true Resume, formal Open RAGBench execution and full
  quality/negative/performance coverage. `run-quality` is accepted only for the
  synthetic isolated fixture.
- Full quality/negative/performance artifact coverage.
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

- Final implementation source and its CI/Integration run IDs are recorded in
  PR #10 and the final delivery. No formal Open RAGBench run is claimed.
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
