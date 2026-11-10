# AtlasRAG

AtlasRAG is an architecture-governed retrieval-augmented generation system built
incrementally through independently verified implementation stages.

The authoritative design is
[`docs/architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md`](docs/architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md).

**Current implementation stage: Stage 4 — Part I: Index & Publish**

**Status: Stage 4 COMPLETE — pull request #5 open and unmerged**

Stage 4 adds lossless representation construction, pinned local
`Qwen3-Embedding-0.6B` embeddings, bounded Milvus candidate indexing,
independent exact-set verification with invocation-bound proof, atomic
PostgreSQL publication through `READY`, and an eight-node LangGraph document
build subgraph with a whole-build advisory lease. The Stage adds no database
migration and does not implement directory synchronization/reconciliation,
runtime maintenance, Redis cache invalidation execution, Part II, or
benchmarks.

The evidence-bearing report is
[`docs/stages/stage-04-part-i-index-publish.md`](docs/stages/stage-04-part-i-index-publish.md).
Fresh local offline, Stage 2 service-backed, Stage 3 PostgreSQL/Docling, real
MinerU, Stage 4 PostgreSQL/Milvus, and real Qwen CUDA gates pass. Final
architecture, security, and report reviews are complete. The branch is
published normally, push- and PR-triggered CI/Integration checks are green,
and [pull request #5](https://github.com/Melon235/AtlasRAG/pull/5) remains open
and deliberately unmerged.

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

Ordinary `make verify` remains network-free, Docker-free, parser-SDK-free,
model-free, and independent of PostgreSQL, Redis, Milvus, and SearXNG.

## Stage-based development

Each stage is developed on its own branch, stays within its declared scope, and
must pass the repository verification gate before publication. See
[`docs/stages/README.md`](docs/stages/README.md) for the workflow.
