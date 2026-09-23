# AtlasRAG Benchmarks

Part III is an external benchmark harness.

The only permitted dependency direction is:

```text
benchmarks -> atlasrag
```

Production code must never import `benchmarks` and must never contain these
benchmark-specific fields:

- `benchmark_mode`
- `benchmark_case_id`
- `qrels_id`
- `gold_label`
- `leaderboard_score`
- `experiment_id`

Stage 0 provides no benchmark implementation.
