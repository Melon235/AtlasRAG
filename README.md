# AtlasRAG

AtlasRAG is an architecture-governed retrieval-augmented generation system built
incrementally through independently verified implementation stages.

The authoritative design is
[`docs/architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md`](docs/architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md).

**Current implementation stage: Stage 1**

**Status: IMPLEMENTATION COMPLETE — PUBLICATION PENDING**

**Next: Stage 2 — Infrastructure & Persistence — NOT STARTED**

Stage 1 implements contracts and the shared kernel only: immutable domain
models, stable enums/errors, declarative graph State schemas, configuration
schemas, fingerprints, request hashing, and trace contracts. It does not
implement providers, infrastructure, persistence, or runtime graph execution.
The local verification gates are green, but Stage 1 is not complete until its
final documentation commit is published and remote CI succeeds.

## Setup

Python 3.11 and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync
```

## Validation

```bash
make verify
```

## Stage-based development

Each stage is developed on its own branch, stays within its declared scope, and
must pass the repository verification gate before publication. See
[`docs/stages/README.md`](docs/stages/README.md) for the workflow.
