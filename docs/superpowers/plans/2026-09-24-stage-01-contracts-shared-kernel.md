# Stage 1 Contracts & Shared Kernel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Every production behavior follows test-driven development: add a focused test, run it and confirm the expected failure, add the minimum implementation, then rerun the focused and regression tests.

**Goal:** Implement the frozen AtlasRAG v1 domain contracts, graph-state schemas, error/trace contracts, configuration schemas, deterministic fingerprints, and request hashing without introducing Stage 2 business infrastructure.

**Architecture:** Pydantic v2 frozen models with forbidden unknown fields are the sole canonical DTO representation. Python 3.11 `TypedDict` schemas carry only current graph execution data. Stable string enums and one canonical JSON/hash primitive keep serialization, fingerprints, and request hashes deterministic while preserving the Architecture Manual's Evidence and State boundaries.

**Tech Stack:** Python 3.11, Pydantic v2, `typing.TypedDict`, SHA-256, pytest, mypy strict, Ruff, uv.

---

## File map

- `src/atlasrag/domain/base.py`: shared frozen/extra-forbid Pydantic base and reusable scalar validators.
- `src/atlasrag/domain/enums.py`: stable string enums only.
- `src/atlasrag/domain/errors.py`: `ErrorCode` and typed AtlasRAG technical exception hierarchy.
- `src/atlasrag/domain/requests.py`: user request, typed filters, retrieval request, and resolved scope.
- `src/atlasrag/domain/retrieval.py`: scores, candidates, branch result, and ordered candidate pool.
- `src/atlasrag/domain/evidence.py`: evidence references, canonical local evidence/provenance, and local-result invariants.
- `src/atlasrag/domain/web.py`: Web request/search/prepared/evidence/result contracts.
- `src/atlasrag/domain/answer.py`: Answer boundary wrappers, `AnswerInput`, generation/review contracts, and temporary working bundle.
- `src/atlasrag/domain/results.py`: discriminated final citations, `FinalResponse`, and `RagCoreResult`.
- `src/atlasrag/domain/trace.py`: JSON-safe `SpanRecord` contract.
- `src/atlasrag/graphs/state/*.py`: exact frozen TypedDict input/state/output schemas; no graph execution.
- `src/atlasrag/config/models.py`: immutable extra-forbid configuration categories and aggregate `RuntimeConfig`.
- `src/atlasrag/config/fingerprint.py`: canonical pipeline/runtime fingerprint projections and SHA-256.
- `src/atlasrag/application/hashing.py`: request hash over query plus filters, excluding session ID.
- `tests/contract/*.py`: focused contract, state, serialization, hashing, trace, and no-business-implementation tests.
- `docs/stages/stage-01-contracts-shared-kernel.md`: evidence-bearing Stage report.
- `docs/stages/README.md` and repository `README.md`: Stage 1 status only; never claim Stage 2 work.

### Task 1: Shared primitives, enums, and technical errors

**Files:**
- Modify: `pyproject.toml`, `uv.lock`
- Create: `src/atlasrag/domain/base.py`
- Create: `src/atlasrag/domain/enums.py`
- Create: `src/atlasrag/domain/errors.py`
- Test: `tests/contract/test_enums_and_errors.py`

- [ ] Add tests that import every frozen Stage 1 enum, assert exact member values, reject enum aliases/unknown strings through a small Pydantic probe model, and verify the requested exception inheritance and fixed `ErrorCode` values.
- [ ] Run `uv run --frozen pytest tests/contract/test_enums_and_errors.py -q`; confirm collection/import fails because the modules do not exist.
- [ ] Add `pydantic>=2.12.0,<3.0.0` as the only Stage 1 runtime dependency and refresh the uv lock without adding LangGraph or service clients.
- [ ] Implement `FrozenModel` with `ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)` and stable string enums for source/retrieval/evidence/Web/answer/final-result/trace concepts.
- [ ] Implement `ErrorCode`, `AtlasRAGError`, `DependencyError`, `DependencyUnavailableError`, `DependencyTimeoutError`, `ProviderResponseError`, `CanonicalDataError`, `InvariantViolationError`, `ResourceExhaustedError`, `ConfigurationError`, and `SourceMutationError`; normal negative domain outcomes remain enums, never exceptions.
- [ ] Rerun the focused test, then `make typecheck` and `make lint`; all must pass.
- [ ] Commit as `stage(01): add shared enums and error contracts`.

### Task 2: User, retrieval, local evidence, and Web contracts

**Files:**
- Create: `src/atlasrag/domain/requests.py`
- Create: `src/atlasrag/domain/retrieval.py`
- Create: `src/atlasrag/domain/evidence.py`
- Create: `src/atlasrag/domain/web.py`
- Modify: `src/atlasrag/domain/__init__.py`
- Test: `tests/contract/test_requests_and_retrieval.py`
- Test: `tests/contract/test_evidence_contracts.py`
- Test: `tests/contract/test_web_contracts.py`

