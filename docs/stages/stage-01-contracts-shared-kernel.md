# AtlasRAG Stage 1 — Contracts & Shared Kernel

## Stage

Stage 1 — Contracts & Shared Kernel.

## Status

**COMPLETE**

The Stage 1 implementation and local verification gates are complete. The
verified implementation commit was published normally to the Stage branch and
its remote CI run succeeded. No force push or automatic merge was performed.

## Scope

Stage 1 implements only the frozen shared contracts on which later stages
depend:

- immutable Pydantic domain and boundary DTOs;
- stable string enums and typed technical errors;
- exact declarative graph input, State, and output schemas;
- immutable configuration schemas;
- deterministic pipeline/runtime fingerprints and request hashing;
- the structured `SpanRecord` trace contract; and
- validation, serialization, architecture, and scope-regression tests.

Pydantic is the only production runtime dependency introduced by this Stage.
Implementation and verification require no real secrets, credentials, network
access, or running services.

This Stage does not implement providers, repositories, infrastructure clients,
LangGraph business execution, RuntimeGate, parsing/indexing, retrieval, Web
search, generation, cache execution, a business CLI, a Web UI, benchmarks, or
any Stage 2 capability.

## Contracts implemented

All canonical Pydantic contracts use the shared `FrozenModel` policy:
`frozen=True`, unknown fields forbidden, non-finite model numbers
forbidden, and nested model instances revalidated. Ordered boundary
collections admit only list/tuple input and are stored as tuples. All nested
boundary text is checked recursively and rejects lone Unicode surrogate code
points before an accepted model can reach a JSON/UTF-8 boundary.

| Area | Contracts | Implemented guarantees |
| --- | --- | --- |
| User and scope | `QueryFilters`, `UserTurnRequest`, `LocalRetrievalRequest`, `InternalRetrievalFilters`, `ResolvedRetrievalScope` | Explicit user filter allowlist, nonblank request fields, immutable internal chunk-type constraints, and one typed resolved scope shared by retrieval branches. |
| Retrieval | `CandidateScores`, `RetrievalCandidate`, `RetrievalBranchResult`, `CandidatePool` | Finite optional score slots, TEXT_CHILD parent requirement, ordered candidate semantics, and distinct OK/EMPTY/UNAVAILABLE and retrieval-mode invariants. Rank history is not stored in DTOs. |
| Local evidence | `SourceAnchor`, `LocalProvenance`, `EvidenceRef`, `LocalEvidence`, `LocalEvidenceResult` | Canonical POSIX-relative provenance, stable local-evidence identity, no Child retrieval payload after the Evidence boundary, and independent execution/sufficiency status invariants, including DEGRADED + SUFFICIENT. |
| Web evidence | `WebEvidenceRequest`, `WebSearchResult`, `PreparedWebSource`, `WebEvidence`, `WebEvidenceResult` | HTTP(S) source identity without URL userinfo, normalized matching host/domain, positive search rank, typed prepared/evidence representations, and cardinality rules for OK/DEGRADED/EMPTY/UNAVAILABLE. Raw HTML and provider responses are absent. |
| Answer boundary | `AnswerLocalEvidence`, `AnswerWebEvidence`, `AnswerInput`, `WorkingEvidenceBundle`, `GenerationResult`, `OutputReviewResult` | `L1...`/`W1...` citation namespaces, globally unique IDs, authoritative immutable evidence mapping, local-evidence cardinality rules, unique declared citations, and stable deterministic review decisions/reason codes. |
| Final result | `FinalLocalCitation`, `FinalWebCitation`, discriminated `FinalCitation`, `FinalResponse`, `RagCoreResult` | Source-specific final citations, unique resolved citation IDs, and explicit SUCCESS/DEGRADED_SUCCESS/SAFE_FAILURE completion without transport, session, trace, or cache-policy fields. |
| Trace | `SpanRecord` | Immutable, UTC-canonical, JSON-safe structured span data kept outside graph State. |

## Enums implemented

All enum values are stable strings; aliases, unknown values, byte input, and
scalar coercion are rejected at the corresponding Pydantic boundaries.

