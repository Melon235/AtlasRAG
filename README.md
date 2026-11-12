# AtlasRAG

AtlasRAG is an architecture-governed retrieval-augmented generation system built
incrementally through independently verified implementation stages.

The authoritative design is
[`docs/architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md`](docs/architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md).

**Current implementation stage: Stage 7 — Part II: Web Evidence & Answer**

**Status: Stage 7 COMPLETE — pull request #8 open and unmerged; live provider
acceptance pending credentials**

Stage 7 adds independent `web_evidence_subgraph` and `answer_subgraph`
capabilities: SearXNG JSON search, SSRF-safe fetch/sanitize/budgets, direct and
batch-summary Web evidence strategies, structured DeepSeek generation,
identity compression, deterministic output review, bounded regeneration, and
authoritative citation resolution. It adds no database migration and does not
implement Query Construction, RagCore orchestration, Query Cache, RuntimeGate,
Stage 8, or benchmarks.

The evidence-bearing report is
[`docs/stages/stage-07-part-ii-web-evidence-answer.md`](docs/stages/stage-07-part-ii-web-evidence-answer.md).
Local, Stage 2–6 regression, real Qwen/MinerU, and local HTTP transport gates
pass; push/PR CI and Stage 2–7 Integration are green. The explicit live
SearXNG/DeepSeek smoke is unverified without external credentials.
[pull request #8](https://github.com/Melon235/AtlasRAG/pull/8) remains open and
deliberately unmerged.

## Setup

Python 3.11 and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync --frozen
```

The default environment deliberately excludes parser SDKs and models. Install
the complete parser runtime explicitly when both real parser paths are needed:

```bash
uv sync --frozen --group parsing-docling --group parsing-mineru
```

The real Stage 4 Qwen smoke additionally needs the nondefault embedding group:

```bash
uv sync --frozen --group indexing-embedding
```

## Validation

```bash
make verify
```

`make verify` is secret-free, CPU-only, and service-free. To run the explicit
Stage 2 integration gate, copy `deploy/local/.env.example` to the ignored
`deploy/local/.env`, replace its local-only placeholders, and run:

```bash
make stage2-verify
```

Stage 3 provides four separately scoped commands:

```bash
make test-stage3
make test-stage3-integration
make stage3-verify
make stage3-parser-smoke
```

- `make test-stage3` runs the focused offline Stage 3 suite with the default
  locked environment.
- `make test-stage3-integration` starts PostgreSQL only and runs real candidate
  transactions plus real Docling DOCX/XLSX fixtures. Its local cleanup stops
  containers without deleting named volumes.
- `make stage3-verify` composes ordinary offline verification, the focused
  Stage 3 suite, and the PostgreSQL/Docling integration gate. It does not run
  MinerU.
- `make stage3-parser-smoke` is the separate mandatory local gate for
  `simple.pdf -> real MinerU flash -> DocumentContainer`; it may initialize or
  download MinerU runtime assets and is intentionally absent from ordinary CI.

Stage 4 provides four separately scoped commands:

```bash
make test-stage4
make test-stage4-integration
make stage4-verify
make stage4-model-smoke
```

- `make test-stage4` runs the focused offline Stage 4 suite with the default
  locked environment and fake embedding.
- `make test-stage4-integration` starts PostgreSQL, etcd, MinIO, and Milvus and
  runs the 36-test real-storage gate with deterministic fake embedding.
- `make stage4-verify` composes ordinary offline verification, the focused
  Stage 4 suite, and the real PostgreSQL/Milvus gate. It never downloads model
  weights.
- `make stage4-model-smoke` is the separate real CUDA gate for the pinned local
  Qwen model, canonical whitespace, ordering, finiteness, and normalization.
  It uses the nondefault embedding group and is intentionally absent from
  ordinary CI.

Stage 5 provides three separately scoped commands:

```bash
make test-stage5
make test-stage5-integration
make stage5-verify
```

- `make test-stage5` runs the focused offline Stage 5 suite.
- `make test-stage5-integration` starts PostgreSQL, etcd, MinIO, and Milvus and
  runs the real sync/recovery/global-safety gate with deterministic fake
  embedding.
- `make stage5-verify` composes ordinary offline verification, the focused
  Stage 5 suite, and the real PostgreSQL/Milvus gate.

Stage 6 provides four separately scoped commands:

```bash
make test-stage6
make test-stage6-integration
make stage6-verify
make stage6-model-smoke
```

- `make test-stage6` runs the focused offline Stage 6 suite.
- `make test-stage6-integration` starts PostgreSQL, etcd, MinIO, and Milvus and
  runs the real local-evidence gate with deterministic fake embedding/reranker.
- `make stage6-verify` composes ordinary offline verification, the focused
  Stage 6 suite, and the real PostgreSQL/Milvus gate.
- `make stage6-model-smoke` is the separate CUDA gate for real Qwen query
  embedding and the real `BAAI/bge-reranker-v2-m3` baseline; it is intentionally
  absent from ordinary CI.

Stage 7 provides:

```bash
make test-stage7
make test-stage7-integration
make stage7-verify
make stage7-provider-smoke
```

`make test-stage7` is offline; `make test-stage7-integration` runs real local
HTTP transport contracts without Docker or external services;
`make stage7-verify` composes both; `make stage7-provider-smoke` is the explicit
live SearXNG/DeepSeek check and requires external credentials/network.

Ordinary `make verify` remains network-free, Docker-free, parser-SDK-free,
model-free, and independent of PostgreSQL, Redis, Milvus, and SearXNG.

## Stage-based development

Each stage is developed on its own branch, stays within its declared scope, and
must pass the repository verification gate before publication. See
[`docs/stages/README.md`](docs/stages/README.md) for the workflow.
