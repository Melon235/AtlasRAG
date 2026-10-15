# AtlasRAG

AtlasRAG is an architecture-governed retrieval-augmented generation system built
incrementally through independently verified implementation stages.

The authoritative design is
[`docs/architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md`](docs/architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md).

**Current implementation stage: Stage 3 — Part I: Parse & Canonical Build**

**Status: Stage 3 COMPLETE — pull request #4 open and unmerged**

Stage 3 adds secure source identity, deterministic PDF/DOCX/XLSX/Markdown/text
routing and normalization, immutable canonical documents/elements/tables,
lossless Parent/Child/Table chunking, production validation, typed persistence
mappers, and atomic candidate transitions through `PARSED` and `CHUNKED`.
The Stage does not add a migration and does not implement embeddings, Milvus
writes, publication, directory synchronization, a LangGraph build graph, Part
II, or benchmarks.

The evidence-bearing report is
[`docs/stages/stage-03-part-i-parse-canonical-build.md`](docs/stages/stage-03-part-i-parse-canonical-build.md).
Fresh local offline, Stage 2 service-backed, Stage 3 PostgreSQL/Docling, and
real MinerU gates pass. Final architecture, security, and report reviews are
ready, including approval of the review-driven security hardening. The branch
is published normally, push- and PR-triggered CI/Integration checks are green,
and [pull request #4](https://github.com/Melon235/AtlasRAG/pull/4) remains open
and deliberately unmerged.

## Setup

Python 3.11 and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync --frozen
```

The default environment deliberately excludes parser SDKs and models. Install
the complete Stage 3 parser runtime explicitly when both real parser paths are
needed:

```bash
uv sync --frozen --group parsing-docling --group parsing-mineru
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

Ordinary `make verify` remains network-free, Docker-free, parser-SDK-free,
model-free, and independent of PostgreSQL, Redis, Milvus, and SearXNG.

## Stage-based development

Each stage is developed on its own branch, stays within its declared scope, and
must pass the repository verification gate before publication. See
[`docs/stages/README.md`](docs/stages/README.md) for the workflow.
