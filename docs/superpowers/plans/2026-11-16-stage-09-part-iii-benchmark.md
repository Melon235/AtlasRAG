# Stage 9 — Part III Benchmark Design Plan

## 1. Base and status

- Stage 7 PR #8 closed at `cc5f0d4`.
- Stage 8 PR #9 merged at `bda75fefb70fb1dc61a527d988e3b362bd329d01`.
- Stage 9 base: `bda75fefb70fb1dc61a527d988e3b362bd329d01`.
- Stage 10 is not started.

Part III is external Python tooling under `benchmarks/`. It may import
`atlasrag`; production code must never import `benchmarks` or any benchmark
gold/query data. There are no benchmark fields in production DTOs or State.

## 2. Dataset identity

Primary dataset: Vectara Open RAGBench, arXiv PDF collection.

- Source: `https://github.com/vectara/open-rag-bench`
- Dataset: `https://huggingface.co/datasets/vectara/open_ragbench`
- Frozen HF revision: `63f6b052ff83508b08e242db42263ee708815c26`
- License: CC-BY-NC-4.0 (dataset metadata), not the repository MIT license.
- Release: 1000 PDFs: 400 positive documents + 600 hard negatives.
- Queries: 3045 total; supported `text` + `text-table`: 2062.
- Excluded: `text-image`, `text-table-image`.
- Gold: `qrels.json` maps query UUID to `doc_id` + `section_id`;
  `answers.json` is reference-answer data.
- Dataset files are not redistributed; snapshot/artifacts stay ignored.

Actual values verified from the official metadata: 3,045 total, 2,062 supported,
396 qrel-positive documents, and 604 negative documents. The plan's earlier
400/600 profile assumption conflicts with this; the Architecture Manual itself
states 1000 documents and 2062 supported queries. The 396/604 profile conflict
blocks a formal FULL_CORPUS scorecard until explicitly accepted. The
supported-query count does match 2,062. Any further release/count mismatch is
also a blocking identity conflict.

## 3. Directory layout

```text
benchmarks/
  cli.py
  config.py
  contracts/
  datasets/open_ragbench.py
  snapshots/
  ingestion/
  alignment/
  observation/
  metrics/
  evaluators/
  runners/
  experiments/
  comparison/
  reporting/
benchmark_data/       # ignored dataset snapshots
benchmark_runs/       # ignored artifacts/manifests
tests/stage9/
```

Optional benchmark dependencies are isolated from normal `make verify`; the
harness core uses the standard library and existing project dependencies.

## 4. Snapshot contract

A snapshot has an immutable `manifest.json` containing dataset identity,
source URL, revision, license, file names/SHA256, document/query/modality
counts, supported query count, creation time. Validation rejects:

- missing/extra files;
- checksum mismatch;
- duplicate query/document identity;
- qrel/answer references to absent queries;
- path traversal;
- unsupported modality labels;
- a changed snapshot identity.

Snapshots are built once from official sources into `benchmark_data/`; formal
runs never download the latest data implicitly. Corpus profiles are derived
from the snapshot:

```text
FULL_CORPUS       = all 1000 reference documents
NEGATIVE_ONLY     = reference documents minus qrel-positive documents
SUPPORTED_QUERIES = source text + text-table
PILOT             = explicitly labelled subset, never an official scorecard
```

## 5. Physical isolation

Benchmark runs use an isolated Compose project/databases/Milvus collection/Redis
DB/Knowledge Directory/artifact root. Before execution the environment verifier
checks port ownership/availability, project ownership, PostgreSQL database
identity, Milvus collection identity, Redis isolation, and canonical/test path
separation. Cleanup is allowed only after ownership checks; no FLUSHALL, no
production collection/table deletion, no user volumes or knowledge directories.

## 6. Real ingestion and readiness

Formal corpus ingestion must invoke the existing Part I document build/publish
boundary, not insert gold/qrels into canonical storage and not hand-write
vectors. A readiness gate reuses the production Stage 5 global verification and
requires exact expected sources, complete publication, canonical lineage, and
Milvus exact-set validity. Partial ingestion yields `PARTIAL`/`INVALID`, never a
FULL_CORPUS scorecard.

