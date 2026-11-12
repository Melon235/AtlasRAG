# Stage 7 — Part II: Web Evidence & Answer

## 1. Status

**IMPLEMENTATION COMPLETE — LOCAL/REGRESSION GATES PASS; REAL EXTERNAL
PROVIDER ACCEPTANCE PENDING.**

Stage 6 was merged and closed at
`528fcf6eb34bb89058edf5f01fec92c335eeb8d5`. Stage 7 is based on that exact
`origin/main` commit.

| Item | Value |
| --- | --- |
| Base | `528fcf6eb34bb89058edf5f01fec92c335eeb8d5` |
| Branch | `stage/07-part-ii-web-evidence-answer` |
| Migration | **NONE** |
| Stage 8 / RagCore / Query Cache / RuntimeGate | **NOT IMPLEMENTED** |

## 2. Approved provider amendment

The repository has SearXNG HTTP infrastructure but **no MCP service, dependency,
or tool/response contract**. The user approved a narrow amendment: use the
existing SearXNG native HTTP JSON endpoint behind a stable search-provider
interface; add `json` to the local SearXNG settings. MCP transport is explicitly
deferred; no MCP tool name or response schema is claimed.

## 3. Delivered scope

### web_evidence_subgraph

```text
search_web → prepare_web_sources → build_web_evidence → END
```

- `SearxngSearchProvider`: bounded HTTP JSON search, top-5, bounded retries,
  validated structured response, provider-order search ranks, domain derived
  from the validated URL (never trusted from the vendor).
- `SafeHttpFetcher`: http/https only, no userinfo, hostname/IDNA/port policy,
  one DNS resolution with every address required to be globally routable,
  numeric-IP pinned connection, Host/SNI preserved, manual redirects with full
  revalidation (max 5), no proxy environment, identity encoding only, bounded
  allowed content types/bytes/timeouts.
- `HtmlSanitizer`: stdlib HTML parser; drops scripts/styles/hidden/navigation/
  active content; bounded plain text; page content remains untrusted data.
- `WebPreparationService`: fetch + sanitize + per-page/total budget inside one
  node; deterministic rank order; snippet fallback only for valid URL/transient
  failures; security-rejected URLs are dropped and never reintroduced.
- `DirectWebEvidenceStrategy` (default) and
  `BatchSummaryWebEvidenceStrategy` (one batch call, fallback to direct).
- Frozen `WebEvidenceResult` OK/DEGRADED/EMPTY/UNAVAILABLE; no citation IDs.

### answer_subgraph

```text
optional_compression → generate_answer → review_output
  ├─ ACCEPT → resolve_citations → FinalResponse
  ├─ REGENERATE → generate_answer (max 1)
  └─ BLOCK → controlled SAFE_FAILURE
```

- Identity compression baseline; failure falls back to original immutable
  AnswerInput mapping.
- `GenerationInputBuilder`: bounded local/web sections, citation IDs, display
  names, no canonical mutation; whitespace-only local content is admitted.
- `DeepSeekGenerationProvider`: configured model, SecretStr key, bounded
  timeout/retries, JSON-object response, strict `GenerationResult` validation;
  system controls separated from untrusted evidence; no tools.
- `OutputReviewService`: schema/citation set/marker consistency, unknown IDs,
  unauthorized prose URLs, control sentinel/protocol marker checks; no crude
  phrase blocking and no factual LLM judge.
- `CitationResolver`: only reviewed GenerationResult + immutable AnswerInput;
  first-appearance order; only used citations; authoritative local provenance
  and Web title/URL/domain; basename disambiguation and no absolute paths.
- Frozen `AnswerState`/outputs; `generation_attempt_index` is the only counter;
  no retrieval/Web/client dependencies in State; unexpected programming errors
  propagate.

## 4. Security model

- All Web content is `UNTRUSTED_EXTERNAL_CONTENT`; it is placed in the user-data
  JSON, never system instructions.
- SSRF defenses cover credentials, non-http(s), unsupported ports, loopback,
  private/link-local/reserved/multicast/unspecified IPv4/IPv6, cloud metadata,
  IDNA/trailing dot/single-label hosts, mixed public/private DNS answers, DNS
  pinning, redirect revalidation, loops, oversized/decompression responses,
  unsupported content types, and bounded timeouts.
- Provider/search exceptions are normalized; only explicit Web degradation
  statuses are used, never catch-all EMPTY.
- API keys are SecretStr/environment-only; no keys, raw HTML, vendor payloads,
  or exceptions enter Graph State.

## 5. Verification

| Gate | Result |
| --- | --- |
| Baseline at `528fcf6` | 3144 passed, 101 deselected |
| `make verify` | **PASS** — 3193 passed, 103 deselected |
| `make test-stage7` | **PASS** — 49 focused tests |
| `make test-stage7-integration` | **PASS** — real local HTTP SearXNG JSON + DeepSeek structured transport |
| `make stage7-verify` | **PASS** — 3193 offline + 49 focused + 1 local transport integration |
| `make stage3-parser-smoke` | **PASS** — real MinerU 4.0.11 |
| `make stage2-verify` | **PASS** — 28 real integration tests |
| `make stage3-verify` | **PASS** — 529 focused + 18 real PostgreSQL/Docling |
| `make stage4-verify` | **PASS** — 654 focused + 36 real PostgreSQL/Milvus |
| `make stage5-verify` | **PASS** — 75 focused + 8 real PostgreSQL/Milvus |
| `make stage6-verify` | **PASS** — 24 focused + 8 real PostgreSQL/Milvus |
| `make stage7-provider-smoke` | **SKIPPED** — no `ATLASRAG_SEARXNG_URL`/`DEEPSEEK_API_KEY`; real provider acceptance not claimed |
| `make architecture-check` / `git diff --check` | PASS |

