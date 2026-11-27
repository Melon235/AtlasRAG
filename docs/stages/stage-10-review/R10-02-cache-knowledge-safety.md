# R10-02 — Cache Consistency & Knowledge Safety

- Review ID: R10-02
- Reviewed commit: `622434e8de4c65c4b919e34b2c5bfb1481f998aa`
- Independent reviewer: **INTERRUPTED / NO REVIEW EVIDENCE** (subagent stopped without findings)
- Self-review: `SELF_REVIEW`
- Scope: `services/cache/session.py`, `runtime/cache_freshness.py`, `parent_context.py`, `source_safety.py`, cache tests
- Requirements: S10-06a, S10-08, S10-09, S10-09a, S10-10, S10-12, S10-13, S10-14

## Findings

### F-03 — HIGH — Redis write exception escaped cache settlement
`_settle_write` raised the failed write result before `commit` could classify it; a normal Redis failure became a query failure instead of `BYPASSED`.
Status: **FIXED**; settlement suppresses the task result and `commit` classifies it. Regression test added.

### F-04 — HIGH — Parent Cache type comparison
`CanonicalChunkHeader.chunk_type` is `StoredChunkType`; the cached path compared it by identity with domain `ChunkType.TEXT_PARENT`, so every valid hit would fail as `CanonicalDataError`.
Status: **FIXED**; comparison now uses enum values.

### G-04 — HIGH verification gap — interrupted deletion
No isolated PostgreSQL/Milvus test covers before-Milvus, after-Milvus/before-PG, after-PG, required deletion failure, unreadable vs missing, and changed-source cases.
Status: **OPEN**.

### G-05 — HIGH verification gap — ambiguous Redis/PG outcomes
No test covers applied write with lost acknowledgement or ambiguous marker-clear transaction.
Status: **OPEN**.

### G-06 — MEDIUM verification gap — Parent Cache
No test covers valid hit, malformed payload, stale revision, ordering, TABLE fallback or disabled cache.
Status: **OPEN**.

## Review status

Independent review incomplete. Self-review found and fixed two defects; knowledge-safety integration evidence remains missing. **R10-02: PARTIAL / INDEPENDENT REVIEW UNAVAILABLE.**
