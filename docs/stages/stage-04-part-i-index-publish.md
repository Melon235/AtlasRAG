# AtlasRAG Stage 4 — Part I: Index & Publish

## Status

**COMPLETE — all local gates pass; the branch was published normally; final-HEAD
push and pull-request CI/Integration checks are green; pull request #5 is open
and unmerged.**

T9 is committed as `22a984a`, T10 as `b27f3e0`, the final review hardening as
`5bbd88f`, and the verified closeout documentation as `6ce5070`. The 2026-11-09
closeout reran the complete local matrix — offline verification, Stage 2
services, Stage 3 PostgreSQL/Docling, real MinerU, Stage 4 PostgreSQL/Milvus,
and real Qwen CUDA — and all gates passed. Results dated October below remain
historical task evidence; the fresh November evidence is recorded in the T11
closeout section. The documentation-only publication-evidence successor is the
commit containing this section; it is verified through pull request #5 head
rather than a self-referential commit hash.

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
| huggingface-hub | 1.33.0 |
| docling-slim | 2.135.0 |
| docling-core | 2.101.1 |
| mineru | 4.0.11 |
| pymilvus | 2.6.17 |
| psycopg | 3.3.6 |

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
T1 was committed as `11eeaf0` (`feat(stage4): add typed retrieval
representations`). Nothing has been pushed or merged. The isolated test
containers were stopped after the gate; volumes were preserved for later
Stage 4 verification and existing user containers/volumes were untouched.

## T2 progress and model preflight

T2 implements the pinned Qwen provider, including whitespace-preserving token
admission and cancellation-safe bounded inference. Its formal provider smoke
has now passed, along with independent spec/quality reviews and the final offline gate.

