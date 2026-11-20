# Stage 9 — Real Open RAGBench Pilot Preflight

**Result: PILOT_BLOCKED (preflight partial).** The official metadata and proposed
selection are verified, but the real pilot cannot execute through the current
Benchmark CLI without a real Open RAGBench runtime/ingestion composition and
explicit spending approval. No pilot ingestion or paid inference occurred.

## 1. Pinned snapshot validation

Source: official `vectara/open_ragbench`, revision
`63f6b052ff83508b08e242db42263ee708815c26`, fetched read-only through
`hf-mirror.com` because the origin host was unreachable from this environment.

Verified counts:

| Item | Expected | Observed |
|---|---:|---:|
| Corpus document identities | 1,000 | 1,000 |
| Total queries | 3,045 | 3,045 |
| Supported text/text-table queries | 2,062 | 2,062 |
| Qrel-referenced documents | 396 | 396 |
| Unreferenced documents | 604 | 604 |
| Qrel document IDs resolved in corpus | — | 396/396 |

Modalities: text 1,914; text-table 148; text-image 763; text-table-image 220.
All four metadata files were validated by `SnapshotManifest` with file checksums
and size checks. Dataset content is not committed; only selected IDs/URLs are in
the package. License remains CC-BY-NC-4.0.

The official hard-negative partition is unresolved: the 604 unreferenced
documents are not certified negatives. `NEGATIVE_ONLY_CORPUS` remains
`BLOCKED_UNVERIFIED_PARTITION`.

## 2. Deterministic pilot selection

Selection rule: take the first three supported `text-table` queries in ascending
query-ID order with unique qrel documents, then ascending `text` queries with
unique documents until ten PDFs/queries are selected. One query per document.

Result: **10 PDFs, 10 queries, 10 unique documents**; 3 text-table and 7 text.
The exact IDs, source URLs, qrels section/relevance, local source path and
deterministic local document ID are in
[`pilot_selection_manifest.json`](stage-09-pilot-preflight/pilot_selection_manifest.json).
Query-specific relevance is in
[`pilot_gold.json`](stage-09-pilot-preflight/pilot_gold.json).

No reference answers or query text are committed in this package.

## 3. PDF source feasibility

Metadata-only HEAD checks were performed for all 10 selected URLs. All returned
HTTP 200 with `content-type: application/pdf` (one required a retry). This is
classified **VERIFIED_METADATA_ONLY**, not proof of parsing or indexing.

The selected PDFs are hosted at `https://arxiv.org/pdf/<id>`; total observed
`Content-Length` for nine was approximately 23.3 MB; one response omitted the
header. Proposal budget should reserve at least 50 MB for PDFs plus parse
artifacts. No PDF bodies were downloaded.

Existing Part I path supports PDF ingestion through its configured parser
(`SourceFile` type detection and `DocumentBuildRunner`); the accepted synthetic
runtime did not parse a real Open RAGBench PDF. Real parse/model behavior is
**REQUIRES_PILOT_MEASUREMENT**.

## 4. Identity mapping assessment

The deterministic candidate mapping is:

```text
official document ID d
  -> local source path "<d>.pdf"
  -> production document_id = derive_document_id("<d>.pdf")
  -> revision_id after build
  -> published/indexed revision
  -> ranked document_id
```

`pilot_selection_manifest.json` records the derived local document ID for every
selected document. This is only a path convention; no accepted runtime module
currently persists/validates that mapping against a real Stage 5 published set.

The official query ID -> qrels mapping is verified in metadata. The missing
piece is the real execution adapter that feeds these identities into
`run-quality` and checks the published document/revision set.

## 5. Quality Runner prerequisites

The accepted synthetic CLI (`benchmarks/cli.py run-quality`) requires
`--runtime-module`; the only accepted module is
`tests/integration/stage9_cli_runtime.py`, which hardcodes synthetic Markdown,
synthetic queries and synthetic readiness. It cannot execute the selected real
PDFs or official queries.

Verified synthetic behavior only:

- real production `RetrievalRunner`/RagCore/LocalEvidence
- isolated PostgreSQL/Milvus/Redis
- invocation correlation and per-case artifacts
- query-specific gold replay and RunValidity
- Web OFF and Query Cache OFF
- Stage 5 SAFE_TO_SERVE in the synthetic fixture

Not implemented for the real pilot:

- a benchmark-owned real Open RAGBench runtime factory
- a real ingestion command selecting the 10 PDFs
- authoritative readiness over the real published set
- official-to-canonical mapping persisted and checked at scoring time
- pilot-specific manifest fields (`PILOT`, real commit, real corpus profile)

These are genuine blockers; the current CLI cannot be represented as executable
against real Open RAGBench data.

## 6. Resource and cost preflight