| Enum | Exact values |
| --- | --- |
| `SourceType` | PDF, DOCX, XLSX, MD, TXT |
| `RetrievalChunkType` | TEXT_CHILD, TABLE |
| `EvidenceType` | TEXT_PARENT, TABLE |
| `RetrievalBranch` | DENSE, BM25 |
| `RetrievalBranchStatus` | OK, EMPTY, UNAVAILABLE |
| `RetrievalMode` | HYBRID, DENSE_ONLY, BM25_ONLY, UNAVAILABLE |
| `CandidateOrdering` | RRF, DENSE, BM25, RERANKER |
| `LocalExecutionStatus` | OK, DEGRADED, UNAVAILABLE |
| `LocalEvidenceStatus` | SUFFICIENT, INSUFFICIENT, AMBIGUOUS |
| `WebContentOrigin` | FETCHED_PAGE, SEARCH_SNIPPET |
| `WebEvidenceRepresentation` | SUMMARY, SANITIZED_CONTENT, SEARCH_SNIPPET |
| `WebExecutionStatus` | OK, DEGRADED, EMPTY, UNAVAILABLE |
| `LocalAnswerEvidenceStatus` | SUFFICIENT, INSUFFICIENT_BEST_MATCH, NONE |
| `OutputReviewDecision` | ACCEPT, REGENERATE, BLOCK |
| `OutputReviewReasonCode` | UNKNOWN_CITATION, CITATION_SET_MISMATCH, INVALID_OUTPUT_SCHEMA, FORGED_SOURCE_REFERENCE, SOURCE_BOUNDARY_VIOLATION, CONTROL_LEAKAGE, PROTOCOL_CONTENT |
| `SourceKind` | LOCAL, WEB |
| `RagCompletionStatus` | SUCCESS, DEGRADED_SUCCESS, SAFE_FAILURE |
| `AnswerExecutionOutcome` | SUCCESS, SAFE_FAILURE |
| `TraceStatus` | OK, DEGRADED, ERROR |

## State schemas implemented

The graph package exports exactly 15 total `TypedDict` schemas. Required
and optional keys, including their annotation targets, are reflection-tested
against Architecture Manual §46A.7.

| Schema | Required keys and types | Optional keys and types |
| --- | --- | --- |
| `RetrievalGraphInput` | `request: UserTurnRequest` | none |
| `RetrievalGraphState` | `request: UserTurnRequest` | `rag_result: RagCoreResult`; `final_response: FinalResponse` |
| `RetrievalGraphOutput` | `final_response: FinalResponse` | none |
| `RagCoreInput` | `request: UserTurnRequest` | none |
| `RagCoreState` | `request: UserTurnRequest`; `local_attempt_index: int` | `base_local_request: LocalRetrievalRequest`; `current_local_request: LocalRetrievalRequest`; `local_result: LocalEvidenceResult`; `web_result: WebEvidenceResult`; `answer_input: AnswerInput`; `final_response: FinalResponse`; `answer_outcome: AnswerExecutionOutcome`; `result: RagCoreResult` |
| `RagCoreOutput` | `result: RagCoreResult` | none |
| `LocalEvidenceSubgraphInput` | `request: LocalRetrievalRequest` | none |
| `LocalEvidenceState` | `request: LocalRetrievalRequest` | `dense_branch: RetrievalBranchResult`; `bm25_branch: RetrievalBranchResult`; `candidate_pool: CandidatePool`; `evidence_refs: tuple[EvidenceRef, ...]`; `local_evidence: tuple[LocalEvidence, ...]`; `result: LocalEvidenceResult` |
| `LocalEvidenceSubgraphOutput` | `result: LocalEvidenceResult` | none |
| `WebEvidenceSubgraphInput` | `request: WebEvidenceRequest` | none |
| `WebEvidenceState` | `request: WebEvidenceRequest` | `search_results: tuple[WebSearchResult, ...]`; `prepared_sources: tuple[PreparedWebSource, ...]`; `result: WebEvidenceResult` |
| `WebEvidenceSubgraphOutput` | `result: WebEvidenceResult` | none |
| `AnswerSubgraphInput` | `input: AnswerInput` | none |
| `AnswerState` | `input: AnswerInput`; `generation_attempt_index: int` | `working_evidence: WorkingEvidenceBundle`; `generation_result: GenerationResult`; `review_result: OutputReviewResult`; `final_response: FinalResponse`; `outcome: AnswerExecutionOutcome` |
| `AnswerSubgraphOutput` | `final_response: FinalResponse`; `outcome: AnswerExecutionOutcome` | none |

State remains execution data only. The only graph-semantic counters are
`RagCoreState.local_attempt_index` and
`AnswerState.generation_attempt_index`. State contains no dependencies,
configuration, clients, repositories, services, provider responses, exceptions,
trace/log accumulators, raw HTML, vectors, conversation history, retry history,
cache metadata, or benchmark data. No graph execution is implemented.

## Error model

`ErrorCode` has exactly eight values:

~~~text
DEPENDENCY_TIMEOUT
DEPENDENCY_UNAVAILABLE
INVALID_PROVIDER_RESPONSE
CANONICAL_DATA_INCONSISTENT
INVARIANT_VIOLATION
RESOURCE_EXHAUSTED
INVALID_CONFIGURATION
SOURCE_MUTATED
~~~

The public typed exception hierarchy is:

~~~text
AtlasRAGError
├── DependencyError
│   ├── DependencyUnavailableError
│   └── DependencyTimeoutError
├── ProviderResponseError
├── CanonicalDataError
├── InvariantViolationError
├── ResourceExhaustedError
├── ConfigurationError
└── SourceMutationError
~~~

Every exception exposes a typed `ErrorCode` and caller-safe message.
Concrete exceptions use fixed codes and fixed safe messages; `DependencyError`
accepts only timeout/unavailable codes, while the base `AtlasRAGError`
remains the typed general form. Construction, string conversion, and
reconstruction preserve the public contract without carrying vendor exception
details.

Normal negative domain outcomes—EMPTY, INSUFFICIENT, and AMBIGUOUS—remain typed
statuses, not exceptions. SAFE_FAILURE remains a controlled domain completion,
not a wrapper for unexpected technical failures.

## Configuration schemas

All configuration models are immutable, reject unknown fields, use strict
scalar validation, and are revalidated when nested in `RuntimeConfig`.

| Schema | Exact fields |
| --- | --- |
| `InfrastructureConfig` | `knowledge_directory`, PostgreSQL host/port/database/user/password, Redis host/port, Milvus host/port, and `searxng_url` |
| `ModelConfig` | Embedding model and tokenizer IDs/revisions, reranker/query/generation model IDs/revisions, `deepseek_api_key`, and `model_device` |
| `PartIConfig` | `loader_behavior_version`, `parser_behavior_version`, `chunker_behavior_version`, `representation_version`, `embedding_dimension`, `bm25_analyzer`, `milvus_schema_version`, `milvus_index_version` |
| `RetrievalConfig` | `query_construction_version`, `retrieval_semantics_version`, `sufficiency_strategy`, `sufficiency_version`, `reranker_strategy` |
| `AnswerConfig` | `generation_prompt_version`, `web_evidence_strategy`, `compression_strategy`, `citation_policy_version`, `output_policy_version` |
| `CacheConfig` | `query_cache_enabled`, `parent_cache_enabled`, `query_ttl_seconds`, `parent_ttl_seconds` |
| `MaintenanceConfig` | `max_document_concurrency`, `drain_timeout_seconds`, `shutdown_timeout_seconds` |
| `ObservabilityConfig` | `trace_sample_rate`, `log_level`, `log_directory`, `log_queries` |
| `RuntimeConfig` | `infrastructure`, `models`, `part_i`, `retrieval`, `answer`, `cache`, `maintenance`, `observability` |

Ports are bounded to 1–65535, counts/TTLs are strict positive integers,
timeouts are finite positive numbers, and trace sample rate is bounded to
0–1. The SearXNG coordinate must be HTTP(S) and contain no URL userinfo.
Stage 1 provides schemas only: no `.env` loader, secret loader, hot reload,
bootstrap, dependency container, or external client exists.
PostgreSQL and DeepSeek credentials are required nonblank schema values stored
as Pydantic `SecretStr`; their `repr` and JSON serialization are masked. They
remain available to a future bootstrap layer without being committed, logged,
traced, manifested, or fingerprinted by Stage 1.

## Fingerprint behavior

Both helpers return lowercase SHA-256 hex over explicit projections serialized
as compact canonical JSON with sorted keys, UTF-8 text
(`ensure_ascii=False`), and non-finite numbers rejected. Lone Unicode
surrogates are rejected before UTF-8 encoding.

`pipeline_fingerprint(config)` includes exactly:

- embedding model ID/revision and tokenizer ID/revision; and
- Part I loader, parser, chunker, representation, embedding-dimension, BM25
  analyzer, Milvus-schema, and Milvus-index values.

`runtime_fingerprint(config)` includes exactly:

- reranker, query, and generation model IDs and revisions;
- query-construction version, retrieval-semantics version, sufficiency
  strategy/version, and reranker strategy; and
- generation-prompt version, Web-evidence strategy, compression strategy,
  citation-policy version, and output-policy version.

Infrastructure, cache, maintenance, and observability categories are excluded
from both projections. Hosts, ports, database/user names, paths, URLs, device
selection, log settings, API keys, and passwords therefore do not affect either
fingerprint. Construction or mapping order does not change a fingerprint. An
index-affecting value changes only the pipeline fingerprint, an
answer-affecting value changes only the runtime fingerprint, and
deployment-only changes alter neither.