The primary agent downloaded the official model/tokenizer snapshot at commit
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` outside the repository. An initial
seven-file runtime-only cache was rejected by Hugging Face Hub 1.33.0 when a
later offline lookup requested the full snapshot: five small documentation/
metadata files were absent. Inspection of the installed completeness check
confirmed the cause. Completing the same revision yielded 12 files totaling
1,207,489,041 bytes; subsequent offline snapshot lookup passed. No revision
or dependency version was changed to bypass the check.

A raw SDK/hardware preflight then passed offline using AutoModel/AutoTokenizer,
explicit CUDA float16/SDPA, last-token pooling and L2 normalization. Actual
device was `cuda:0`; both a short text and a 1500-space input produced finite
1024-dimensional vectors, with tokenizer lengths 7 and 14 respectively.
`truncation=False` and the original whitespace text were used. This is real
Qwen inference, but it does **not** substitute for the production provider's
mandatory model smoke or the later production exact-set verification gate.

### T2 provider implementation and verification

`QwenEmbeddingProvider` exposes explicit asynchronous `initialize` and bounded
`embed`; construction/import does not load optional SDKs or initialize CUDA.
Initialization is cache-only by default, with explicit download opt-in. Both
loaders use the same resolved pinned snapshot, local-only, without trusting
remote code. Identity checks use the loaded dimension/context and every model
parameter's device/dtype; no obsolete Transformers `_commit_hash` attribute
is assumed.

The immutable execution profile defaults to one input per batch and 8192
tokens including special tokens, with the model's 32768-position ceiling.
Input remains unchanged, including nonempty whitespace; empty strings and
oversized batches are rejected. Token overflow and malformed successful
vector output have distinct internal errors. Unknown errors, OOM, timeout,
dependency failures, and cancellation are not business failures. The worker
bridge preserves original exceptions; cancellation waits for Python work and
explicit CUDA synchronization before releasing the shared semaphore. CUDA
weights/forward are float16 with SDPA; L2 normalization uses float32.

Direct `huggingface-hub==1.33.0` is added only to the optional embedding group
for its public pinned-snapshot API. The primary agent compared lockfiles:
all **200 package name/version pairs are unchanged**. SDK imports are allowed
only in the exact Qwen adapter file, with negative tests for other locations.

Implementer RED evidence included 55 initial missing-provider failures,
4 scope/dependency failures, 15 checker failures, and 11 additional CUDA
settlement/configuration/malformed-output failures. The focused suite then
passed (138 tests). A full run exposed one remaining legacy scope union
omission, independently reproduced by the primary agent: **1 failed, 2483
passed, 50 deselected in 91.67s**. The precise Stage 4 file set was added to
that guard and an accidental duplicate union removed; no directory exemption
or production behavior changed. Focused scope/embedding tests passed (131),
and independent spec review passed (153 focused tests).

The primary agent independently ran the formal real-provider model smoke
against the offline pinned cache: **1 passed in 21.48s**. Evidence: model and
tokenizer revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, actual `cuda:0`,
float16 forward/float32 normalization, SDPA, dimension 1024, batch size 3,
finite normalized outputs, batched/individual positional correspondence,
and the actual Stage 3 1500-space Child through the representation builder.
No CPU fallback, text trimming, substitution, or model-version drift occurred.
This closes the real Qwen whitespace gate; the later production Milvus
verification-service gate remains required.

The primary agent's corrected full offline gate completed successfully:
**2484 passed, 50 deselected in 81.24s**, with formatting (176 files), lint,
type checking (158 source files), and architecture checks passing. Independent
quality review found no actionable issues. This verifies T2, not Stage 4 as a
whole; publication and production exact-set gates remain outstanding.
T2 was committed as `3f294c2` (`feat(stage4): add pinned local Qwen embedding
provider`). No push or merge has occurred.

## T3 progress and real storage regression

The provider adds bounded append-only batches, an explicit visibility barrier,
and Strong paginated scalar/JSON projections without vectors. Projection rows
retain duplicates, unexpected types/identities and semantically incorrect
metadata for independent verification. No collection schema or migration is
changed; the existing Stage 2 replacement path and generic SDK runner remain
unchanged. Early-exiting projection consumers must explicitly close their
iterator.

Only typed PyMilvus data/type mismatch exceptions become the new safe internal
write-rejection error. Other SDK failures remain technical. Additional RED
tests reproduced five paths where a late worker error could displace pending
cancellation, plus cancellation during cleanup of an already failed read.
The new capabilities use a separate settling helper so cancellation remains
cancellation after all owned work settles; old Stage 2 behavior is preserved.

Initial T3 RED had 34 missing-capability failures. The implementer's focused
new/old provider, record and scope suite reached **220 passed in 38.92s**.
The primary agent's first full gate stopped at mypy: cross-test fixture imports
were assigned two module names. An explicit test-package marker resolves that
ambiguity; precise typing fixes in the new tests passed full mypy (161 files),
Ruff and formatting (179 files). The implementer reran the focused suite:
**220 passed in 34.41s**. Production code was unchanged by this repair.
No broad mypy exemption or production behavior change is used to bypass it.

The primary agent independently ran the frozen production code against the
isolated real Milvus project: **8 passed in 206.84s**. This includes all five
old Milvus integration tests, the two canonical whitespace regressions, and
new R1-preserving R2 batch append with Strong projection pages of size one.
An earlier pre-final run had **7 passed in 170.44s** before the new integration
case was collected; it is not substituted for the eight-test result.
Independent spec review passed with **47 focused tests in 0.62s**. AST comparison
against `3f294c2` confirmed all pre-existing Milvus functions/methods unchanged.
Independent quality review found no actionable correctness, race or security
issues. The primary agent's final full offline gate passed after stopping the
owned Milvus/MinIO/etcd containers: **2531 passed, 51 deselected in 80.64s**,
with formatting (179 files), lint, mypy (161 files), and architecture checks
passing. Test volumes were preserved; no user containers or volumes were
removed. T3 is verified; independent business verification/publish remain later
tasks, not claims made by this provider-only milestone.
T3 was committed as `5ad929f` (`feat(stage4): extend Milvus with bounded batch
indexing`). No push or merge has occurred.

## T4 progress

The candidate indexing service reconstructs the full canonical boundary from
persisted elements and chunks, validates source-backed lineage, commits the
short INDEXING transition, and performs all embedding/Milvus work outside
that transaction. Its input API accepts context, not caller-supplied canonical
chunks. Tests exercise partial four-of-ten writes followed by scoped R2 retry,
R1 preservation, and release of previous-batch vectors/records before the next
embedding call. The implementer's first focused run passed 33 tests. The final
frozen implementation has 62 T4 cases, including source-consistent forged
chunks rejected against persisted elements, full building ownership, commit
failure rollback, TABLE mapping, and unchanged propagation of classified and
ordinary errors for later orchestration.

Compatibility inspection is additive: read-only provider dimension/analyzer
properties let the service compare injected provider configuration with
RuntimeConfig, then existing initialization validates the physical collection.
Persisted typed chunk strategy version is also checked against the configured
version, not merely for uniformity within the stored set. No frozen schema,
fingerprint projection, canonical output, or existing provider method changes
are made. Version/analyzer binding first produced three expected RED cases;
the corrected suite then passed. The implementer's combined focused gate
passed **377 tests in 36.22s**, and its full offline test run passed
**2593 tests, 51 deselected in 82.05s**. The primary agent independently ran
T4 plus new/old provider tests: **171 passed in 2.27s**; import boundaries and
diff checks passed. The primary agent's complete `UV_OFFLINE=1 make verify`
then passed: **2593 passed, 51 deselected in 81.66s**, formatting (185 files),
lint, mypy (167 files), and architecture gates clean. Independent spec review
passed; its own run was 61 passed with the known sandbox-thread case excluded,
while the primary agent's escalated runs included that case successfully.
Independent quality review also passed with no actionable findings. T4 is
verified; this does not establish an exact-set receipt or authorize publication.
T4 was committed as `55422a8` (`feat(stage4): index durable canonical
candidates`). No push or merge has occurred.

## T5 implementation and pending gates

The invocation-owned `CandidateVerificationAttempt` binds the actual immutable
snapshot returned by T4 indexing. Its private receipt binds complete persisted
authority (including typed strategy metadata) and expected scalar projection,
without accepting caller digests or insert acknowledgements. Verification
reconstructs the PostgreSQL canonical boundary, checks every actual Strong
projection row, and rereads the boundary afterward. It never re-embeds or
constructs placeholder vectors. Consumption is single-use inside the future
publisher's existing transaction, with no writes or commits of its own.
Missing proof, canonical changes, wrong lifecycle state, overlapping operations,
and cancellation fail closed; an invalidated generation cannot rearm proof.

Only positively identified malformed projection data receives the new narrow
internal provider subtype. Ordinary provider errors, outages, timeouts, and
cleanup failures remain technical. If malformed data and cleanup failure occur
together, the result is a generic technical provider error, not INDEX_VERIFY.
This refinement touches only the additive Stage 4 projection capability; frozen
Stage 2 methods, schema, public ErrorCode, and canonical output are unchanged.

The implementer's initial RED run had **39 failures**; additional targeted RED
runs covered malformed response classification (4), lifecycle validation (1),
and simultaneous cleanup failure (1). Its final sandbox-safe focused run passed
**179 tests, 51 deselected in 41.16s**. The primary agent independently ran all
T5 verifier tests: **52 passed in 4.81s**, plus the deterministic provider
cleanup-classification regression: **1 passed in 0.35s**. Full Ruff lint,
formatting (187 files), mypy (169 source files), and `git diff --check` passed.
The limited focused run explicitly excludes threaded provider tests and the
T4 threaded schema-check case; it is not a full offline-gate result.

Two new real-Milvus cases exercise the formal attempt index/verify/consume path
on Stage 3 whitespace Children, including mixed Unicode. They use a durable
test-only fake PostgreSQL store and fake embeddings; T9 still owns real
PostgreSQL plus Milvus E2E. After normal approval capability recovered, the
primary agent ran both new cases together with all prior Stage 2/Stage 4 Milvus
integration regressions: **10 passed in 262.89s**. The formal-verifier whitespace
hard gate is therefore established on real Milvus, not merely on a frozen
fixture or direct low-level query. The owned Milvus/MinIO/etcd containers were
then stopped without removing volumes or touching other projects.

On 2026-10-19, automatic approval review initially failed with a selected-model capacity
error before executing sandbox-external commands, including the primary
agent's read-only isolated Compose status check. This was an approval-service
failure, not a determination that the commands were unsafe. No approval bypass
was attempted. After the user's subsequent continuation request, a retry through
the normal approval path succeeded. Read-only inspection confirmed all three
owned containers were stopped; the isolated project was then started and all
three services reported healthy. The primary agent's complete threaded
verifier/candidate/new-and-old-provider regression passed **227 tests in 8.48s**.
The known baseline sandbox thread-wakeup limitation was handled through normal
approval, not skips or a changed production threading boundary. Import/artifact
checks and **65 scope-contract tests in 35.17s** also passed independently.

Independent spec review passed (53 tests in 5.51s). Independent quality review
found no critical or important issue (53 tests in 5.23s); one minor lifecycle
test-coverage gap was corrected by independently exercising direct consumption
after status mutation while a valid receipt still exists. Quality re-review
confirmed resolution (2 targeted tests in 0.35s). The primary agent reran the
complete updated verifier suite: **53 passed in 5.06s**, with full static checks
still clean. With the owned services stopped, the primary agent's complete
`UV_OFFLINE=1 make verify` (LangSmith/LangChain API keys removed and tracing
disabled) passed: **2650 passed, 53 deselected in 92.23s**, full formatting,
lint, mypy, and architecture checks clean. T5 task gates and both independent
reviews are complete. Stage 4 publication, graph, real PG/Milvus E2E, final
push, and remote CI remain outstanding. No service state was inferred from
the earlier failed status check.
T5 was committed as `5af10dc` (`feat(stage4): verify candidate exact sets
independently`). No push or merge has occurred.

## T6 controlled failure classification

The indexing-specific classifier admits only the existing narrow deterministic
types for REPRESENTATION, EMBEDDING, INDEX_WRITE, and INDEX_VERIFY. Its recorder
propagates every unclassified original exception before reading the clock or
opening a UoW. For an admitted failure, it revalidates matching INDEXING
ownership, source, observed hash, building triple, and metadata slots before
changing only status, failed stage, and timestamp with one explicit commit.
Current/building identities, metadata, canonical rows, and cache freshness are
not changed. Storage errors and cancellation propagate and roll back.

The approved `ChunkInputLimitError(ValueError)` applies only to the four existing
table header/cell/header-block/row capacity checks. Profile, source-map, split,
structural-atom, and internal consistency errors remain unclassified. The
frozen PARSE/CHUNK recorder and public ErrorCode are untouched. The primary
agent's AST comparison proved the chunker is identical to the pre-T6 source
apart from the added import and exactly four exception types. Tests additionally
pin pre-change canonical serialization hashes for text, whitespace, FULL
tables, and ROW_GROUP tables, including chunk IDs and unchanged profile/version.

Initial RED: **111 failed, 11 passed**, from missing T6 capabilities. The final
suite contains 125 T6 cases. The primary agent independently ran T6 plus frozen
chunking/table/determinism/candidate-service regressions: **206 passed in 4.33s**.
The implementer's scope suites passed **107 tests in 70.38s**. Full static checks
initially caught formatting and test annotation issues omitted by the focused
check; these were corrected without behavior changes. The primary agent's
complete `UV_OFFLINE=1 make verify`, with API keys removed, tracing disabled,
and owned services stopped, passed **2775 tests, 53 deselected in 91.70s**,
plus full formatting (190 files), lint, mypy (172 source files), and architecture
checks. Independent spec review passed (181 focused tests and 65 scope tests);
independent quality review passed without findings (125 tests in 2.16s).
T6 task gates and reviews are complete; graph-level routing remains T8 work.
T6 was committed as `c63c0b6` (`feat(stage4): classify controlled build
failures`). No push or merge has occurred.

## T7 atomic publication progress

Publication is being implemented against the existing PostgreSQL UoW and
typed parse-metadata envelope. Its invocation capability must invalidate proof
on every exit, including failures opening the transaction or evaluating the
clock, so a failed publication requires fresh verification. This adds only a
proof-invalidating method to the Stage 4 attempt, never a proof-creating API.

The primary agent started only the owned Stage 4 PostgreSQL service and checked
the loopback port and isolated database name before probing readiness. Compose
supplies separate connection variables; the test DSN was assembled in-process
without printing or persisting credentials. Readiness passed. Existing Stage 2
PostgreSQL integration regressions then passed **15 tests in 8.45s**, using the
existing per-test UUID database creation/migration/cleanup fixture. This is a
storage baseline, not by itself proof of T7 publication or rollback.

T7 now consumes actual invocation proof in its sole transaction, constructs and
validates the promoted typed envelope/document, writes the cache marker, and
commits once. Its finally block invalidates proof even if transaction opening
fails. Uncertain commit outcomes retain frozen UoW semantics; no retry or claim
of rollback after an already completed server commit is introduced. Nothing
calls Redis, touches Milvus, deletes R1, or changes schema during publication.

The initial TDD run produced 36 expected missing-capability failures. The frozen
implementation has 48 publication unit cases plus the existing 53 verifier
cases; the primary agent's combined run passed **101 tests in 11.12s**, full
Ruff/format (193 files), mypy (175 source files), and diff checks clean. Exact
scope/import tests passed **137 in 35.77s**. Independent spec review passed,
including its own **101 tests in 10.59s**.

The first real T7 PostgreSQL run passed **12 tests in 9.05s**. On the frozen
implementation, the final combined Stage 2/Stage 3/T7 PostgreSQL suite passed
**40 tests in 22.63s**. The 12 T7 cases cover four successful publications and
eight injected failures after actual document/marker SQL writes, across no
current revision/R1 and initially false/true marker. A fresh real UoW proves
the document, both metadata slots, revision triples, canonical rows, and marker
roll back together; stale proof is rejected and a real reverify permits retry.
Embedding and Milvus remain test doubles in this PG-focused gate; T9 still owns
combined real PG/Milvus E2E. The owned PostgreSQL container was then stopped
without removing its volume. Independent quality review passed without
findings, including **101 tests in 11.07s**. The primary agent's full
`UV_OFFLINE=1 make verify`, with owned services stopped, API keys removed,
and tracing disabled, passed **2823 tests, 65 deselected in 93.19s**, plus
formatting, lint, mypy, and architecture checks. T7 task gates are complete;
T8 orchestration, T9 E2E, final push, and remote CI remain outstanding.
T7 was committed as `ecc88a6` (`feat(stage4): atomically publish verified
revisions`). No push or merge has occurred.

## T8 lease preflight

Independent technical review confirmed that pool health cannot establish the
health of the separately held advisory-lock session. A public composition route
exists without changing the frozen lease API: an invocation-local pool adapter
captures the exact connection supplied to the existing lock manager and builds
an existing `RuntimeMetadataRepository` against it. A read plus prompt rollback
provides a same-session checkpoint and validates the required singleton. SQL
stays in the repository; the original lease remains the sole releaser. No
private lease fields, shared last-connection registry, new repository file, or
migration are needed. This is part of the planned outer runner, not Graph State.

T8 must latch observed session loss, invalidate proof, propagate a technical
error, and prevent publication at the pre-publication checkpoint. T9 must kill
only the lease backend while an ordinary UoW remains usable to test that case.
These are checkpoint guarantees under the sole-owner protocol, not database
fencing: session death after a probe and already-issued external work remain
possible. Absolute exclusion of stale work after session death at any instant
would require a separate architectural decision; it is not claimed here.

Implementation resumed on 2026-10-21. The runner design uses an explicit
document concurrency bound and reserves separate pool capacity for held leases
and short UoWs. Injected pools have a caller-declared capacity contract; the
production composition owns its configured pool. Cancellation shields only
the current unsafe operation until it settles, never the whole graph through
publication. The final T8 gates and independent reviews are recorded below.

Early TDD evidence: graph/contracts started with 33 missing-implementation
failures, the real service composition added five missing-capability failures,
and the outer runner added 11 missing-module failures. A parent-run offline
focused suite then passed **49 tests in 1.15s**, including actual worker-thread
cancellation. This is an intermediate result, not a frozen T8 completion gate.
The pinned LangGraph runtime deliberately wraps a node-raised CancelledError
as NodeCancelledError; tests distinguish that technical error from actual
task cancellation. Neither path may record a business failure or continue to
publication. No vendor changes or test skips were used.

## T8 graph and runner verification

The actual LangGraph graph now composes the eight fixed lifecycle nodes and
the two explicit business exits. Each invocation owns fresh services and
verification proof; State contains only transient canonical data and safe
result metadata. Indexing continues to reconstruct persisted canonical data,
not consume transient chunks as authority. Source mutation discards only the
candidate. WARN parsing remains publishable; six positively classified failure
stages use their existing or Stage 4 recorder, while technical failures escape.

The outer runner captures and probes the actual lease session through public
dependencies. It hashes only after acquiring the document lock, reserves
separate lease/UoW capacity, and releases once after issued work settles. The
process owner explicitly initializes shared Qwen and index resources before
accepting builds; parser SDKs remain lazy. It rejects close while builds are
active, so its caller must await them first. Injected index lifecycle ownership
is explicit. Qwen has no unload API and retains its process-lifetime model.

Actual worker-barrier tests cover parse, initial hashing, and chunking under
repeated cancellation. A separate regression proves cancellation stops later
index batches. Startup cancellation initially exposed index close before its
worker finished; the application now settles only that initialization before
cleanup, without modifying the frozen provider or shielding the whole graph.

The parent independently passed **63 focused tests in 1.62s**. The first full
gate found one scope-helper defect: Stage 2 passed absolute paths while the
new exact LangGraph whitelist handled relative paths only (**2919 passed,
1 failed, 65 deselected**). Nine new path/API regressions now prove only the
approved repository file and imports are admitted, including rejection of an
external same-name file. No Stage 2 business tests or contracts were changed.

Final parent-run `make verify` passed **2929 tests, 65 deselected in 98.53s**,
with **202 formatted files**, Ruff, mypy over **184 files**, and both architecture
checks passing. LangSmith keys were removed, tracing disabled, uv offline,
and the task-owned infrastructure containers stopped. The focused graph gate
also blocks network connection attempts. Independent specification and code
quality reviews both passed on 2026-10-22. These are T8 offline guarantees;
real combined PostgreSQL/Milvus build and selective lease-backend termination
remain T9 work. Stage 4 push and final remote gates remain outstanding.
T8 was committed as `69953f4` (`feat(stage4): compose locked document build
subgraph`).

## T9 real storage and lease integration

The storage gate now contains 36 cases across the four explicitly selected
Stage 4 integration files. Test-only composition uses real TXT/Markdown
parsers, canonical chunking, the actual build graph and runner, real PostgreSQL
and Milvus, and deterministic fake embedding without model assets. Full scalar
and typed-metadata projections are compared with persisted canonical source;
whitespace-only Child text is preserved. Markdown TABLE coverage exercises
both stores; non-null sheet/ROW_GROUP coverage separately uses a canonical
fixture with real Milvus, not a claimed real XLSX parse.

R1 snapshots cover canonical rows, current triple/metadata, and full Milvus
projections. R2 fault cases cover all four controlled index failures, technical
embedding/write/verification failures, and rollback after actual publication
document/marker SQL. Source mutation tests cover both current/no-current
recovery. Partial-write retry proves real partial and stale rows exist, then
observes an empty candidate set before full replacement without touching R1.

Real session tests cover same-document exclusion and different-document
concurrency. Cancellation records normal parser settlement before the sole
unconditional lease release. The lease-loss case terminates only the captured
runner backend after validating its uniquely owned test database, proves an
ordinary UoW remains usable, and rejects publication without FAILED conversion
or reacquisition. This is checkpoint liveness evidence, not fencing.

The initial complete gate returned **35 passed, 1 failed in 828.84s**. The
different-document concurrency case exceeded the frozen Stage 2 fixture's
10-second SDK timeout during rate-limited collection flush. An isolated
unchanged rerun reproduced it (**1 failed in 39.05s**). Each candidate issues
three flushes; two concurrent candidates share one collection. Milvus documents
the default per-collection flush quota as 0.1 QPS
([official configuration reference](https://milvus.io/docs/configure_quotaandlimits.md)).

The single-variable experiment constructs only this test's provider through
the fixture's public client/collection fields with the production-default
30-second SDK timeout. The 60-second outer watchdog, actual concurrency and
all assertions remain unchanged. No production code, Stage 2 fixture, server
quota, retry policy, or business error classification changed. Both targeted
cases then passed: **2 passed in 82.61s**. Parent-run static checks passed:
205 formatted files, Ruff, mypy over 187 files, and import boundaries. The
complete four-file rerun then passed **36 tests in 855.01s**. No production
change was necessary; all storage assertions, failure routes, and actual
lease-backend termination ran without skips. Stage 4 still requires T10–T11
and successful publication.

The parent reran the entire focused `tests/stage4` suite with tracing disabled,
keys removed, and uv offline: **560 passed in 17.40s**. The storage containers
were still running for the independent integration gate; this focused result
is not presented as the services-stopped full `make verify` gate.

On 2026-10-23 the parent also reran the real Qwen gate with Hugging Face
offline mode and the already cached pinned snapshot: **1 passed in 30.73s**.
The actual model/tokenizer revision remained
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, CUDA 0, float16/SDPA, 1024
dimensions. Batch-three versus individual inference, finite normalized
vectors, ordering, and the exact 1500-space canonical Child all passed.
This smoke batch is not a change to the production batch-one baseline.

A supplemental T0–T9 static review found no actionable correctness defect.
It identified a deployment constraint: the approved offline guarantee assumes
tracing is disabled. External LangSmith/LangChain tracing flags or an inherited
tracing context can enable upstream callback tracing of graph inputs/outputs.
AtlasRAG adds no direct integration, but does not claim to override an external
opt-in. Do not enable those flags or wrap these builds in a tracing context;
stronger isolation is not part of the approved Stage 4 gate.

Final T9 parent-run `make verify` passed **2929 tests, 84 deselected in
106.88s**, with format, Ruff, mypy (187 files), import boundaries, and tracked
artifact checks all passing. The task-owned PostgreSQL/Milvus stack was
stopped, API keys removed, tracing disabled, and uv offline; volumes were
retained. Specification and quality reviews, including the final test-only
timeout delta, passed. Commit message: `test(stage4): cover real index publish
and lease invariants`.

## T10 scope gates, commands, and CI

The four new Make targets keep offline tests, the exact four-file real storage
suite, and optional Qwen smoke separate. `stage4-verify` runs the default gate,
focused Stage 4 tests, then the storage script. The script starts only
PostgreSQL, etcd, MinIO, and Milvus with bounded Compose health waiting plus a
PostgreSQL capability probe. Its failure/signal cleanup preserves the primary
exit status, surfaces cleanup failures after otherwise successful work, and
never removes local volumes.

CI adds a run-specific Stage 4 job using frozen default dependencies and fake
embedding. Actions and the MinIO source build remain pinned; all three tracing
flags are disabled. Failure diagnostics precede always-cleanup of the owned
ephemeral CI project and volumes. Existing Stage 2/3 jobs and selectors remain
unchanged; the workflow test now terminates its Stage 3 slice at the exact new
job boundary.

Scope regressions close SQLAlchemy/sqlite3 plumbing and indexing-service
cache/Part II import gaps. Exact graph capability checks now reject wildcard
imports rather than treating their permitted base module as permission for
every symbol. Closed State fields, the fifteen public exports, migration set,
and indexing graph files remain frozen. No production business code changed.

TDD evidence: **56 failed / 120 passed** before implementation, plus **3
failed** for the subsequently identified wildcard bypass. Implementer final
focused/Stage 3 tooling validation passed **210 tests**, and standalone Stage
4 passed **631 tests**. The parent full gate on 2026-10-23 passed **3013 tests,
84 deselected in 108.88s**, with 207 formatted files and mypy over 189 files.
Specification and code-quality reviews both passed. After resuming on
2026-10-27, a fresh actual `make stage4-verify` passed its default gate
(**3013 passed / 84 deselected in 111.83s**) and focused target (**631 passed
in 21.28s**), then successfully started the four dependencies and passed the
PostgreSQL readiness probe. The real integration completed **36 passed in
842.87s**, followed by successful cleanup and exit code 0. The four owned
containers and their network were removed; persistent volumes were retained.
This independently validates the actual full script, not only recording-tool
tests. All eight T10 code/test files passed staged artifact, whitespace, and
common credential-pattern checks. Commit message: `ci(stage4): add isolated
offline and storage gates`.

## Closeout regression evidence in progress

On 2026-10-23, `make stage3-parser-smoke` was rerun in the existing isolated
MinerU dependency environment with frozen, offline uv resolution. The real
PDF-to-canonical flash path passed **1 test in 4.22s**, using MinerU SDK and
producer **4.0.11** (one page, seven canonical elements). The default
environment was not changed. Remaining closeout gates and final-HEAD remote
results are still outstanding.

On 2026-10-27 the new `make stage4-model-smoke` target passed **1 test in
23.65s** using the isolated embedding environment and offline cached snapshot.
Actual CUDA/float16/SDPA, the pinned model/tokenizer revision, 1024 dimensions,
canonical whitespace, ordering, finiteness, and normalization all passed.

The real `make stage3-parser-smoke` was refreshed on 2026-10-27 as well:
**1 passed in 2.62s**, still using SDK/producer 4.0.11 and the same canonical
one-page/seven-element result.

| Gate | Latest parent evidence (2026-10-27 unless noted) |
| --- | --- |
| `make verify` | PASS — 3013 passed, 84 deselected; services stopped |
| `make test-stage4` | PASS — 631 passed |
| `make stage2-verify` | Pending closeout rerun |
| `make stage3-verify` | Pending closeout rerun |
| `make stage3-parser-smoke` | PASS — real MinerU flash, 1 passed |
| `make stage4-verify` | PASS — full script, 36 real integration tests, successful volume-preserving cleanup |
| `make stage4-model-smoke` | PASS — real CUDA Qwen, 1 passed |
| Final-HEAD remote CI / Integration | Not yet pushed |

## Final review questions

The final independent static review passed on 2026-10-27. These answers apply
to AtlasRAG's supported ownership protocol, not arbitrary external database
writers. Runtime and remote completion evidence remains separately gated.

| Question | Answer / enforced boundary |
| --- | --- |
| Can TEXT_PARENT be written to Milvus? | No; service filtering and record/provider validation reject it. |
| Can an embedding vector enter Graph State? | No; closed State fields and streamed-update regressions exclude it. |
| Can NaN/Inf be indexed? | No; embedding/service/record validation rejects nonfinite values. |
| Can wrong-dimension embeddings be indexed? | No; identity, vector, and physical collection checks reject them. |
| Can the provider silently use another model revision? | No; model/tokenizer pins and loaded identity checks must match. |
| Can representation silently truncate? | No; full raw/rendered text is retained or capacity fails explicitly. |
| Can stale in-memory chunks be indexing authority? | No; indexing reconstructs persisted canonical records. |
| Can retry leave stale candidate records? | Not after successful retry and verification; a failed attempt can leave unpublished residuals. |
| Can verification pass with an extra record? | No; full exact-set comparison rejects extras/duplicates/Parent rows. |
| Can verification pass with a missing chunk? | No; expected records are independently rebuilt from PostgreSQL. |
| Can R2 publish before verification? | No; invocation-bound, single-use proof is required and revalidated. |
| Can R1 change while R2 indexes? | R1 rows/current triple/current metadata are preserved; latest-candidate status may change. |
| Can successful publish leave the cache marker false? | No; promotion and marker=true share one transaction/commit. |
| Can candidate metadata overwrite current early? | No; building/current envelope slots remain separate until publication. |
| Can building_revision_id remain after published READY? | No; the complete building triple and metadata slot are cleared atomically. |
| Can transient Milvus outage become business FAILED? | No; technical failures propagate without that classification. |
| Can crash/cancellation automatically become FAILED? | No; recovery policy is deferred, and cancellation preserves technical semantics. |
| Can same-document builds overlap? | Not while the healthy session lease is held; lost-session checks are not fencing. |
| Does the lease span prepare through publish? | Yes, with issued work settled before the sole release. |
| Can different documents hold independent locks? | Yes; real concurrent-runner integration verifies this. |
| Can Stage 4 invalidate Redis? | No; only the PostgreSQL freshness marker is set. |
| Can Stage 4 serve production retrieval? | No; Part II remains deferred. |
| Can Stage 4 start directory sync/reconciliation? | No; Stage 5 has not started. |

## T11 closeout verification — 2026-11-09

The temporary Stage 4 worktree, optional environments, model cache, test env
file, and prior execution session were unavailable after the execution
environment changed. All implementation commits through `b27f3e0` survived in
Git. Only the confirmed stale worktree registration was pruned; the branch was
restored into an isolated worktree. Main, its untracked user documents, and all
pre-existing containers, volumes, and credentials were left unchanged. No
production code, test, canonical contract, schema, migration, or dependency
version changed during restoration.

The default locked environment (68 packages), Docling (98), MinerU (150), and
embedding (109) environments were reconstructed offline from the frozen uv
cache. The pinned Qwen snapshot (`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`,
12 files, about 1.2 GB) was restored to an ignored cache; six small repository
metadata files were completed so the Hub completeness check passes. Docker,
the pinned service images, and the RTX 3070 Laptop (8192 MiB, driver 576.28)
were available. The gates used a fresh, uniquely named Compose project with
process-only credentials and non-default loopback ports; no pre-existing
volume, credential, or user data was reused. Containers were stopped after each
script; named test volumes were retained.

| Command | Fresh result on the closeout tree |
| --- | --- |
| `UV_OFFLINE=1 make verify` | PASS — 3036 passed, 84 deselected in 112.11s; 208 formatted files, Ruff clean, mypy 190 files, import/artifact checks clean |
| `make stage2-verify` | PASS — exit 0; 28 real integration tests in 156.82s across all six healthy services; migration head `20260928_0001`; volume-preserving teardown |
| `make stage3-verify` | PASS — exit 0; 529 focused Stage 3 tests in 5.33s plus 18 real PostgreSQL/Docling tests in 7.81s |
| `make stage3-parser-smoke` | PASS — 1 passed in 3.01s; real MinerU SDK/producer 4.0.11, one page, seven canonical elements |
| `make stage4-verify` | PASS — exit 0; 653 focused Stage 4 tests in 22.26s plus 36 real PostgreSQL/Milvus tests in 848.04s; volume-preserving cleanup |
| `make stage4-model-smoke` | PASS — 1 passed in 24.99s; real Qwen revision `97b0c614...`, `cuda:0`, float16/SDPA, 1024 dimensions, batch 3, exact 1500-space canonical Child, finite normalized vectors, order correspondence |
| `make architecture-check` | PASS — import boundaries and forbidden tracked artifacts |
| `git diff --check` | PASS |

Two environment observations did not change code or tests. During Stage 2
startup, bounded Milvus readiness probes observed transient collection
creation/loading errors; each recovered on the next poll and all 28 tests
passed. The first Qwen smoke attempt failed before inference because the
restored cache used the legacy `huggingface/` layout while `HF_HOME` made
Hugging Face 1.33 resolve `hub/`, and the session `NO_PROXY` contained the
bracket form `[::1]`, which httpx rejects. Pointing `HF_HUB_CACHE` at the
existing snapshot, completing the six small metadata files, and sanitizing
`NO_PROXY` fixed the environment; the retry passed every real inference
assertion. No sentinel, CPU fallback, truncation, test skip, or validator
relaxation was introduced.

### Final review corrections

The final independent static audit found one gate-hermeticity gap and three
minor guard/contract gaps. All were corrected before publication; no frozen
Stage 1–3 contract, canonical output, schema, migration, or dependency version
changed.

1. **Offline graph and storage gates are now self-contained.** A Stage 4-wide
   autouse test guard removes LangSmith/LangChain keys and endpoints, forces
   `LANGSMITH_TRACING`, `LANGSMITH_TRACING_V2`, `LANGCHAIN_TRACING`, and
   `LANGCHAIN_TRACING_V2` false, and denies `socket.connect`, `connect_ex`,
   `create_connection`, and `getaddrinfo`. `scripts/stage4_verify.sh` applies the
   same environment override for the real storage run, and the CI Stage 4 job
   declares all four tracing flags false. The command regression runs with all
   four flags inherited as true and dummy keys, then asserts the script removed
   the keys and forced every flag false. A graph test passes under inherited
   `LANGSMITH_TRACING_V2=true` without a LangSmith warning or upload.
2. **Stage 4 index DTOs now match the Milvus text boundary.** Dense/sparse text
   rejects NUL and Unicode surrogate code points while remaining nonempty and
   whitespace-preserving; the record `file_name` enforces nonblank, no NUL, and
   the existing 1,024-byte cap. Tests cover NUL, surrogates, the accepted
   1,024-byte boundary, and rejected 1,025-byte input.
3. **Database plumbing is rejected in every indexing service.** The import
   checker now rejects direct `sqlite3`, SQLAlchemy, psycopg, psycopg_pool, and
   PyMilvus imports under `services/indexing/`, with negative tests for the
   publication and candidate services.
4. **The embedding-SDK scope exception is path-exact.** The Stage 1 scope helper
   now admits `torch`/`transformers`/`huggingface_hub` only for the exact
   repository-relative Qwen adapter path and rejects an external same-named
   file.

Re-verification after these corrections: 290 focused review tests passed; the
full offline gate passed **3036 tests, 84 deselected** in 112.44s with 208
formatted files, Ruff clean, mypy over 190 files, and both architecture checks
clean; the focused Stage 4 suite passed **653 tests**; and the complete real
PostgreSQL/Milvus gate passed **36 tests in 848.04s** with volume-preserving
cleanup. The added conftest is tracked in the same closeout commit.

## Task ledger

| Task | Status |
| --- | --- |
| T0 approvals, isolated baseline, dependency/CUDA preflight | COMPLETE |
| T1 contracts and representation | COMPLETE — 11eeaf0 |
| T2 embedding protocol and Qwen | COMPLETE — 3f294c2 |
| T3 Milvus batch/projection capabilities | COMPLETE — 5ad929f |
| T4 persisted candidate indexing | COMPLETE — 55422a8 |
| T5 independent verification | COMPLETE — 5af10dc |
| T6 controlled failure recording | COMPLETE — c63c0b6 |
| T7 atomic publication | COMPLETE — ecc88a6 |
| T8 graph and whole-build lease | COMPLETE — 69953f4 |
| T9 real storage integration | COMPLETE — 22a984a |
| T10 scope gates, commands, CI | COMPLETE — b27f3e0 |
| T11 complete verification and publication | COMPLETE — 6ce5070; PR #5 open and unmerged |

## Git history and publication

Committed Stage 4 work:

```text
d9d6703 chore(stage4): establish approved scope and locked dependencies
11eeaf0 feat(stage4): add typed retrieval representations
3f294c2 feat(stage4): add pinned local Qwen embedding provider
5ad929f feat(stage4): extend Milvus with bounded batch indexing
55422a8 feat(stage4): index durable canonical candidates
5af10dc feat(stage4): verify candidate exact sets independently
c63c0b6 feat(stage4): classify controlled build failures
ecc88a6 feat(stage4): atomically publish verified revisions
69953f4 feat(stage4): compose locked document build subgraph
22a984a test(stage4): cover real index publish and lease invariants
b27f3e0 ci(stage4): add isolated offline and storage gates
5bbd88f fix(stage4): harden final offline and index contract gates
6ce5070 docs(stage4): record T11 closeout verification
```

The documentation-only commit containing this publication section supersedes
`6ce5070` for the report itself. It changes documentation only and is verified
through the pull-request head rather than embedding its own commit hash.

Publication evidence:

- Verified closeout commit:
  `6ce507098ce3623172f2de5a4b465c8ad6fe7f8e`
- Normal push to `origin/stage/04-part-i-index-publish`: **PASS**
- Force push: **NOT USED; PROHIBITED**
- Remote CI `Verify`: **PASS** — push run
  [42615965981](https://github.com/Melon235/AtlasRAG/actions/runs/42615965981)
  and pull-request run
  [42616008288](https://github.com/Melon235/AtlasRAG/actions/runs/42616008288)
- Remote `Integration`: **PASS** — push run
  [42615966072](https://github.com/Melon235/AtlasRAG/actions/runs/42615966072)
  and pull-request run
  [42616008083](https://github.com/Melon235/AtlasRAG/actions/runs/42616008083)
  passed the Stage 2, Stage 3, and Stage 4 jobs
- Pull request number/status:
  [#5](https://github.com/Melon235/AtlasRAG/pull/5) — **OPEN, CLEAN,
  UNMERGED**
- Merge into `main`: **NOT PERFORMED; MUST REMAIN MANUAL**

## Deferred scope

No directory scan, SyncPlan, index_graph, reconciliation, revision cleanup,
missing-source deletion, MaintenanceCoordinator, RuntimeGate, Redis
invalidation, global serving safety, Part II retrieval/generation, benchmark,
observability backend, or LangGraph DB checkpointer is implemented here.

Stage completion requires successful local gates including real Qwen CUDA,
normal commit/push, and green final-HEAD remote gates. No automatic merge is
authorized.
