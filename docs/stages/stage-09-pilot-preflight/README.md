# Stage 9 — Open RAGBench Pilot Preflight Package

Status: **PILOT_PREFLIGHT_PARTIAL / PILOT_BLOCKED**

This directory contains a deterministic selection of real Open RAGBench identities
and the preflight evidence. No PDF was downloaded, parsed, embedded or ingested.
No paid provider call was made.

- `pilot_selection_manifest.json` — 10 documents, 10 supported queries, source URLs,
  qrels section/relevance, deterministic local source path and derived local document ID.
- `pilot_gold.json` — query-specific gold mapping for the selected queries.
- `../stage-09-pilot-preflight.md` — source, identity, runtime, cost and blocker assessment.

Dataset: `vectara/open_ragbench`, revision
`63f6b052ff83508b08e242db42263ee708815c26`, license CC-BY-NC-4.0.
The 604 unreferenced documents are **not** certified hard negatives. Official
`NEGATIVE_ONLY_CORPUS` remains `BLOCKED_UNVERIFIED_PARTITION`.
