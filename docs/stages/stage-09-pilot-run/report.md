# Real Pilot Execution Report

## Execution

Commands (real models/resources, no synthetic substitution):

```bash
python -m benchmarks.cli pilot-ingest \
  --snapshot /tmp/pilot-snapshot \
  --selection docs/stages/stage-09-pilot-preflight/pilot_selection_manifest.json \
  --gold docs/stages/stage-09-pilot-preflight/pilot_gold.json \
  --pdf-directory /tmp/real-pilot-pdfs \
  --runtime-module tests/integration/stage9_real_pilot_ingestion_runtime.py \
  --output /tmp/real-pilot-ingest-final

python -m benchmarks.cli run-quality --mode pilot \
  --snapshot /tmp/pilot-snapshot \
  --selection docs/stages/stage-09-pilot-preflight/pilot_selection_manifest.json \
  --gold docs/stages/stage-09-pilot-preflight/pilot_gold.json \
  --readiness /tmp/real-pilot-ingest-final/readiness.json \
  --runtime-module tests/integration/stage9_real_pilot_quality_runtime.py \
  --output /tmp/real-pilot-quality-final \
  --repetitions 1 --max-cases 10
```

## Results

| Item | Result |
|---|---|
| Real PDFs downloaded / ingested | 10 / 10 |
| MinerU parsing | real 4.0.11, all selected PDFs |
| PostgreSQL documents / chunks | 10 / 988 |
| Milvus exact selected set | verified |
| Stage 5 readiness | SAFE_TO_SERVE |
| Official queries executed | 10 |
| RunValidity | VALID (10/10) |
| Web calls | 0 |
| Query Cache GET/SET | 0 |
| Final DeepSeek model | deepseek-flash |
| Final calls / tokens | 21 / 40,396 |
| Final peak-bound cost | $0.017342 |
| Final wall time | 53.6 s |
| Prior verification run | 20 calls / 39,681 tokens / 48.0 s |

## Metrics

All ten cases return the selected qrel document at rank 1. Document-level
Hit@5/10, Recall@5/10, MRR@10 and nDCG@10 are 1.0 for Dense, BM25, Fusion, and
Reranker. This is a 10-query pilot with one relevant document per query, not a
full-benchmark quality claim. Strict evidence metrics are unavailable.

## Failure accounting

No technical failure, SAFE_FAILURE, or incomplete case occurred in the final
run. The ledger/artifact run is VALID.

## Cap compliance

The final run is inside the 10-PDF, 10-query, 100k-token, 2-hour and $0.20
limits. Across the final run plus the earlier verification run, 41 calls were
made, one over the 40-call cap. No further paid calls were made after that
discovery.

## Artifacts

See `manifest.json`, `cases.jsonl`, `observation_cases.jsonl`,
`real_pdf_readiness.json`, `metrics_canonical.json`, `pilot_gold_canonical.json`
and `pilot_result.json` in this directory.
