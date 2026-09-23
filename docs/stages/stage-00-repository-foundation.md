# AtlasRAG Stage 0 — Repository Foundation

**Status:** LOCAL COMPLETE — PUBLICATION PENDING

Stage 0 is not complete until the Stage commit is pushed successfully and the
remote CI result is green. This report records the verified local,
pre-publication state; the publication finalizer has not yet run.

## Scope

Stage 0 establishes only the repository foundation: packaging, deterministic
quality gates, architecture boundaries, documentation, and the Git/GitHub Stage
workflow. It implements no Part I–IV business logic.

## Initial baseline

Historical migration evidence was captured by running the legacy template test
suite before cleanup:

- 2 tests were collected.
- The result was `1 passed, 1 failed`.
- The failed test was
  `tests/integration_tests/test_graph.py::test_retrieval_graph`.
- The legacy test used the LangSmith `@unit` integration and made a mandatory
  external connection to `api.smith.langchain.com`. Without network access or a
  LangSmith API key, it failed with a LangSmith connection/proxy error.

The failing test was obsolete template inheritance. It was removed rather than
skipped or preserved with a LangSmith mock. No AtlasRAG code depended on it. The
legacy LangSmith and LangGraph dependencies and configuration were also removed
because Stage 0 intentionally has no RAG stack runtime dependencies. LangGraph
remains part of the frozen AtlasRAG architecture and is deferred to the first
Stage that uses it.

## Implemented

- Installed the authoritative Architecture Manual at
  `docs/architecture/AtlasRAG_Complete_Final_Architecture_Design_Manual.md`.
  `cmp` confirms that it is byte-identical to the supplied source, and its
  SHA-256 is
  `69cd88716909401acab8132a290c6f12f7b509e6da058f035046eba1f3796124`.
- Established the `src/atlasrag` package skeleton for application, config,
  domain, graphs, observability, providers, repositories, runtime, and services.
- Bounded the project to Python `>=3.11,<3.12`, added a frozen `uv` lock, kept
  runtime dependencies empty, and limited development dependencies to bounded
  versions of mypy, pytest, pytest-cov, and Ruff.
- Removed the old retrieval-template package, tests, workflows, LangGraph
  launcher configuration, static asset, and template-only dependencies.
- Added repository rules in `AGENTS.md`, focused project and Stage documentation,
  a non-secret environment example, and ignore policies for secrets, runtime
  data, benchmark data, model artifacts, local service volumes, and tool output.
- Added an AST-based import-boundary checker that rejects production imports
  from `tests` or `benchmarks` and fails closed on invalid production source.
- Added a Git-index-based forbidden-artifact checker for secrets, environments,
  caches, runtime and service data, benchmark outputs, model weights, coverage
  output, and editor state.
- Added the stable Make interface: `setup`, `format`, `format-check`, `lint`,
  `typecheck`, `architecture-check`, `test`, and `verify`.
- Added secret-free, service-free CI that installs Python 3.11 and `uv`, performs
  a frozen sync, and runs `make verify` without PostgreSQL, Redis, Milvus,
  SearXNG, LangSmith, or model services.
- Added a reusable safe Stage finalizer that requires the expected branch and a
  configured `origin`, runs the gates, requires the report, validates staged
  content, creates one commit, and pushes without force. It does not validate the
  origin URL. Its diagnostics distinguish missing remotes, authentication
  failures, and other push failures, and redact scheme-form URLs from displayed
  push stderr.
- Added fully offline tests for package structure, architecture boundaries,
  forbidden artifacts, and finalizer behavior.

## Files added or changed

- Governance and documentation: `AGENTS.md`, `README.md`, `benchmarks/README.md`,
  `docs/architecture/`, `docs/stages/`, and `.env.example`.
- Packaging and repository policy: `pyproject.toml`, `uv.lock`,
  `.python-version`, `.gitattributes`, `.gitignore`, and `Makefile`.
- Production skeleton: `src/atlasrag/` and its nine required regular
  subpackages.
- Quality and publication tooling: `scripts/check_import_boundaries.py`,
  `scripts/check_forbidden_tracked_files.py`, `scripts/finalize_stage.sh`, and
  `.github/workflows/ci.yml`.
