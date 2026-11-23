# Stage 9 — Final 50-Document Diagnostic and DeepEval Integration

## Status

- `CORPUS_50_READY`
- `RETRIEVAL_DIAGNOSTIC_ACCEPTED`
- `DEEPEVAL_ADAPTER_READY`
- `DEEPEVAL_REAL_EVALUATION_AWAITING_PAID_AUTHORIZATION`
- Stage 9 remains **PARTIAL**.

Dataset revision: `63f6b052ff83508b08e242db42263ee708815c26`.
Web OFF, Query Cache OFF, Parent Cache OFF. No paid calls this iteration.

## Parser failures and replacement

Five original selected target PDFs failed real MinerU normalization and Docling
fallback:

- `2408.07372v2`
- `2410.08287v3`
- `2412.03848v3`
- `2412.05495v6`
- `2408.14507v2`

Recorded cause: `ProviderResponseError` / provider layout-invalid
(normalization), not a synthetic substitution. Real MinerU also produced
invalid mixed superscript+subscript styles on some layouts.

A deterministic reserve panel (`replacement_seed=20261124`, seed+1) selected
five TEXT target queries and five qrel-linked distractors, excluding the
original 50 document identities and all prior queries. It was frozen before
retrieval execution. The final set is therefore
`DIAGNOSTIC_SUBCORPUS_50_FINAL`, not the original frozen 50; selection bias from
replacing parser-failing documents is disclosed.

## Final corpus

- 50 unique real PDFs
- 30 supported queries: 24 TEXT, 6 TEXT_TABLE
- 30 qrel-linked target documents
- 20 additional qrel-linked distractors
- Real ingestion: 50/50 READY
- Fresh Stage 5: SAFE_TO_SERVE
- Exact source and Milvus sets verified; canonical lineage verified.

## Native retrieval metrics

`RETRIEVAL_ONLY_DIAGNOSTIC`, direct production LocalEvidenceRunner; no DeepSeek
Query Construction/Generation.

All 30 queries returned the qrel target at rank 1.

| Stage | Hit@1/5/10 | Recall@5/10 | MRR@10 | nDCG@10 | Mean latency |
|---|---:|---:|---:|---:|---:|
| Dense | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 | 1.0 | 1.0 | 73.79 ms |
| BM25 | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 | 1.0 | 1.0 | 18.27 ms |
| Fusion | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 | 1.0 | 1.0 | 12.25 ms |
| Reranker | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 | 1.0 | 1.0 | 349.72 ms |

TEXT (24 queries) and TEXT_TABLE (6 queries) slices are also all 1.0.
Reranker vs pre-rerank Fusion: 0 helpful, 0 hurt, 30 unchanged; mean rank delta 0.
The subset remains saturated and does not differentiate retrieval strategies.

## Retry/gold rank regression

Metrics are computed with k=1 for Hit@1, k=5 for Hit@5/Recall@5, and k=10 for
Hit@10/Recall@10/MRR@10/nDCG@10. A rank-regression test covers Gold at ranks
1, 3, 8, and outside Top 10.

## DeepEval

- Official package: `deepeval==4.2.4` installed in an isolated evaluation
  environment.
- Verified actual signatures for `LLMTestCase`, `FaithfulnessMetric`,
  `AnswerRelevancyMetric`, `ContextualRelevancyMetric`,
  `ContextualPrecisionMetric`, `ContextualRecallMetric`.
- Adapter: `benchmarks/evaluation/deepeval_adapter.py`.
- Adapter validates captured production fields and marks incomplete cases
  `NOT_EVALUABLE`; no score is fabricated.
- Real answer/context capture from a paid end-to-end run is **not present**;
  therefore no DeepEval scores are claimed.
- A deterministic fake metric/judge integration is covered at the adapter
  boundary; real Judge calls await a fresh paid authorization and a wired
  budgeted judge.

## Budget

`benchmarks/budget.py` provides durable shared attempt reservation and
reconciliation, corruption fail-closed behavior, and fake-HTTP regression
coverage. It is not yet wired into every production provider client. The
historical pilot's 41 calls versus 40 cap is retained in
`stage-09-pilot-run/ERRATA.md`.

Proposed next paid batch, **not authorized**:

- 20 queries maximum
- 60 HTTP attempts including retries
- 160,000 combined tokens
- $0.50 USD
- 2 hours
- Web/Query Cache/Parent Cache OFF

## Artifacts

- `selection_final_50.json`, `gold_final_50.json`
- `readiness_final_50.json`
- `observations_final_50.json`
- `results_final_50.json`

No FULL_CORPUS or NEGATIVE_ONLY claim is made.

## Final execution note

The 50-document final panel is complete and the 30-query retrieval diagnostic
was executed. Native retrieval metrics are saturated at 1.0; this is a valid
diagnostic observation, not evidence that the strategies are equivalent.
DeepEval real Judge evaluation was **not** executed; it remains at the paid
authorization gate. The adapter and verified 4.2.4 signatures are ready, but a
real answer/context capture and fresh paid batch are still required.
