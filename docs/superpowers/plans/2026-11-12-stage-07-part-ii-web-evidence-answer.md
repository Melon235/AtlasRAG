# AtlasRAG Stage 7 — Part II: Web Evidence & Answer

## 1. Status and authority

- Stage 6 is closed at `528fcf6eb34bb89058edf5f01fec92c335eeb8d5`.
- Branch: `stage/07-part-ii-web-evidence-answer`.
- Baseline: **3144 passed, 101 deselected** (`make verify`).
- Authority: Architecture Manual §25.4–25.5, §37–44, §46A.4–46A.8; frozen
  Stage 1 DTO/State contracts; Stage 2–6 storage/provider boundaries.
- No database migration. No Stage 8 orchestration, Query Construction,
  RuntimeGate, Query Cache, CLI/Web UI, or benchmark harness.

### Approved provider amendment

The repository contains SearXNG HTTP infrastructure (`searxng_url`, compose
service, localhost-only settings) but no MCP service, MCP dependency, or
tool/response contract. Inventing an MCP tool schema is forbidden. The user
approved a narrow amendment: use the existing SearXNG **native HTTP JSON**
search endpoint (`format=json`), behind a stable search-provider interface.
MCP transport is explicitly deferred; no MCP tool name/schema is claimed.

## 2. Scope

Two independent subgraphs:

```text
web_evidence_subgraph
  search_web → prepare_web_sources → build_web_evidence → END

answer_subgraph
  optional_compression → generate_answer → review_output
    ├─ ACCEPT → resolve_citations
    ├─ REGENERATE → generate_answer
    └─ BLOCK → safe terminal failure
```

Business outputs are the frozen `WebEvidenceResult` and
`(FinalResponse, AnswerExecutionOutcome)`. AnswerInput is supplied by the
caller/Stage 8; Stage 7 does not build it and does not select evidence.

## 3. Web search provider

`SearchProvider` protocol:

```text
async search(query: str) -> tuple[WebSearchResult, ...]
```

`SearxngSearchProvider` calls the configured SearXNG URL with
`GET /search?q=<urlencoded>&format=json`, bounded connect/read timeout, at most
two technical retries for transport/HTTP 429/5xx, and no retries for malformed
contracts. `json` is added to the local SearXNG settings formats.

Normalization:
- top 5 results from `results[]`, preserving provider order as `search_rank`;
- title/snippet bounded and nonblank;
- URL parsed by the frozen `WebSearchResult` HttpUrl contract; domain is
  derived from the validated URL host, never trusted from the vendor;
- userinfo, empty/invalid hosts, unsupported URLs are rejected;
- invalid response shape or all-invalid results → typed provider error;
- dependency/timeout → typed unavailable; zero results is a normal empty tuple.

No vendor response, raw JSON, or exception enters Graph State.

## 4. Safe fetch threat model (critical)

`SafeHttpFetcher` accepts only `http`/`https`, no userinfo, no NUL, an explicit
host, no trailing dot, and ports 80/443 only. The URL hostname is IDNA-normalized
and rejected if it is `localhost`, a single-label name, `.local`, `.internal`, or
an IP literal that is not globally routable.

The fetcher resolves the hostname once with `socket.getaddrinfo`, validates
**every** returned IPv4/IPv6 address with `ipaddress.ip_address(...).is_global`,
and rejects the request if any address is loopback/private/link-local/reserved/
multicast/unspecified (including `169.254.169.254`). It then connects directly
to one validated numeric IP, sends the original hostname in `Host`, and for TLS
uses that hostname as SNI with the default certificate verification context.
DNS is not re-resolved for the connection, preventing DNS rebinding.

Redirects (301/302/303/307/308) are followed manually, maximum 5; every target is
reparsed, revalidated, and re-resolved/pinned. A redirect to a private address is
rejected. No proxy environment is consulted; no cookies/auth are forwarded.
Responses must have an allowed text content type, no unsupported
`Content-Encoding`, a bounded declared/actual body, and a bounded timeout.
Streaming reads enforce the byte cap; decompression is not used by default.

The implementation uses stdlib `http.client`, `socket`, `ssl`, and `ipaddress` in
one exact provider file; the scope guard permits only that file. Tests inject the
resolver/connection boundary to prove private IPv4/IPv6, metadata, redirect,
DNS-pinning, content-type, and oversize rejection without external network.

## 5. HTML extraction, sanitization, budgets

`HtmlSanitizer` uses `html.parser.HTMLParser` and produces plain bounded text:
- drops script/style/noscript/template/svg/iframe/object/embed/form/input/button/
  nav/footer/header/aside content;
- drops elements with `hidden`, `aria-hidden=true`, or inline display:none/
  visibility:hidden;
- keeps title (from `<title>`/first heading) and visible body text;
- normalizes whitespace, removes control characters, decodes entities;
- never treats page text as instructions; all prepared content is labelled
  UNTRUSTED_EXTERNAL_CONTENT in the generation boundary.

`prepare_web_sources` performs fetch + sanitize + budget inside one graph node;
raw HTML/HTTP objects never cross the node. Budgets are explicit immutable
profile values: per-page chars, total chars, max concurrent fetches, max bytes,
max redirects, timeout. Sources are processed in search-rank order; budget
allocation is deterministic and preserves rank.

A source whose fetch fails falls back to its safe snippet as
`SEARCH_SNIPPET`; a source with no usable snippet is dropped. A security-policy
rejection never reintroduces the URL through a snippet identity.

## 6. Web evidence strategies

`build_web_evidence` keeps the frozen graph node. Two interchangeable strategies
return `WebEvidence[]`:

