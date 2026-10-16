# AtlasRAG Stage 4 — Part I: Index & Publish

## Status

**IN PROGRESS — T0 committed; T1 implementation and review gates passed.
Stage 4 is not complete.**

T2–T11 have not started. No real Qwen smoke, publish verification, final
commit/push, or remote Stage 4 CI success is claimed.

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
lint, and architecture gates passed. T0 was committed as `d9d6703`
(`chore(stage4): establish approved scope and locked dependencies`).

## T1 implementation and verification

The internal immutable contracts and pure `RepresentationBuilder` are
implemented. The closed baseline profile uses strict integer `schema_version=1`,
`strategy_name="raw-child-table-slice"`, and
`representation_version="representation-v1"`. The builder checks the configured
representation version without changing the frozen pipeline fingerprint.

Child dense/sparse text is exactly canonical raw content. Table text uses the
unchanged Stage 3 renderer; sheet, row range, and source-element facts remain
typed at `structured_metadata.provenance`, with canonical table source anchors
preserved separately. No sheet label is invented in rendered text. Parent
chunks are rejected. Text limits count UTF-8 bytes, admit the exact 65,535-byte
boundary, and never truncate. The builder's explicit capacity failure uses
`RepresentationInputLimitError`; ordinary validation/programming errors are
not converted into business failures.

Implementer-observed RED evidence: two missing-package collection errors;
16 pure-layer import guard failures; two strict schema failures; four direct
DTO byte-capacity failures; and one excessive configured-capacity failure.
These were followed by implementation and passing focused tests. The new
production manifest permits exactly five files and has negative tests for
unapproved files in both new packages. The import checker adds prohibitions,
not directory-wide exemptions.

The primary agent independently ran `UV_OFFLINE=1 make verify` on the complete
T1 working tree: **2372 passed, 47 deselected in 77.54s**. Ruff format checked
169 files, lint passed, mypy checked 151 files, and import/artifact gates passed.
`git diff --check` also passed. No model, tokenizer, database, or network service
is used by T1. Independent spec review initially passed with 74 focused
contract/representation tests and 111 guard tests; the subsequent quality
review found the integration conflict below. The passing existing suite does
not establish complete canonical-input coverage or Stage 4 completion.

## T1 blocking review finding — source-backed whitespace Child

On 2026-10-16 the quality reviewer found, and the primary agent independently
reproduced, a conflict across frozen Stage 2/3 contracts:

- The existing Stage 3 test
  `test_long_source_whitespace_is_losslessly_covered_by_default_sized_children`
  uses `"A" + " " * 4000 + "B"` with parent/child limits 8000/1500. The actual
  chunker emits Child lengths **1500, 1500, 1002**; the middle Child is
  nonempty, whitespace-only, and has valid typed source lineage. Concatenation
  preserves the original source exactly. This is deliberate frozen behavior.
- The new T1 `_index_text` validator calls `_nonblank`, so the representation
  rejects that canonical Child for both `dense_text` and `sparse_text`.
- The frozen Stage 2 `MilvusChunkRecord._content` also calls `_nonblank` and
  rejects the same Child for `content` and `sparse_text`. Its existing contract
  test explicitly rejects whitespace-only `sparse_text`.

Changing only T1 cannot resolve the downstream contradiction. Dropping the
Child, trimming/prefixing its text, or changing Stage 3 chunking would violate
the approved lossless representation and exact-set requirements. Broadly
catching the validation error as a business failure is not an approved fix.

Implementation stopped under AGENTS rule 17 rather than changing the frozen
contracts silently. The user subsequently explicitly approved the Stage 4
amendment: index text changes from **nonblank** to **nonempty and
whitespace-preserving**. Whitespace-only canonical text is allowed; empty
strings, trimming, dropping whitespace Children, rechunking, and
sentinel/substitution are prohibited. Stage 3 output, schema, chunk IDs and
content hashes remain unchanged; there is no migration.

The approved narrow change affects Stage 2 index `content`/`sparse_text` and
matching Stage 4 text contracts only. Identifiers/file names remain nonblank;
Unicode, NUL and byte-capacity protections remain in place. Required gates:
cross-stage regression, real Qwen whitespace embedding, and real Milvus
whitespace record plus exact-set verification. If real Milvus cannot represent
the text, STOP / REPORT; neither DTO tests nor a fake provider substitutes for
that evidence. Implementation has resumed. The T1 evidence below completes
the cross-stage and early real Milvus admission gates, not the later Qwen or
production-verifier gates.

### Amended T1 evidence

The implementer observed **14 failed, 120 passed** before the narrow validator
changes, then **134 passed**. Cross-stage tests use the actual frozen Stage 3
chunker and preserve all three Children through representation, transient
record, and Milvus record construction, including the middle 1500 spaces.
IDs, content hashes, source lineage, byte content, and concatenated source
remain identical. The only pre-existing production modification is index
`_content` admission; identifier/file-name and Unicode/NUL/size checks remain.
The former whitespace-only rejection test explicitly evolves under the
approved amendment and still rejects the empty string.

The primary agent started a unique isolated local Milvus/etcd/MinIO project
using the pinned images, without reusing existing service volumes. Real tests
passed independently: **2 passed in 31.27s**. Both ASCII and mixed-Unicode
canonical sources retain the whitespace-only Child. Strong reads match the
entire expected projection, including IDs, count, duplicate absence, content,
sparse text, parent/source metadata, and typed structural metadata. Fixtures
clean up only their own UUID-named test collections. These tests use explicit
test vectors and the existing write provider; they do not claim Qwen inference
or the future production verification service has run.

Independent spec and quality re-reviews both passed; each reran the 134 focused
tests and found the previous conflict resolved without further issues. The
primary agent's final full offline gate passed: **2392 passed, 49 deselected
in 75.56s**, Ruff format 170 files, lint clean, mypy 152 files, and both
architecture gates clean. No Stage 3 production files or migrations changed.
T1 is ready for its planned commit; nothing has been pushed or merged.

## Task ledger

| Task | Status |
| --- | --- |
| T0 approvals, isolated baseline, dependency/CUDA preflight | COMPLETE |
| T1 contracts and representation | VERIFIED — ready for commit |
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
