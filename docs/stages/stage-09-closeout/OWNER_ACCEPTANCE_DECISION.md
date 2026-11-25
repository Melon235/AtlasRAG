# Stage 9 Owner Acceptance Decision

Date: 2026-11-25 (owner authorization in this task).

## Approved scope amendments

1. **Primary generation metrics**
   - Faithfulness 20/20, mean 0.9917.
   - Answer Relevancy 20/20, mean 0.9273.
   - COMPLETE for the existing 20-query diagnostic panel only.
2. **Extended contextual metrics**
   - Contextual Relevancy: AUXILIARY, PARTIAL, 3/20, mean 0.2587.
   - Remaining 17 Contextual Relevancy evaluations, 20 Contextual Precision
     and 20 Contextual Recall evaluations: **APPROVED_DEFERRED**, not executed.
   - Original five-metric panel remains `DEEPEVAL_PANEL_20_PARTIAL`, 43/100.
3. **Parent-Child architecture**
   - Frozen and unchanged.
   - Lower Contextual Relevancy density is not proof of harmful noise or of a
     Parent Expansion defect; no causal ablation was executed.
4. **FULL_CORPUS**
   - `APPROVED_DEFERRED`, `NOT_EXECUTED`; removed as a blocking requirement for
     amended Stage 9 acceptance. Diagnostic results are not FULL_CORPUS.
5. **NEGATIVE_ONLY**
   - `APPROVED_DEFERRED`, `NOT_EXECUTED`; historical
     `BLOCKED_UNVERIFIED_PARTITION` is preserved. The 604 unreferenced IDs are
     still not certified hard negatives.
6. **Performance Runner**
   - `TRANSFERRED_TO_PART_IV`, `NOT_EXECUTED`. The future Part IV backlog must
     retain P50/P95 latency, LLM calls/tokens per query, retry/degraded/Web
     fallback rates, Parent/Session cache hit ratios, cold/warm cache,
     single/dual-session behavior, and GPU/OOM accounting.
7. **Ablations/strategy experiments**
   - `APPROVED_DEFERRED`, `NOT_EXECUTED`; existing experiment definitions are
     retained and no baseline superiority is claimed.
8. **Ragas**
   - `DEFERRED`, `NOT_EXECUTED`.

## Citation boundary

- Marker validation: 20/20 MARKER_ONLY, preserved.
- Deterministic citation integrity: production citation-resolver tests remain
  mandatory and pass. Real-capture offline verification is limited because the
  captures store marker IDs and generation contexts, not the full allowed
  evidence mapping; no violation was observed and no new result is fabricated.
- Semantic citation grounding/claim-level entailment: `APPROVED_DEFERRED`,
  `NOT_EXECUTED`.

## Acceptance reconciliation

The prior 24-row audit summary had 14 SATISFIED + 4 PARTIAL + 5 UNSATISFIED +
0 APPROVED_DEFERRED + 0 TRANSFERRED + 0 UNKNOWN = 23. The 24th row was the
`OWNER_APPROVAL_REQUIRED` scope-decision requirement, omitted from the old
status categories. This owner decision resolves it, making 24/24 rows accounted
for exactly once.
