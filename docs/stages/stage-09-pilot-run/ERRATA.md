# Errata — Real 10-Query Pilot

The previous 10-query run was published as `OPEN_RAGBENCH_PILOT` with a
`FULL_CORPUS` manifest field.

Corrections:

- It is a **SMALL_POSITIVE_CORPUS** pilot, not a formal FULL_CORPUS benchmark.
- It is not evidence of general Open RAGBench quality.
- Across two paid executions, **41 HTTP calls** were made against a 40-call
  authorization. The final run was 21 calls; the earlier verification run was
  20. This is a recorded budget-cap overrun. No further paid calls were made
  for that batch.
- The four 1.0 metric rows are retained as historical results; they are not
  relabeled as a formal benchmark score. Derived annotations should use the
  labels `SMALL_POSITIVE_CORPUS` and `RETRIEVAL_ONLY_DIAGNOSTIC` as applicable.

The original artifact files are preserved unchanged. This errata is an explicit
derived annotation, not a rewrite.
