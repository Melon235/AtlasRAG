# Stage 9 Final Scorecard

## Scope separation

- **Retrieval diagnostic:** `DIAGNOSTIC_SUBCORPUS_50_FINAL`, 30 queries / 50 documents.
- **Generation evaluation:** `DEEPEVAL_PANEL_20`, 20 queries (16 TEXT, 4 TEXT_TABLE).
- This is **not** formal FULL_CORPUS or NEGATIVE_ONLY.

## Retrieval quality — document level

All 30 queries returned the qrel target at rank 1.

| Stage | Recall@10 | nDCG@10 | MRR@10 | Cases |
|---|---:|---:|---:|---:|
| Dense | 1.0 | 1.0 | 1.0 | 30 |
| BM25 | 1.0 | 1.0 | 1.0 | 30 |
| Fusion | 1.0 | 1.0 | 1.0 | 30 |
| Reranker | 1.0 | 1.0 | 1.0 | 30 |

Source: `docs/stages/stage-09-diagnostic-50/results_final_50.json`.
Granularity: document-level. No chunk/Parent-level claim is made.

## Primary generation quality

| Metric | Evaluated | Mean | TEXT | TEXT_TABLE |
|---|---:|---:|---:|---:|
| Faithfulness | 20/20 | 0.9917 | 0.9896 | 1.0000 |
| Answer Relevancy | 20/20 | 0.9273 | 0.9180 | 0.9643 |

Judge: `deepseek-flash`; DeepEval 4.2.4; thinking mode disabled.

## Auxiliary diagnostics

| Metric | Evaluated | Mean | Status |
|---|---:|---:|---|
| Contextual Relevancy | 3/20 | 0.2587 | PARTIAL |
| Contextual Precision | 0/20 | null | NOT_EXECUTED |
| Contextual Recall | 0/20 | null | NOT_EXECUTED |

The 3-case Contextual Relevancy mean is not representative of all 20 queries.
The three observed values are 0.082, 0.241 and 0.452 (full IDs in JSON).

## Coverage invariants

```text
Faithfulness             20
Answer Relevancy         20
Contextual Relevancy      3
Contextual Precision      0
Contextual Recall         0
TOTAL                    43 / 100
Remaining                57
```

## Citation validation

`MARKER_ONLY`: 20/20 cases contain nonempty citation IDs appearing in the
answer. This is not strict citation grounding, entailment or claim-level
correctness.

## Engineering metrics

P50/P95 latency, average LLM calls/tokens per query, retry rate, degraded
retrieval rate, Web fallback rate and related production Performance Runner
metrics are **UNAVAILABLE**; no Performance Runner was executed.

## Provenance

- Retrieval: `docs/stages/stage-09-diagnostic-50/`
- Generation: `docs/stages/stage-09-deepeval-run/`
- Raw artifact hashes: `docs/stages/stage-09-closeout/artifact_integrity.json`
