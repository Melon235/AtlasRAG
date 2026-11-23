# Stage 9 — Real Open RAGBench Pilot Results

**Label: OPEN_RAGBENCH_PILOT** (10-query pilot; not a full benchmark score).

- Dataset: `vectara/open_ragbench`
- Revision: `63f6b052ff83508b08e242db42263ee708815c26`
- 10 official PDFs downloaded, parsed with real MinerU 4.0.11, embedded with
  Qwen3-Embedding-0.6B, indexed in Milvus, persisted in PostgreSQL.
- Fresh Stage 5 readiness: `SAFE_TO_SERVE`, 10/10 published documents.
- Real BGE reranker and DeepSeek `deepseek-flash` used.
- 10 official supported queries, one repetition, FINAL_LOCAL_ATTEMPT.
- RunValidity: VALID (10/10 completed, 0 product failures, 0 technical failures).
- Final pilot run: 21 DeepSeek API calls, 34,592 input + 5,804 output =
  40,396 tokens, peak-bound cost $0.017342, 53.6 s.
- A prior verification run consumed 20 calls / 39,681 tokens; total across both
  runs was 41 calls, exceeding the 40-call cap by one. No further calls were made.
- Web calls: 0. Query Cache GET/SET: 0.
- Document metrics, all 10 cases: Hit@5/10 = 1.0, Recall@5/10 = 1.0,
  MRR@10 = 1.0, nDCG@10 = 1.0 for Dense, BM25, Fusion and Reranker.
- Strict Parent/Table evidence metrics: unavailable (no trustworthy provenance).
- This perfect result is not evidence of general Open RAGBench quality; each
  selected query has one qrel document and the sample is tiny.

Artifacts in this directory:

- `manifest.json`, `cases.jsonl`, `observation_cases.jsonl`
- `real_pdf_readiness.json`
- `metrics_canonical.json`, `pilot_gold_canonical.json`
- `pilot_result.json`