- [ ] Write failing request tests for trimmed non-empty `session_id`/`query`, typed `QueryFilters` allowlist, unknown-field rejection, request immutability, `LocalRetrievalRequest` field exactness, and a typed non-dict `ResolvedRetrievalScope`.
- [ ] Run the request test and confirm missing imports are the failure cause; implement only the request/filter/scope models and rerun to green.
- [ ] Write failing retrieval tests for optional score slots, finite numbers, TEXT_CHILD parent requirement, TABLE optional parent, TEXT_PARENT rejection, ordered tuple semantics, branch EMPTY versus UNAVAILABLE, and exact DTO field sets.
- [ ] Implement `CandidateScores`, `RetrievalCandidate`, `RetrievalBranchResult`, and `CandidatePool`; lists become immutable tuples after validation and no historical rank fields are added.
- [ ] Write failing local-evidence tests for EvidenceRef type restrictions, stable identity `(document_id, revision_id, evidence_type, context_id)`, absence of Child fields, minimal typed `SourceAnchor`/`LocalProvenance`, and every `LocalEvidenceResult` cross-field invariant including DEGRADED+SUFFICIENT.
- [ ] Implement the local Evidence contracts and rerun the focused tests to green.
- [ ] Write failing Web tests for one-field `WebEvidenceRequest`, HTTP(S) URL/domain/rank validation, content origin/representation enums, immutable evidence tuples, and EMPTY versus UNAVAILABLE with no technical error payload.
- [ ] Implement Web DTOs without raw HTML, generic scores, trust classes, citation IDs, exceptions, or provider responses.
- [ ] Run all three focused test files, `make typecheck`, and `make lint`.
- [ ] Commit as `stage(01): implement retrieval and evidence contracts`.

### Task 3: Answer boundary and final result contracts

**Files:**
- Create: `src/atlasrag/domain/answer.py`
- Create: `src/atlasrag/domain/results.py`
- Modify: `src/atlasrag/domain/__init__.py`
- Test: `tests/contract/test_answer_contracts.py`
- Test: `tests/contract/test_result_contracts.py`

- [ ] Write failing tests for `L[1-9][0-9]*` and `W[1-9][0-9]*` citation IDs, global uniqueness, immutable authoritative citation mapping, and the SUFFICIENT / INSUFFICIENT_BEST_MATCH / NONE cardinality invariants.
- [ ] Implement `AnswerLocalEvidence`, `AnswerWebEvidence`, `AnswerInput`, and a typed temporary `WorkingEvidenceBundle`; AMBIGUOUS has no Answer-layer enum value.
- [ ] Write failing tests for `GenerationResult` citation-ID validation/uniqueness and the stable `OutputReviewResult` decision/reason-code vocabulary with no retry counter.
- [ ] Implement the generation/review contracts and rerun focused tests.
- [ ] Write failing tests for the LOCAL/WEB discriminated final-citation union, fixed source kinds, source-specific fields, duplicate citation rejection, and `RagCoreResult` completion status with no `cache_eligible` field.
- [ ] Implement `FinalLocalCitation`, `FinalWebCitation`, `FinalResponse`, and `RagCoreResult` without transport/session/trace metadata.
- [ ] Run both focused test files, `make typecheck`, and `make lint`.
- [ ] Commit as `stage(01): implement answer and result contracts`.

### Task 4: Exact graph-state schemas and prohibitions

**Files:**
- Create: `src/atlasrag/graphs/state/__init__.py`
- Create: `src/atlasrag/graphs/state/retrieval.py`
- Create: `src/atlasrag/graphs/state/rag_core.py`
- Create: `src/atlasrag/graphs/state/local_evidence.py`
- Create: `src/atlasrag/graphs/state/web_evidence.py`
- Create: `src/atlasrag/graphs/state/answer.py`
- Test: `tests/contract/test_graph_states.py`
- Test: `tests/contract/test_state_prohibitions.py`

- [ ] Write failing reflection tests that compare every input/state/output `__required_keys__`, `__optional_keys__`, and annotation target against Manual §46A.7.
- [ ] Implement the exact Python 3.11 `TypedDict` schemas using `Required`/`NotRequired`; only the two frozen graph-semantic counters are present.
- [ ] Write failing schema-level prohibition tests that reject forbidden key/type vocabulary for providers, repositories, services, database/HTTP clients, config/retry/trace/log/exception/history/raw HTML/vectors/conversation/benchmark data.
- [ ] Confirm all state tests pass and production state modules contain no LangGraph execution, SQL, Redis, HTTP, or dependency container.
- [ ] Run the focused tests, `make typecheck`, and `make architecture-check`.
- [ ] Commit as `stage(01): add typed graph state schemas`.