- `DirectWebEvidenceStrategy`: bounded sanitized content → `SANITIZED_CONTENT`,
  snippet fallback → `SEARCH_SNIPPET` (default baseline; deterministic).
- `BatchSummaryWebEvidenceStrategy`: one batch provider call for all prepared
  sources, preserving each source's validated title/URL/domain/rank; summary
  failure falls back to the direct strategy and reports degradation.

The one-batch summary is a benchmark candidate, not a quality claim. The
production default remains Stage 9 benchmark-selected. No source/URL/title is
generated by the summary provider.

Status semantics:
- search dependency failure → `UNAVAILABLE`;
- successful search with zero usable evidence → `EMPTY`;
- some source degradation/fetch failure/summary fallback with usable evidence
  → `DEGRADED`;
- all sources fetched/summarized normally → `OK`.

## 7. Answer input boundary

Stage 7 consumes the frozen `AnswerInput` unchanged. It never rebuilds evidence
selection, never assigns citation IDs, and never calls retrieval, reranking,
SearXNG, or Web fetch from the answer subgraph.

`WorkingEvidenceBundle` is the temporary bounded representation. The identity
compression strategy (default NONE) returns the same citation mapping. A future
deterministic or model-based compressor must preserve citation IDs and canonical
objects; failure falls back to the original bundle.

`GenerationInputBuilder` creates a generation-only view with separate
`local_knowledge` and `web_search` sections, citation IDs, local display names,
provenance where relevant, and bounded content. Canonical evidence is never
mutated or permanently truncated. Whitespace-only canonical local content is
admitted by contract; it is passed as data, not treated as evidence of a fact.

## 8. Generation provider and prompt boundary

`GenerationProvider` protocol:

```text
async generate(input: GenerationInput) -> GenerationResult
```

`DeepSeekGenerationProvider` uses the configured generation model identity, the
SecretStr API key, a bounded timeout, and bounded technical retries. It sends
the original query and bounded evidence JSON in the user message; control
instructions live only in a separate system message. Evidence is labelled
untrusted data, no tools are provided, and no chain-of-thought is persisted.

Structured output is parsed and validated into `GenerationResult`; malformed
provider output becomes a typed protocol error so the graph can regenerate.
Dependency failures remain technical failures (not SAFE_FAILURE).

## 9. Output review, regeneration, citations

`OutputReviewService` is deterministic:
- schema/answer text integrity;
- only L/W citation IDs present in AnswerInput;
- citation markers in answer_text exactly match `used_citation_ids`;
- URLs in prose must match an authorized Web evidence URL (ignoring fenced code);
- exact internal prompt sentinel / tool-protocol markers → control/protocol
  leakage;
- unknown citation IDs, forged URLs, source-boundary violations → BLOCK;
- invalid output schema or correctable citation mismatch → REGENERATE.

Review does not attempt factual truth and is not an LLM judge. It cannot prove
natural-language correctness.

`max_regenerations = 1` is a node hyperparameter. `generation_attempt_index` is
the only State counter. Regeneration reuses the immutable AnswerInput and
citation mapping; it never retrieves/Web-fetches. Exhausted regeneration or
BLOCK produces `SAFE_FAILURE` with a controlled non-evidence message and no
fabricated citations.

`CitationResolver` consumes only the reviewed GenerationResult and immutable
AnswerInput. It resolves citation IDs in first-appearance order:
- local → canonical provenance (`file_name`, relative path, evidence type,
  source anchor); basename display unique, relative path display when ambiguous;
- web → authoritative title/URL/domain from AnswerInput.
No unknown ID can reach resolution; no absolute path is exposed.

## 10. Graph/state/dependency rules

Both graphs use the frozen minimal `WebEvidenceState` and `AnswerState`. Nodes
call injected services only. Providers, HTTP clients, API keys, raw responses,
raw HTML, vectors, retry controls, and exceptions never enter State. No
checkpointer/request recovery is added. Dependencies are injected outside State.

## 11. Test matrix

Web: search success/zero/unavailable/malformed; normalization/rank; URL/domain
binding; SSRF IPv4/IPv6/loopback/private/metadata/credentials/trailing-dot;
redirect revalidation; DNS rebinding pinning; content-type/oversize/timeout;
sanitization/hidden/script/nav; snippet fallback; budgets/ordering; summary
fallback; fabricated identity absence; raw HTML absent from State.

Answer: local-only/web-only/mixed; INSUFFICIENT_BEST_MATCH; whitespace evidence;
identity compression; compression fallback; structured success/malformed;
unknown/mismatched citations; forged URL; control leakage; legitimate prompt
injection discussion; regeneration success/exhaustion/BLOCK; programming error
propagation; citation order/only-used/duplicate filename/no absolute path; no
retrieval/Web/LLM client in State.

Integration uses local controlled HTTP fixtures for SearXNG/DeepSeek transport,
fake model providers where applicable, and the real HTTP provider boundary. A
separate `stage7-provider-smoke` requires explicit credentials/network; absent
credentials are reported as unverified live acceptance, never as a fake pass.

## 12. Commands/CI

```text
make test-stage7
make test-stage7-integration
make stage7-verify
make stage7-provider-smoke
```

`make verify` stays offline/CPU-safe/Docker-free/model-free/API-key-free. CI adds
a Stage 7 job running local transport integration only; no live search or paid
API calls.

## 13. Definition of Done

All frozen DTO/State negative tests, offline suite, Stage 2–6 regressions,
Stage 7 local integration, SSRF/redirect/DNS tests, generated-output review and
regeneration tests, citation integrity tests, scope/architecture checks, stage
report, normal push, final-HEAD CI/Integration green, and an unmerged PR. Stage 8
must not start.
