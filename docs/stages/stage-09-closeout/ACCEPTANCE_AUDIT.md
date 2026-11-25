# Stage 9 Acceptance Audit

## Sources

- `docs/superpowers/plans/2026-11-16-stage-09-part-iii-benchmark.md` §12–14
- `docs/stages/stage-09-part-iii-benchmark.md` §2, §5, §8
- `docs/stages/stage-09-diagnostic-50/FINAL_REPORT.md`
- `docs/stages/stage-09-deepeval-run/REPORT.md`
- Raw artifacts listed in `artifact_integrity.json`

Stable local IDs are used because the original plan contains no requirement-ID
table.

| ID | Requirement | Source | Available evidence | Status |
|---|---|---|---|---|
| S9-01 | Harness tests/offline checks pass | Plan §14 | `make verify` 3286 pass, 1 skipped; Stage 9 60 pass, 1 skipped | SATISFIED |
| S9-02 | Snapshot validated with pinned revision/identities | Plan §12; report §2 | Pinned metadata validated: 1,000 docs, 3,045 queries, 2,062 supported, 396 referenced, 604 unreferenced | SATISFIED |
| S9-03 | Dataset eligibility/modality filtering | Plan §13 | 24 TEXT + 6 TEXT_TABLE diagnostic; excluded image modalities | SATISFIED for diagnostic |
| S9-04 | Benchmark adapter boundaries; no production import | Plan §13; report §6 | Architecture checks; benchmarks → atlasrag boundary | SATISFIED |
| S9-05 | Deterministic retrieval metrics | Plan §13 | 30-query document metrics, all stages | SATISFIED (diagnostic scope) |
| S9-06 | Real pilot ingestion/readiness reported | Plan §14 | 50/50 real PDFs READY, SAFE_TO_SERVE | SATISFIED (diagnostic scope) |
| S9-07 | Primary generation metrics | Owner decision §6 | Faithfulness 20/20, Answer Relevancy 20/20 | SATISFIED (20-query panel) |
| S9-08 | All five DeepEval metrics | Plan §13 optional evaluators; owner requested five | Contextual Relevancy 3/20; Precision 0/20; Recall 0/20 | PARTIAL |
| S9-09 | Citation validation | Report §4 / execution | MARKER_ONLY 20/20; no strict grounding | PARTIAL |
| S9-10 | Deterministic correctness | Plan §13 | Schema/metric/citation-marker tests; no real strict grounding | PARTIAL |
| S9-11 | Benchmark isolation | Plan §12 | Isolated PostgreSQL/Milvus/Redis; exact IDs/readiness | SATISFIED for tested scope |
| S9-12 | Web/Query Cache/Parent Cache OFF | Owner decision | Paid run: 0 Web, 0 cache GET/SET, Parent bypass | SATISFIED |
| S9-13 | Execution provenance | Plan §13 | Captures, manifests, ledger, hashes | SATISFIED |
| S9-14 | Budget protection | Owner decision | Durable ledger; historical 41/40 incident recorded; fresh batch capped | SATISFIED (guard), incident preserved |
| S9-15 | Failure accounting | Prompt/report | 20/20 production; 0 technical/product failures; Judge budget errors explicit | SATISFIED |
| S9-16 | Reproducibility | Plan §14 | Artifacts/fingerprints/hashes; isolated deepeval env; dataset not redistributed | PARTIAL |
| S9-17 | Engineering metrics (P50/P95/calls/tokens/retry/fallback) | Report §4A | Not measured by Performance Runner | UNSATISFIED / NOT_RUN |
| S9-18 | CI and Stage 2–9 integration | Plan §14 | Final Push/PR CI and integrations | SATISFIED |
| S9-19 | Formal FULL_CORPUS | Plan §12; report §8 | Not run | UNSATISFIED |
| S9-20 | NEGATIVE_ONLY | Report §2/§8 | Blocked unverified partition | UNSATISFIED / BLOCKED |
| S9-21 | Performance Runner | Report §5 | Not implemented | UNSATISFIED |
| S9-22 | Ablations/strategy experiments | Plan §11 | Not run | UNSATISFIED / PROPOSED_DEFERRED |
| S9-23 | PR open/unmerged; Stage 10 not started | Plan §14 | PR #10 open; no Stage 10 implementation | SATISFIED |
| S9-24 | Scope adjustment for extended metrics/formal scopes | This closeout | Owner accepted auxiliary interpretation; formal scope deferral not explicitly approved | OWNER_APPROVAL_REQUIRED |

## Counts

```text
SATISFIED: 14
PARTIAL: 4
UNSATISFIED / BLOCKED: 5
OWNER_APPROVAL_REQUIRED: 1
NOT_APPLICABLE: 0
UNKNOWN: 0
```

(S9-20/S9-21/S9-22 are unsatisfied; S9-08/S9-09/S9-10/S9-16 are partial.)

## Discrepancy: stale canonical Stage 9 report

`docs/stages/stage-09-part-iii-benchmark.md` still states that real Open RAGBench
ingestion, `run-quality`, and DeepEval were not accepted/run. This is stale
relative to the actual artifacts:

- 50/50 real PDFs were ingested and verified.
- Real 20-query production captures exist.
- DeepEval 4.2.4 produced 43/100 evaluations.

The historical report is preserved and this discrepancy is recorded rather than
silently rewritten.

## Mandatory blockers

- Full five-metric DeepEval panel: 43/100, explicit PARTIAL.
- Formal FULL_CORPUS: not run.
- NEGATIVE_ONLY: blocked by unresolved authoritative partition.
- Performance Runner/engineering latency metrics: not implemented.
- Strategy/ablation experiments: not run.
- Strict citation grounding: not implemented (MARKER_ONLY only).
