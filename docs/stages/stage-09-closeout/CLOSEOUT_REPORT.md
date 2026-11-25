# Stage 9 Closeout Report

```text
Stage 9: PARTIAL
Closure recommendation: OWNER_APPROVAL_REQUIRED
Original DeepEval panel: DEEPEVAL_PANEL_20_PARTIAL
Primary generation metrics: COMPLETE
Stage 10: NOT_STARTED
```

## Evidence preserved

Raw artifacts in `docs/stages/stage-09-diagnostic-50/` and
`docs/stages/stage-09-deepeval-run/` are unchanged. SHA-256 and sizes are in
`artifact_integrity.json`.

- 20/20 frozen production captures
- 35 prior successful evaluations preserved
- 8 new successful Judge-only evaluations
- 43/100 total successful metric evaluations
- 57 remaining: 17 Contextual Relevancy + 20 Precision + 20 Recall
- Missing scores remain null/not executed; no zeros substituted

## Final verified scorecard

Retrieval diagnostic (50 real PDFs, 30 queries; document-level):
Dense/BM25/Fusion/Reranker all Recall@10 = nDCG@10 = MRR@10 = 1.0.

Generation panel (20 queries):
Faithfulness 20/20 = 0.9917; Answer Relevancy 20/20 = 0.9273.
Contextual Relevancy 3/20 = 0.2587 (partial).

## Budget accounting

Original batch: 200 attempts, 240,781 tokens, $0.367252 peak-bound.

Fresh Judge-only batch: 42 attempts, 261,945 tokens, conservative ledger
$0.787038/$0.80; token-based estimate ≈$0.236. Termination reason:
`CONSERVATIVE_COST_RESERVATION_LIMIT`. Actual provider invoice is unavailable.
Historical 41/40 overrun remains documented in
`docs/stages/stage-09-pilot-run/ERRATA.md`; the new ledger is separate.

## Parent expansion trade-off

Child chunks support precise retrieval. Parent expansion intentionally restores
broader context (definitions, conditions, exceptions, explanations). Low
Contextual Relevancy may reflect that broader context but causality is
unproven because no ablation was run. The primary Faithfulness/Answer Relevancy
completion does not prove all context is useful or that Parent expansion is
optimal. No architecture change was made.

## Acceptance conclusion

Primary generation metrics and the 30-query retrieval diagnostic are complete
for their diagnostic scopes. The original Stage 9 contract still has
unsatisfied or partial requirements: all five DeepEval metrics, strict citation
grounding, FULL_CORPUS, NEGATIVE_ONLY, Performance Runner, formal engineering
metrics, and ablation/strategy experiments.

Therefore Stage 9 remains **PARTIAL**. Owner approval is required for any scope
adjustment treating the formal benchmark/performance/ablation work as deferred.
This is not a PASS and not full benchmark completion.

## New execution

```text
New paid API calls: 0
New Judge calls: 0
New production RAG runs: 0
New PDF ingestion runs: 0
New corpus expansion: 0
New formal benchmark runs: 0
New Ragas runs: 0
New ablation runs: 0
```

## Deterministic verification (actual)

```text
make test-stage9: 60 passed, 1 skipped
make verify: 3286 passed, 1 skipped, 108 deselected
ruff check: PASS
mypy: PASS (344 source files)
architecture-check: PASS
git diff --check: PASS
```

Raw artifact integrity: PASS (all hashes/sizes unchanged).

## Owner finalization (amended acceptance)

Owner decision: `OWNER_ACCEPTANCE_DECISION.md`.

```text
Stage 9: PASS UNDER AMENDED ACCEPTANCE SCOPE
Original acceptance contract: SUPERSEDED IN PART (approved scope amendments)
Original DeepEval panel: DEEPEVAL_PANEL_20_PARTIAL (43/100)
Primary generation metrics: COMPLETE
FULL_CORPUS: APPROVED_DEFERRED / NOT_EXECUTED
NEGATIVE_ONLY: APPROVED_DEFERRED / NOT_EXECUTED / BLOCKED_UNVERIFIED_PARTITION
Performance Runner: TRANSFERRED_TO_PART_IV / NOT_EXECUTED
Ablations: APPROVED_DEFERRED / NOT_EXECUTED
Ragas: DEFERRED / NOT_EXECUTED
```

Deterministic citation resolver tests: **3 passed**. Real-capture offline
deterministic integrity is **UNAVAILABLE** because captures do not contain the
full allowed-evidence mapping; no violation was observed and no result is
fabricated. Semantic grounding is approved-deferred.

The acceptance matrix now accounts for all 24 rows exactly once:
18 SATISFIED, 0 PARTIAL, 4 APPROVED_DEFERRED, 2 TRANSFERRED, 0 UNSATISFIED,
0 UNKNOWN. The old 24-vs-23 discrepancy was the omitted owner-approval row.

## Citation validation evidence

- `MARKER_VALIDATION`: 20/20 nonempty markers; MARKER_ONLY.
- `DETERMINISTIC_CITATION_INTEGRITY`: existing production CitationResolver test
  subset — **3 passed**, 11 deselected. Real-capture offline scope is
  unavailable because captures lack the full allowed-evidence mapping.
- `SEMANTIC_CITATION_GROUNDING`: APPROVED_DEFERRED / NOT_EXECUTED.