## 7. Gold alignment

`GoldEvidenceAlignment` is benchmark-only. It maps one gold section to zero,
one, or multiple canonical TEXT_PARENT/TABLE evidence units using document
identity, section content, source anchors, and deterministic normalization.
Confidence is HIGH/MEDIUM/UNRESOLVED. Strict evidence metrics use HIGH only;
MEDIUM is a separate relaxed diagnostic; UNRESOLVED is reported, never treated
as negative evidence. Document-level relevance is separate from strict evidence
relevance.

## 8. Observation and quality policy

Quality runs: Web OFF, Query Cache OFF, Parent Cache absent/bypassed, fixed
snapshot/corpus/query set/strategy. Sessions are independent
`benchmark:{run_id}:{query_id}:{repetition}` identities. The harness invokes
the real production `retrieval_graph`; it does not implement a second RAG.
Observation capture records the available production result and trace facts;
gold joins happen only in Part III. If a required production observation is not
exposed, the harness records the limitation rather than monkeypatching or
adding benchmark fields to production State.

## 9. Metrics

Deterministic metrics are benchmark-owned:

- Hit@K, Recall@K, MRR@K, nDCG@K for binary/graded relevance.
- Explicit ranked-unit identity (child/table candidate, unique parent/table
  selected evidence, or document).
- Duplicate ranked units deduplicated; document-level recall does not double
  count the same document.
- Strict aligned-evidence and relaxed document metrics are reported separately.
- Overall/TEXT/TEXT_TABLE slices are mandatory.

Answer/citation/routing evaluation uses deterministic protocol checks first,
with secondary external evaluators optional and fingerprinted. Reference
answers are never supplied to online generation.

## 10. Runs, validity, manifests, comparison

`RunValidity` is VALID/PARTIAL/INVALID and is determined by predeclared rules
(planned/attempted/completed, controlled product failure, technical failure,
unfinished cases). Failed observations are append-only and never overwritten.
Resume only fills unfinished observations. A run manifest records code commit,
dirty tree, snapshot/corpus/query fingerprints, pipeline/runtime/evaluator
fingerprints, policies, environment, model identities, concurrency, repetitions,
and timestamps.

Comparison validation requires compatible dataset/query/corpus/alignment/
metric/evaluator/Web/cache conditions; index-affecting and runtime-only
experiments are classified and validated by declared factor. Incompatible
comparisons are rejected.

## 11. Experiments

Reranker isolation uses identical frozen pre-rerank candidate artifacts across
BGE CrossEncoder, ColBERT, and RankLLM challengers; E2E validation fixes all
other factors. Sufficiency compares deterministic vs LLM strategies on FULL and
NEGATIVE_ONLY. Web representation uses identical frozen PreparedWebSource
fixtures and no live Web. Compression compares identity, deterministic, and
model-based working representations with identical evidence/citations.

No single weighted winner score; report trade-offs and evidence-based
recommendations. Production defaults are not changed automatically.

## 12. Resources and stop conditions

Full 1000-PDF ingestion and thousands of LLM calls may exceed RTX 3070 8GB,
disk, time, and API budget. Required sequence: metadata/snapshot, pilot
ingestion with real MinerU, pilot quality run, measured cost/resource report,
then formal runs only if approved. Missing dataset revision, license, isolation,
alignment, or resources yields BLOCKED/PARTIAL with exact missing acceptance.

## 13. TDD tasks

1. contracts + snapshot validation;
2. Open RAGBench adapter + modality/profile rules;
3. alignment confidence/coverage;
4. deterministic metrics with hand-checkable fixtures;
5. manifests/validity/resume/comparison;
6. isolation checks and CLI;
7. synthetic isolated integration;
8. real snapshot metadata validation and pilot ingestion;
9. optional evaluator adapters;
10. formal runs/strategy experiments only within measured resources;
11. report, independent audit, CI, open PR.

## 14. Definition of Done

Harness tests/offline checks pass; snapshot and isolation are verified; real
pilot status is reported; formal acceptance is only claimed for completed runs;
Stage 8 production behavior is unchanged; Stage 9 PR is open/unmerged; Stage 10
is not started.