## Request hash behavior

`request_hash(UserTurnRequest)` hashes this exact logical payload through
the same canonical JSON/SHA-256 primitive:

~~~text
{
  "query": request.query,
  "filters": request.filters.model_dump(mode="json", exclude_none=True) or null
}
~~~

`session_id` is deliberately excluded because it is the future cache
namespace. No filters and an all-null `QueryFilters` produce the same
hash; filter construction order is irrelevant; any supplied filter or query
change changes the hash. Validated query and filter text is preserved rather
than whitespace-normalized, so `"question"` and `" question "`
intentionally hash differently.

## SpanRecord safety and serialization

`SpanRecord` contains exactly `trace_id`, `span_id`,
`parent_span_id`, `stage`, `attempt`, `started_at`,
`ended_at`, `duration_ms`, `status`, `error_code`, and
`attributes`.

- Identifiers/stage are strict nonblank text; attempt is a strict nonnegative
  integer and duration is a finite nonnegative real number.
- Timestamps must be timezone-aware. Each is converted to canonical UTC before
  storage and comparison; out-of-range UTC conversions are rejected, and
  `ended_at` must not precede `started_at` by absolute instant.
  DST folds, cross-zone inputs, sub-minute offsets, JSON round trips, and equal
  timestamps are covered.
- `attributes` must be a JSON object. Values may recursively contain only
  null, booleans, integers, finite floats, strings, arrays, and string-keyed
  objects. Cycles, bytes, datetimes, sets, arbitrary objects, non-finite
  numbers, blank/non-string keys, and lone Unicode surrogates are rejected.
- Attribute input is deep-copied and deeply frozen: mappings become read-only
  mapping proxies and arrays become tuples. Later caller mutation cannot alter
  a record. Serialization thaws those structures to normal JSON objects/arrays,
  preserving semantic `model_dump_json()`/`model_validate_json()`
  round trips.
- Sensitive/control attribute keys are denied recursively after NFKC,
  case-fold, and punctuation-insensitive normalization. The blocked vocabulary
  covers Chain-of-Thought, system prompts/instructions, raw/full evidence,
  evidence content, raw HTML, authorization/auth headers, API keys, passwords,
  secrets, private/client credentials, access/refresh/bearer tokens, and
  production-forbidden experiment metadata, benchmark IDs, candidate strategy
  sets, evaluation databases, qrels, metric objects, and
  gold/expected-answer metadata, including the complete benchmark-only
  `GoldEvidenceAlignment` field vocabulary. Matching recognizes
  singular/plural normalized snake, punctuation-separated, and camel-case
  forms without rejecting allowed aggregate keys such as
  `passwordless_enabled` or ordinary observability names such as
  `golden_signal_latency_ms`.

Trace is not stored in graph State, and no trace backend is implemented.

## Tests added

Stage 1 adds 1,138 contract tests across 14 files:

| Test file | Coverage |
| --- | --- |
| `test_enums_and_errors.py` | Exact enum values, strict enum input, exception hierarchy/codes/messages, and safe reconstruction. |
| `test_requests_and_retrieval.py` | User/scope filters, request/candidate field exactness, strict validation, retrieval invariants, immutability, and public exports. |
| `test_evidence_contracts.py` | Evidence bridge, provenance, identity, local execution/sufficiency cross-field invariants, and serialization boundaries. |
| `test_web_contracts.py` | Web request/search/preparation/evidence schemas, URL/domain safety, status cardinality, and forbidden raw/provider fields. |
| `test_answer_contracts.py` | Citation namespaces/uniqueness, AnswerInput cardinality, working bundle, generation result, and output review vocabulary. |
| `test_result_contracts.py` | Discriminated local/Web final citations, source-specific fields, uniqueness, response, and RAG completion. |
| `test_graph_states.py` | Exact export order, total `TypedDict` identity, required/optional keys, and resolved annotation targets for all 15 schemas. |
| `test_state_prohibitions.py` | Recursive key/type/import/module guards against dependency, config, trace, client, history, raw-data, vector, conversation, and benchmark contamination. |
| `test_config_and_fingerprints.py` | All nine config schemas, strict bounds/revalidation, exact deterministic projections, change isolation, and deployment invariance. |
| `test_request_hashing.py` | Canonical repeatability, session exclusion, filter normalization/order, text preservation, and Unicode safety. |
| `test_trace_contracts.py` | Span shape, canonical UTC, instant ordering, deep JSON freeze, sensitive-key safety, and semantic round trips. |
| `test_nested_model_revalidation.py` | Rejection of forged/mutated nested Pydantic instances across contract boundaries. |
| `test_serialization_roundtrip.py` | JSON round trips for the nine required representative boundary/result models and recursive rejection of non-serializable surrogate text. |
| `test_stage1_scope.py` | Frozen production/dependency manifests, static/dynamic import guards, declarative graph-only policy, and absence of deferred implementations. |