- Reserved structure: `deploy/local/`, `tests/unit/`, `tests/contract/`, and
  `tests/integration/`.
- Offline tests: `tests/test_repository_foundation.py`,
  `tests/test_import_boundaries.py`, `tests/test_forbidden_tracked_files.py`, and
  `tests/test_finalize_stage.py`.
- Deleted legacy template categories: `src/retrieval_graph/`, template unit and
  integration tests plus their shared configuration, `langgraph.json`, legacy
  CI workflows, the Studio UI image, `.codespellignore`, and template-only
  package/lock entries.

## Tests added

- `test_repository_foundation.py` verifies the root package, all nine required
  subpackage imports, Architecture Manual presence, and repository structure.
- `test_import_boundaries.py` verifies the real repository and exercises all
  prohibited import forms, allowed lookalikes, deterministic diagnostics, and
  fail-closed syntax handling.
- `test_forbidden_tracked_files.py` exercises allowed paths, every forbidden
  artifact family, staged and repository-wide checks, nested invocation,
  deterministic ordering, control-character escaping, undecodable Git path
  bytes, and unusual repository-root names.
- `test_finalize_stage.py` exercises wrong-branch, missing-origin,
  missing-report, failed-verification, forbidden-file, empty-commit, successful
  local push, rejected-push, authentication classification, and URL-redaction
  behavior against temporary local Git repositories.

The current independent test result is `78 passed`.

## Verification

Verification ran with Python 3.11.16. The following were session-only inputs:

```text
UV_CACHE_DIR=/tmp/atlasrag-uv-cache
UV_PYTHON_INSTALL_DIR=/tmp/atlasrag-uv-python
```

LangSmith, LangChain tracing, DeepSeek, PostgreSQL, Redis, Milvus, and SearXNG
environment variables were unset for the run. No network service was required.

| Command | Result |
| --- | --- |
| `uv sync --frozen --python /tmp/atlasrag-uv-python/cpython-3.11.16-linux-x86_64-gnu/bin/python3.11` | PASS — frozen environment checked 14 packages |
| `make format-check` | PASS — 22 files already formatted |
| `make lint` | PASS — all Ruff checks passed |
| `make typecheck` | PASS — no issues in 16 source files |
| `make architecture-check` | PASS — import-boundary and forbidden-tracked-file checks emitted no diagnostics |
| `make test` | PASS — 78 collected, 78 passed |
| `make verify` | PASS — format, lint, type, architecture, and test gates all passed; 78 tests passed |
| `git status --short` before this report | PASS — clean worktree |
| `git status --short` after this report | PASS — only `?? docs/stages/stage-00-repository-foundation.md` |
| `git diff --check` after this report | PASS — no whitespace errors in tracked changes |

Ignore-policy evidence from `git check-ignore -v`:

```text
.gitignore:2:.env                .env
.gitignore:30:/benchmark_data/  benchmark_data/example.json
.gitignore:25:/runtime/         runtime/example.json
.gitignore:32:/models/          models/example.safetensors
```

The full gate was rerun after this report was created, while the report remained
untracked. The finalizer will stage it and run `git diff --cached --check` plus
the staged forbidden-artifact check. No remote CI claim is made here.

## Known limitations

- Stage 0 intentionally provides no database, vector-store, cache, Web-search,
  model, graph, business CLI, or application runtime implementation.
- PostgreSQL, Redis, Milvus, SearXNG, model, and other service integration
  validation is deferred to the Stage that introduces each integration.
- This report is in the pre-publication state and remains uncommitted until the
  reviewed finalizer creates the required final Stage commit and pushes it.

## Architecture deviations

NONE

## Git and publication

- Branch: `stage/00-repository-foundation`
- Pre-finalization HEAD:
  `05de63175fa388f187d0c98cf676348b61d0619c`
- Expected origin: `https://github.com/Melon235/AtlasRAG.git`
- Publication: `PENDING — finalizer not yet run`
- Remote CI: `PENDING`

## Next

Stage 1 — Contracts & Shared Kernel. Do not begin Stage 1 as part of this work.
