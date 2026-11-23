# DIAGNOSTIC_SUBCORPUS_50 — Retrieval-only Diagnostic

## Status: PARTIAL (40-document resource fallback)

The frozen 50-document selection is in `selection.json` (30 queries: 24 text,
6 text-table; 50 unique real PDFs, 20 qrel-linked distractors; seed 20261123).

Five selected PDFs could not be normalized by real MinerU or Docling. The
predeclared fallback `selection_40_fallback.json` contains 25 target queries
and 40 real PDFs (15 distractors). It was fixed **before** retrieval outcomes
were inspected. No synthetic documents or vectors were substituted.

- Real ingestion: 40/40 READY, `SAFE_TO_SERVE`.
- Query execution: 25 official queries.
- Method: real Qwen query embedding, BM25, Fusion, BGE Reranker.
- No DeepSeek Query Construction/Generation, Web, Query Cache or Parent Cache.
- Label: `RETRIEVAL_ONLY_DIAGNOSTIC`.
- No official FULL_CORPUS or NEGATIVE_ONLY claim.

## Results

All four stages returned the target document at rank 1 for every query:

| Stage | Hit@1/5/10 | Recall@5/10 | MRR@10 | nDCG@10 | Mean latency |
|---|---:|---:|---:|---:|---:|
| Dense | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 | 1.0 | 1.0 | 130.47 ms |
| BM25 | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 | 1.0 | 1.0 | 18.92 ms |
| Fusion | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 | 1.0 | 1.0 | 12.73 ms |
| Reranker | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 | 1.0 | 1.0 | 370.48 ms |

TEXT (20 queries) and TEXT_TABLE (5 queries) slices are also all 1.0.
Reranker vs Fusion: 0 helpful, 0 hurt, 25 unchanged; mean rank delta 0.

The subset is still saturated and does **not** differentiate retrieval
strategies. A 100-document escalation was not executed; the 50-document batch
already required substantial download/parse time and five parser failures.
This is a resource/time limitation, not a successful larger experiment.

Artifacts:

- `selection.json`, `selection_40_fallback.json`
- `gold_40_fallback.json`
- `readiness_40_fallback.json`
- `observations_40_fallback.json`
- `results_40_fallback.json`
