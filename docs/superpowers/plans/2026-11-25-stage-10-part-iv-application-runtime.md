# Stage 10 Implementation Plan

## Scope

Application runtime foundation only: ownership, gate, tasks, guard, health,
cache freshness/commit, maintenance coordination, startup source safety,
composition/lifecycle root. No transport, CLI, Web UI, streaming, backup,
Performance Runner, RAG algorithm or model change.

## Work packages

1. Add backward-compatible operational config and explicit loader.
2. Add fixed per-user flock ownership.
3. Add RuntimeGate, query/maintenance leases and in-flight task registry.
4. Add shared ResourceGuard and guarded Qwen/BGE providers.
5. Add source-availability safety and canonical header projection.
6. Add ApplicationCoordinator startup/query/maintenance/recovery.
7. Add cache freshness barrier and prepare/commit cache boundary.
8. Add Parent Cache TEXT_PARENT integration.
9. Add ApplicationComposition shared-resource lifecycle root.
10. Add offline tests, CI gates, docs and acceptance evidence.

## Verification

```text
make test-stage10
make stage10-verify
make verify
```

Real isolated infra and GPU/system tests remain separate.

## Known boundary

The composition root accepts a `BusinessBundle` builder over shared resources;
complete production wiring of every Stage 5–8 service is deferred until the
bundle builder is reviewed. This is recorded as PARTIAL, not PASS.