Measured environment (this host, read-only inspection):

| Resource | Observed | Classification |
|---|---|---|
| GPU | RTX 3070 Laptop, 8,192 MiB total, 2,232 MiB used at inspection | VERIFIED |
| System RAM | 7.6 GiB total, 4.5 GiB available at inspection | VERIFIED |
| Disk | 836 GiB available on `/home` | VERIFIED |
| PDFs | ~23 MB for nine; one unknown | ESTIMATED |
| Peak GPU for parser/embedding/reranker | not measured | REQUIRES_PILOT_MEASUREMENT |
| PostgreSQL/Milvus/Redis growth | not measured for real corpus | UNKNOWN |

Proposed hard caps for the future pilot:

- Maximum 10 PDFs.
- Maximum 10 supported queries.
- Maximum 40 DeepSeek calls.
- Maximum 100,000 combined input/output tokens.
- Maximum 2 hours wall time.
- Maximum 10 GB additional benchmark storage.
- No external Web provider calls.

**Monetary cap: UNRESOLVED.** Reliable current DeepSeek pricing and the exact
call mix are not verified in this preflight, so no dollar figure is invented.
User approval must include a concrete spending cap before any paid call.

## 7. Blocker register

| ID | Classification | Evidence | Impact / minimum resolution |
|---|---|---|---|
| B1 | BLOCKER | `benchmarks/quality_command.py` loads a caller-supplied runtime module; only `tests/integration/stage9_cli_runtime.py` exists and hardcodes synthetic corpus/queries. | Real pilot cannot run. Add a benchmark-owned real runtime adapter that ingests selected PDFs and loads official query/gold identities. |
| B2 | BLOCKER | No accepted command exists for real selected-PDF ingestion/readiness. `verify-corpus` returns `SNAPSHOT_VALID` metadata only. | Readiness for the selected published set is unproven. Add approved ingestion + fresh Stage 5 verification. |
| B3 | BLOCKER | The manifest writes `environment_class="SYNTHETIC_INTEGRATION"` and `run_id="synthetic-"` in `benchmarks/quality_command.py`. | A real run would be mislabeled. Add a pilot execution mode with immutable pilot identity. |
| B4 | EXTERNAL_BLOCKER | No spending approval and no verified current provider rate. | Paid pilot cannot start. Obtain concrete monetary cap and provider authorization. |
| B5 | REQUIRES_PILOT_MEASUREMENT | PDF parser, embedding, reranker memory/latency and token usage not measured. | Resource admission must be measured on the selected 10 PDFs before full run. |
| D1 | DEFERRED | No true `run-performance`/Resume. | Does not block a Quality-only pilot. |
| D2 | DEFERRED | Strict Parent/Table evidence mapping incomplete. | Report strict coverage as unavailable; do not block document-level pilot. |
| U1 | UNVERIFIED | Real PDF parse quality and qrel section alignment for selected documents. | Must be measured in pilot; do not claim retrieval quality before execution. |

## 8. Proposed execution plan (not executed)

No command below is claimed to work for real Open RAGBench yet; B1-B3 must be
implemented first.

```bash
# 1. Isolated environment
docker compose --env-file deploy/local/.env -f deploy/local/compose.yaml up -d postgres redis etcd minio milvus
# 2. Ingest selected PDFs (NOT IMPLEMENTED)
# 3. Fresh Stage 5 verification (NOT IMPLEMENTED for real corpus)
# 4. Quality run (requires a real runtime module; NOT IMPLEMENTED)
python -m benchmarks.cli run-quality \
  --snapshot <pinned-snapshot> \
  --runtime-module <real-open-ragbench-runtime> \
  --gold docs/stages/stage-09-pilot-preflight/pilot_gold.json \
  --output <pilot-run-dir> --repetitions 1 --max-cases 10
# 5. Artifact/metric validation
python -m benchmarks.cli check-artifacts --artifacts <pilot-run-dir>/observation_cases.jsonl --manifest <pilot-run-dir>/manifest.json
python -m benchmarks.cli compute-metrics --artifacts <pilot-run-dir>/observation_cases.jsonl --manifest <pilot-run-dir>/manifest.json --gold docs/stages/stage-09-pilot-preflight/pilot_gold.json
# 6. Cost/resource report: capture actual DeepSeek calls/tokens and wall time.
# 7. Cleanup: docker compose ... down --remove-orphans after preserving benchmark-only artifacts.
```

## 9. Readiness decision

**PILOT_BLOCKED.** The official metric identities and a deterministic selection
are verified, but the real pilot requires B1-B3 implementation and B4 explicit
spending authorization. Until then, no real Open RAGBench quality metric may be
reported.

No paid calls, PDF ingestion, pilot execution or full-corpus workload occurred.