### Task 5: Configuration, fingerprints, request hashing, and trace

**Files:**
- Create: `src/atlasrag/config/models.py`
- Create: `src/atlasrag/config/fingerprint.py`
- Create: `src/atlasrag/application/hashing.py`
- Create: `src/atlasrag/domain/trace.py`
- Modify: `src/atlasrag/config/__init__.py`
- Modify: `src/atlasrag/application/__init__.py`
- Test: `tests/contract/test_config_and_fingerprints.py`
- Test: `tests/contract/test_request_hashing.py`
- Test: `tests/contract/test_trace_contracts.py`

- [ ] Write failing tests for all eight immutable/extra-forbid configuration categories plus aggregate `RuntimeConfig`; secrets/deployment coordinates are schema data but never fingerprint inputs.
- [ ] Implement explicit, minimally sufficient version/model/strategy/operational fields without `.env` loading, hot reload, clients, or a DI container.
- [ ] Write failing fingerprint tests for repeatability, construction/map-order independence, index-affecting changes, answer-affecting changes, and deployment-only invariance.
- [ ] Implement canonical UTF-8 JSON (`sort_keys=True`, compact separators) and SHA-256 over explicit pipeline/runtime projections; never hash hosts, ports, passwords, API keys, or log paths.
- [ ] Write failing request-hash tests proving dependence on normalized query+filters and independence from `session_id`; implement it using the same canonical serialization semantics.
- [ ] Write failing `SpanRecord` tests for timezone-aware ordered timestamps, non-negative attempt/duration, stable trace status/error code, JSON-only typed attributes, unknown-field rejection, and forbidden sensitive/control attribute names.
- [ ] Implement Trace contracts only; do not add a backend or put Trace in State.
- [ ] Run all focused tests, `make typecheck`, and `make lint`.
- [ ] Commit as `stage(01): add config hashing and trace contracts`.

### Task 6: Boundary serialization and scope regression suite

**Files:**
- Create: `tests/contract/test_serialization_roundtrip.py`
- Create: `tests/contract/test_stage1_scope.py`
- Modify: Stage 1 modules only when a failing round-trip or scope test exposes a contract defect.

- [ ] Write parameterized JSON round-trip tests for `UserTurnRequest`, `CandidatePool`, `LocalEvidenceResult`, `WebEvidenceResult`, `AnswerInput`, `GenerationResult`, `FinalResponse`, `RagCoreResult`, and `SpanRecord` using `model_dump_json()` then `model_validate_json()` semantic equality.
- [ ] Write an AST/dependency regression test proving Stage 1 does not import psycopg, redis, pymilvus, MinerU, Docling, torch, DeepSeek clients, SearXNG clients, benchmarks, or tests, and does not implement graph execution/business providers.
- [ ] Run the new tests first and fix only genuine contract/serialization defects.
- [ ] Run all `tests/contract`, then the entire existing test suite.
- [ ] Commit as `test(stage-01): verify serialization and scope boundaries`.

### Task 7: Documentation, full verification, publication, and remote CI

**Files:**
- Create: `docs/stages/stage-01-contracts-shared-kernel.md`
- Modify: `docs/stages/README.md`
- Modify: `README.md`

- [ ] Update status text to `Stage 1 COMPLETE` and `Next: Stage 2 NOT STARTED` only after implementation gates pass; document contracts, enums, states, errors, fingerprints, request hash, tests, limitations, and architecture deviations.
- [ ] Run, in order, `uv sync`, `make format`, `make format-check`, `make lint`, `make typecheck`, `make architecture-check`, `make test`, and `make verify`; do not skip, xfail, or weaken a gate.
- [ ] Run `git diff --check`, inspect `git status`, and audit the complete `origin/main...HEAD` diff for secrets, forbidden artifacts, benchmark contamination, or Stage 2 implementation.
- [ ] Request an independent whole-stage spec review, then an independent code-quality review; fix every Critical/Important issue and rerun verification.
- [ ] Complete the Stage report with actual test counts, branch, commit/publication/CI evidence, and `Architecture deviations: NONE` only if verified.
- [ ] Use `scripts/finalize_stage.sh stage/01-contracts-shared-kernel docs/stages/stage-01-contracts-shared-kernel.md "stage(01): implement contracts and shared kernel"` for the final report/status commit and normal push; never force-push or merge.
- [ ] Wait for the Stage branch CI run to finish successfully. If push or CI fails, Stage 1 remains NOT COMPLETE until fixed, recommitted, repushed, and green.
- [ ] Stop after reporting Stage 1. Do not start PostgreSQL, Redis, Milvus, Docker Compose, providers, graphs, RuntimeGate, CLI business commands, Web UI, benchmarks, or Stage 2.

