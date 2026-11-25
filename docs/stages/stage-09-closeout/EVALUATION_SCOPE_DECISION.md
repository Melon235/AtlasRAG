# Stage 9 Evaluation Scope Decision

## Owner interpretation accepted

Contextual Relevancy is treated as an **auxiliary diagnostic** for the current
Parent-Child architecture.

Child chunks are the retrieval/ranking unit. Parent context intentionally
restores surrounding definitions, prerequisites, conditions, exceptions and
explanations. Context that does not directly answer a query is not automatically
retrieval noise. The observed low Contextual Relevancy values may reflect the
broader Parent context, but this remains a **hypothesis**: no causal ablation
was executed.

- Do not claim all Parent context is useful.
- Do not claim Parent Expansion caused the low scores.
- Do not claim Parent Expansion is experimentally superior.
- Do not claim high Faithfulness proves factual correctness.
- Do not claim high Answer Relevancy proves retrieval completeness.
- Do not optimize the architecture based on the three partial cases.

## Metric priority

| Metric | Priority | Execution state |
|---|---|---|
| Faithfulness | PRIMARY | COMPLETE (20/20) |
| Answer Relevancy | PRIMARY | COMPLETE (20/20) |
| Recall@10 / nDCG@10 / MRR@10 | PRIMARY retrieval | VERIFIED for 30-query diagnostic |
| Contextual Relevancy | AUXILIARY | PARTIAL (3/20) |
| Contextual Precision | EXTENDED DIAGNOSTIC | NOT EXECUTED; proposed deferral |
| Contextual Recall | EXTENDED DIAGNOSTIC | NOT EXECUTED; proposed deferral |
| Ragas | DEFERRED | NOT EXECUTED |

## Approval status

The owner has accepted the auxiliary interpretation above and does not require
additional paid Contextual Relevancy completion solely to improve that metric.

The original Stage 9 acceptance documents still contain formal FULL_CORPUS,
NEGATIVE_ONLY, performance and experiment expectations. Deferring contextual
precision/recall and the formal benchmark scopes requires an explicit owner
acceptance/scope decision; it is **not** silently waived by this record.

No architecture optimization or ablation was performed.
