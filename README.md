# AtlasRAG

AtlasRAG is an architecture-governed retrieval-augmented generation system built
incrementally through independently verified implementation stages.

The authoritative design is
[`docs/architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md`](docs/architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md).

**Current implementation stage: Stage 2**

**Status: Stage 2 IMPLEMENTATION VERIFIED — PUBLICATION PENDING**

**Next: Stage 3 — Part I: Parse & Canonical Build — NOT STARTED**

Stage 2 implements infrastructure and persistence capabilities only: pinned
localhost Docker Compose services, canonical PostgreSQL records/migrations and
repositories, explicit Unit of Work and advisory-lock leases, typed Redis
caches, a fail-closed Milvus index provider, readiness probes, and isolated
integration tests. Local offline and service-backed verification are green;
normal branch publication and both remote workflows are still pending.

Stage 2 does not implement parsing, chunk construction, embeddings, Part I or
Part II graphs, a real SearXNG provider, cache policy, RuntimeGate, or benchmark
execution. Stage 3 has not started.

## Setup

Python 3.11 and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync
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

## Stage-based development

Each stage is developed on its own branch, stays within its declared scope, and
must pass the repository verification gate before publication. See
[`docs/stages/README.md`](docs/stages/README.md) for the workflow.