Real model/Stage 4–6 smokes are rerun in closeout; no fake result is presented
as live provider acceptance.

## 6. Test coverage

Web: success/zero/unavailable/malformed search; ordering/domain binding;
SSRF loopback/private/metadata/credentials/ports/schemes/IPv6; mixed DNS;
redirect revalidation/pinning; content-type/oversize; sanitizer hidden/script/
nav; snippet fallback; security drop; budgets/order; summary fallback; statuses.

Answer: local-only/mixed/INSUFFICIENT_BEST_MATCH; whitespace evidence; identity
compression/fallback; structured generation; malformed protocol; unknown/
mismatched citations; forged URL; control leakage; legitimate prompt-injection
discussion; regeneration success/exhaustion; BLOCK safe failure; citation order/
only-used; duplicate basename/no absolute paths; programming error propagation.

## 7. Explicit quality boundaries

- **Web summarization strategy is benchmark-selected**, not a final quality
  winner. Direct sanitized/snippet evidence is the default candidate.
- **Compression is benchmark-selected**, not a final quality winner; identity
  is the Stage 7 baseline.
- **Output Review is protocol/security validation, not proof of factual
  correctness.** It cannot establish that natural-language claims are true.
- The SearXNG adapter is native HTTP JSON, not MCP, by the approved amendment.
- Live SearXNG/DeepSeek acceptance remains unverified without external
  credentials/network; local transport tests are not claimed as live acceptance.

## 8. Deferred work

Query Construction, RagCore orchestration/retry/routing, build_answer_input,
Session Query Cache, RuntimeGate/MaintenanceCoordinator, CLI/Web UI, Stage 8,
and the Stage 9 benchmark harness/comparison are not implemented.

## 9. Task ledger

| Task | Status |
| --- | --- |
| T0 baseline, frozen audit, design, amendment | COMPLETE |
| T1 SearXNG HTTP JSON provider | COMPLETE |
| T2 SSRF-safe fetch | COMPLETE |
| T3 HTML sanitize/budgets/snippet | COMPLETE |
| T4 Web strategies/result | COMPLETE |
| T5 web_evidence_subgraph | COMPLETE |
| T6 DeepSeek structured provider | COMPLETE (transport tested; live pending) |
| T7 compression/generation input | COMPLETE |
| T8 deterministic output review | COMPLETE |
| T9 citation resolver/FinalResponse | COMPLETE |
| T10 answer_subgraph/regeneration | COMPLETE |
| T11 integration/review/closeout/PR | IN PROGRESS |

## 10. Independent audit

A read-only audit initially found two security blockers and several gaps. All
were corrected and retested:

- **Reserved IPv4-compatible IPv6 (`::/96`) accepted:** `_is_public_address`
  now rejects `is_reserved` and the entire `::/96` space, in addition to
  mapped/6to4/Teredo/NAT64 alternatives. Tests cover `::127.0.0.1`.
- **No total fetch deadline:** a per-fetch watchdog closes active connections at
  the explicit total timeout; `fetch_timeout_seconds` is wired from the profile
  and a slow-response test proves abort.
- **Malformed SearXNG response:** now propagates as a technical provider
  failure instead of being rewritten as business `UNAVAILABLE`.
- **Source-boundary review:** internal document/revision/chunk/element IDs in
  prose produce `SOURCE_BOUNDARY_VIOLATION` and BLOCK; code fences and
  legitimate technical discussion are not blocked by crude phrase rules.
- **Summary strategy completeness:** a production `DeepSeekSummaryProvider`
  (one batch, JSON, bounded summaries) was added; summary failure falls back to
  direct evidence.
- **Snippet safety:** search snippets are sanitized and control-stripped before
  becoming `SEARCH_SNIPPET` evidence.
- **URL review normalization:** scheme/host case, default ports, path
  normalization and code-fence exclusion avoid false positives.
- **Coverage:** state-isolation, programming-error propagation, web-only
  AnswerInput, retry-count, deadline, source-boundary and snippet tests added.
- **Final re-audit fixes:** malformed `http.client.HTTPException` responses are
  normalized to a per-source fetch failure (snippet fallback/DEGRADED, never a
  whole-stage crash); total-budget clipping is DEGRADED; URL comparison strips
  only the scheme's own default port, rejects userinfo and does not decode
  reserved path delimiters; source-boundary IDs are checked only in prose
  (fenced code exempt); snippet titles are sanitized; summary `max_tokens`/
  retry/malformed tests added; connection timeout follows the per-fetch limit.

**Architecture verdict: READY.** **Security/release verdict: READY after these
fixes.** The only unresolved acceptance item is external live provider
credentials/network; it is reported as unverified, not as a fake pass.

## 11. Publication

_To be completed after commits, normal push, PR creation, and final-HEAD
remote CI/Integration checks. Stage 7 must remain unmerged; Stage 8 must not
start._