The focused Task 6 pair
(`test_serialization_roundtrip.py` and
`test_stage1_scope.py`) contains 68 passing tests. The Stage 0 baseline
was 79 tests; 79 baseline + 1,138 Stage 1 tests = 1,217 tests in the full suite.

## Verification

Fresh implementation verification was run on 2026-09-28 with Python 3.11.16.
It was entirely offline and needed no secrets, external APIs, databases,
caches, vector stores, model services, or other network services.

| Command/gate | Result |
| --- | --- |
| Focused Task 6 pytest run | PASS — 68 passed |
| `pytest tests/contract` | PASS — 1,138 passed |
| `uv sync` | PASS — resolved 20 packages and checked 18 packages |
| `make format` | PASS — 59 files left unchanged |
| `make format-check` | PASS — 59 files already formatted |
| `make lint` | PASS — all Ruff checks passed |
| `make typecheck` | PASS — no issues in 51 source files |
| `make architecture-check` | PASS — import boundaries and forbidden tracked artifacts emitted no violations |
| `make test` | PASS — 1,217 passed |
| `make verify` | PASS — all local gates; 1,217 passed |
| Stage 0 regression baseline within full suite | PASS — all 79 pre-Stage-1 tests remain green |

The complete `origin/main...HEAD` diff plus the final uncommitted review fixes
and documentation were also audited against the frozen Stage scope: production
imports are limited to the standard library, AtlasRAG, and Pydantic; graph
modules are declarative State only; provider/repository/runtime/service/
observability packages remain docstring-only stubs; the dependency lock
contains no deferred runtime client; and no benchmark, secret, forbidden
artifact, or Stage 2 implementation was introduced.

Documentation and final-sequence checks after this report update:

| Check | Result |
| --- | --- |
| `make format-check` | PASS — 59 files already formatted |
| `make architecture-check` | PASS |
| `git diff --check` | PASS — no whitespace errors in tracked changes |
| New-report whitespace check | PASS — no whitespace errors |
| `git status --short` | PASS — only the reviewed Stage 1 fixes, tests, plan correction, and delivery documentation remain uncommitted |

## Architecture deviations

NONE

The implementation was compared with the authoritative Architecture Manual,
especially §§45, 46A, and 87, the Stage 1 scope/Definition of Done, the
committed implementation plan, the exact State reflection tests, the scope
regression suite, and the full Stage diff. No frozen architecture or contract
change was required.

## Known limitations

- This is a contracts/shared-kernel Stage; it provides no real business
  execution or external-service integration.
- PostgreSQL, Redis, Milvus, SearXNG, MinerU, Docling, embedding/reranking/model
  providers, Docker Compose, and network/service validation are deferred.
- The graph package defines State schemas only; it has no LangGraph runtime
  dependency, compiled graph, node, or routing execution.
- Configuration loading, secret acquisition, runtime bootstrap/DI, hot reload,
  and RuntimeGate are not implemented.
- `SpanRecord` is a contract only; no trace persistence/export backend is
  present.
- Fingerprint and request-hash helpers define identities only; later stages
  will integrate them with indexing, manifests, sessions, and caches.

## Git branch, commit, push, and CI

- Branch: `stage/01-contracts-shared-kernel`
- Verified implementation Stage commit:
  `837106afd6704b9ce84fb4e0ce530f52bc495f27`
- GitHub push: **PASS — branch published normally to
  `origin/stage/01-contracts-shared-kernel`**
- Remote CI: **PASS — GitHub Actions CI run
  [36376129832](https://github.com/Melon235/AtlasRAG/actions/runs/36376129832)**
- Completion-status documentation: recorded by the documentation-only
  successor commit containing this report; its resulting HEAD and CI are
  reported in the final handoff to avoid self-referential commit metadata
- Force push: not used and prohibited
- Integration: no merge performed

The repository finalizer reran `make verify`, checked staged artifacts, created
the verified implementation commit, and pushed normally. This documentation
update changes status/evidence only; it does not alter the Stage 1 contracts.

## Next

Stage 2 — Infrastructure & Persistence: **NOT STARTED**.

Do not begin Stage 2 as part of Stage 1 publication.
