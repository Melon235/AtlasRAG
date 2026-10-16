# AtlasRAG Stage 4 — Part I: Index & Publish

## Status

**IN PROGRESS — T0 complete; T1 next. Stage 4 is not complete.**

T1–T11 have not started. No Stage 4 business implementation, real Qwen smoke,
publish verification, final commit/push, or remote Stage 4 CI success is claimed.

## Authority and approved decisions

The tracked Architecture Manual remains authoritative. Implementation follows
`docs/superpowers/plans/2026-10-15-stage-04-part-i-index-publish.md`.

The user approved execution of T0 through T11 with these conditions:

- A: evolve only exact Stage 4 file/import/AST scope whitelists; no broad
  graph-directory exemption and no Stage 1–3 frozen contract changes.
- B: the transitive LangSmith distribution is allowed; direct integration,
  tracing, API-key requirements, and network requirements are prohibited.
  Graph execution must pass offline with tracing disabled.
- C: an internal `ChunkInputLimitError(ValueError)` may classify deterministic
  input-capacity failures only. Ordinary `ValueError` or `Exception` must not
  become CHUNK FAILED. Public ErrorCode and canonical chunk outputs/IDs stay
  unchanged.

Database migration: **NONE planned or added**.

## Base and isolation

- Branch: `stage/04-part-i-index-publish`.
- Base and refreshed `origin/main`:
  `4c5e6b0591286280d050c26c9df10a60ee499baa`.
- Stage 3 head `415cbbd200b6f00adbb9e24f38fcae9ed6ff61aa` and PR #4 merge
  `4c5e6b0591286280d050c26c9df10a60ee499baa` both passed ancestor checks.
- The Stage 4 worktree is isolated from the main checkout. The untracked user
  Architecture Manual in the main checkout is untouched and will not be staged.

## T0 evidence

- `uv sync --frozen --offline` succeeded using the existing shared uv cache:
  Python 3.11.16, 42 packages installed, project built in the new worktree.
- An initial empty temporary-cache attempt could not install cached-missing
  PyMilvus with networking disabled. This was a cache-selection issue, not a
  package/version regression; the unchanged frozen sync above succeeded.
- The first sandboxed `make verify` passed format/lint, mypy (141 source files),
  and import/artifact checks, but hung at the offline Milvus provider async
  thread tests. It was terminated; that attempt is not a passing baseline.
- An isolated unchanged
  `test_missing_collection_is_created_with_exact_schema_and_indexes` timed out
  in the sandbox and passed outside it (`1 passed in 1.36s`). The timeout stack
  showed an idle executor worker and the event loop waiting in its selector.
  A delayed `asyncio.to_thread` probe also hung in the sandbox and completed in
  0.102 seconds outside it. This isolates an execution-environment issue; no
  production code or old test was changed to bypass it.
- Fresh `UV_OFFLINE=1 make verify` outside that sandbox completed successfully:
  **2273 passed, 47 deselected in 77.17s**, mypy 141 source files, Ruff format
  and lint plus import/artifact boundaries all passed. This is current T0
  evidence on the unchanged Stage 3 code, not substituted historical evidence.
- Planning-time host inspection reported RTX 3070 Laptop, 8192 MiB, driver
  576.28. The T0 CUDA tensor preflight below has now passed; real Qwen inference
  is still unverified and remains a separate T2/final gate.
- Existing lockfile torch 2.14.1 contains a macOS arm64 wheel only. Its presence
  does not prove a Linux CUDA environment. T0 must resolve and verify suitable
  official wheels without breaking the frozen parser dependency groups.

## Dependency and offline-gate progress

T0 adds exact `langgraph==1.2.15` to runtime and a nondefault
`indexing-embedding` group. The candidate CUDA lock uses official PyTorch
`torch==2.13.0+cu126` for Linux/Windows, preserves the existing macOS torch
2.14.1 resolution, and retains transformers 5.19.0. The CUDA wheel source is
explicit (`https://download.pytorch.org/whl/cu126`); ordinary dependencies
remain on PyPI. This is installation progress, not a claim of CUDA readiness.

The lock now resolves 200 package records. Old/new lock comparison found zero
removed existing `(name, version)` pairs; all frozen parser versions are
retained. The default environment installs 68 packages and excludes Docling,
MinerU, torch, and transformers. `uv pip check` and offline lock validation
passed for that environment.

