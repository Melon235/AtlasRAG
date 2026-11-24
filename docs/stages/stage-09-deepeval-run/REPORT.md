# Real DeepEval Evaluation — 20-Query Panel

## Status

`DEEPEVAL_EXECUTED_PARTIAL` — 20/20 production cases completed; Judge coverage
18/20 Faithfulness and 17/20 Answer Relevancy. No second batch was started.

- Corpus: `DIAGNOSTIC_SUBCORPUS_50_FINAL`
- Panel: frozen `evaluation_panel_20.json` (16 TEXT, 4 TEXT_TABLE)
- Dataset revision: `63f6b052ff83508b08e242db42263ee708815c26`
- Production graph: real RetrievalRunner/RagCore, Web OFF, Query Cache OFF,
  Parent Cache OFF
- Judge/model: DeepEval 4.2.4 + DeepSeek `deepseek-flash`
- New batch ledger: 200 attempts max, 300,000 tokens, $0.50

## Production execution

- Completed answers/context captures: **20/20**
- Technical/product failures: **0**
- Captured contexts: exact `GenerationInput.local_knowledge` content supplied
  to generation; citation IDs preserved.
- Deterministic citation validity: **20/20** (all citation IDs appear in the
  final answer and citations are nonempty).
- Real answer text and contexts: `production_captures.jsonl`
- Gold/reference answers were joined externally for evaluation only.

## Budget accounting

| Item | Actual |
|---|---:|
| Ledger attempts | **200 / 200** |
| Production attempts | 45 |
| Judge attempts (including retries/failed) | 155 |
| Total tokens | **240,781 / 300,000** |
| Production input/output tokens | 72,981 / 13,329 |
| Judge input/output tokens | 100,357 / 54,114 |
| Total peak-bound cost | **$0.367252 / $0.50** |
| Production cost bound | $0.037889 |
| Judge cost bound | $0.329363 |

The batch stopped at the 200-attempt cap. No further Judge calls were made.

## DeepEval results

| Metric | Evaluated | Not evaluated | Mean | TEXT | TEXT_TABLE |
|---|---:|---:|---:|---:|---:|
| Faithfulness | 18/20 | 2 | **0.9907** | 0.9881 | 1.0000 |
| Answer Relevancy | 17/20 | 3 | **0.9145** | 0.8991 | 0.9643 |

Contextual Relevancy, Contextual Precision and Contextual Recall were **not
run** because the 200-attempt cap was reached after the primary metrics.
They are not reported as measured zeros.

Missing/errored metrics are `EVALUATION_ERROR` records in
`deepeval_results.json`; the last cases hit `BudgetExceededError` before
dispatch, preserving the hard cap. No scores were fabricated.

## Interpretation

Retrieval was saturated at rank 1 across the earlier native diagnostic, while
Answer Relevancy varies (0.769–1.0) and Faithfulness is near-perfect but not
uniform. This demonstrates why retrieval success must not be equated with
answer correctness or claim-level faithfulness. DeepEval scores are
LLM-judged, not ground truth.

## Artifacts

- `production_captures.jsonl`
- `deepeval_results.json`
- `budget_ledger.json`
- `results.json`
- `REPORT.md`

## Limits

- 20-query diagnostic panel, not a full Open RAGBench benchmark.
- Faithfulness/Answer Relevancy coverage is partial.
- Contextual metrics were not executed.
- No Ragas integration.