New dependency tests first failed on missing LangGraph, missing CUDA-source
configuration, and missing LangGraph import. After the explicit dependency
configuration and exact manifest updates, focused dependency/legacy guards
passed (61 tests). No production file/import/graph AST exemption has been
added at T0.

Post-change `UV_OFFLINE=1 make verify` passed: **2280 passed, 47 deselected in
78.66s**, mypy 142 source files, format/lint/import/artifact gates passed.
The graph dependency smoke clears keys, disables tracing, blocks network
attempts, and asserts no such attempt occurred. LangSmith is transitive only;
its installed pytest entry point is not a direct AtlasRAG tracing integration.

Isolated optional environments also passed dependency checks and their real
parser regressions: Docling Office **5 passed**; MinerU 4.0.11 flash **1 passed**
(one page, seven elements, model cache used offline).

The isolated embedding environment installed 109 packages and passed
`uv pip check`. With torch **2.13.0+cu126** / CUDA build **12.6**, a float16
16-by-16 CUDA allocation, matrix multiplication, and synchronization succeeded
on `cuda:0` (RTX 3070 Laptop); the expected output value was 16.0. This is real
GPU execution, not a device-availability-only check. Transformers 5.19.0
AutoModel, AutoTokenizer, and Qwen3Model imports also passed offline. No model
weights were downloaded for this check.

The joint parser/embedding environment installed 191 packages and passed
dependency checking. Combined real Docling/MinerU tests and the four new
dependency tests passed: **10 passed in 9.42s**. Its CUDA float16 SDPA check
also produced finite output of shape `[1, 1, 4, 16]`.

Direct and key transitive versions currently resolved:

| Distribution | Version |
| --- | --- |
| langgraph | 1.2.15 |
| langchain-core | 1.6.10 |
| langgraph-checkpoint | 4.3.0 |
| langgraph-sdk | 0.4.7 |
| langgraph-prebuilt | 1.1.1 |
| langsmith (transitive only) | 0.14.8 |
| torch (Linux/Windows explicit CUDA source) | 2.13.0+cu126 |
| torch (retained macOS resolution) | 2.14.1 |
| transformers | 5.19.0 |

Independent spec review passed: the reviewer reran 61 focused tests, verified
the four optional environments, the complete lock, and real CUDA matmul/SDPA.
The primary agent independently reran the four dependency tests (4 passed in
0.29s), actual CUDA matmul, and full `UV_OFFLINE=1 make verify` (2280 passed,
47 deselected in 81.45s; mypy 142 files; all other gates passed).

Independent quality review found no blocking issue and identified one minor
test-isolation gap: inherited `LANGSMITH_TRACING_V2=true` took precedence over
the two tracing flags originally disabled in the test. The test now sets and
asserts all three flags false. With the inherited flag true, the new assertion
first failed before graph execution (1 failed in 0.03s); after the explicit
override, the same test passed (1 passed in 0.18s), and all four dependency
tests passed (0.23s). No production code or direct LangSmith import was added.
The quality reviewer checked the correction and approved T0 with no
outstanding findings. The primary agent's final full gate after that change
passed: **2280 passed, 47 deselected in 77.27s**, mypy 142 files, all format,
lint, and architecture gates passed. T1 has not begun.

## Task ledger

| Task | Status |
| --- | --- |
| T0 approvals, isolated baseline, dependency/CUDA preflight | COMPLETE |
| T1 contracts and representation | NOT STARTED |
| T2 embedding protocol and Qwen | NOT STARTED |
| T3 Milvus batch/projection capabilities | NOT STARTED |
| T4 persisted candidate indexing | NOT STARTED |
| T5 independent verification | NOT STARTED |
| T6 controlled failure recording | NOT STARTED |
| T7 atomic publication | NOT STARTED |
| T8 graph and whole-build lease | NOT STARTED |
| T9 real storage integration | NOT STARTED |
| T10 scope gates, commands, CI | NOT STARTED |
| T11 complete verification and publication | NOT STARTED |

## Deferred scope

No directory scan, SyncPlan, index_graph, reconciliation, revision cleanup,
missing-source deletion, MaintenanceCoordinator, RuntimeGate, Redis
invalidation, global serving safety, Part II retrieval/generation, benchmark,
observability backend, or LangGraph DB checkpointer is implemented here.

Stage completion requires successful local gates including real Qwen CUDA,
normal commit/push, and green final-HEAD remote gates. No automatic merge is
authorized.
