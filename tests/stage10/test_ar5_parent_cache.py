"""A-R5 deterministic Parent Context Cache matrix (AR5-01 through AR5-10)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from parent_cache_support import (
    ELEMENT_A,
    ParentFakeRawRedis,
    TrackedUnitOfWorkFactory,
    build_parent_cache_rig,
    build_stored_document,
    build_stored_table,
    build_stored_text_child,
    build_stored_text_parent,
    cached_payload_for,
    ref_for,
)
from stage3.fakes import FakeDatabase
from support import runtime_config

from atlasrag.domain.enums import EvidenceType, RetrievalChunkType, SourceType
from atlasrag.domain.errors import CanonicalDataError
from atlasrag.domain.evidence import LocalEvidence, LocalProvenance, SourceAnchor
from atlasrag.domain.indexing.models import EmbeddingModelIdentity
from atlasrag.domain.ingestion.chunks import (
    TableSlice,
    TableSliceMode,
)
from atlasrag.domain.requests import UserTurnRequest
from atlasrag.providers.cache.keys import parent_cache_key, query_cache_key
from atlasrag.providers.cache.models import CachedParentContext
from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.providers.index.models import MilvusSearchHit
from atlasrag.providers.reranker.base import RerankerModelIdentity
from atlasrag.repositories.postgres.chunks import ChunkRepository
from atlasrag.repositories.postgres.records import StoredChunkType
from atlasrag.runtime import production as production_module
from atlasrag.runtime.composition import SharedResources
from atlasrag.services.ingestion.mapping import map_chunk_from_stored
from atlasrag.services.local_evidence.parent_context import ParentContextService


def _production_shared(
    *,
    raw_redis: ParentFakeRawRedis,
    parent_cache_enabled: bool,
    parent_ttl_seconds: int,
    child_hit: MilvusSearchHit,
) -> tuple[SharedResources, list[str]]:
    base = runtime_config()
    config = base.model_copy(
        update={
            "cache": base.cache.model_copy(
                update={
                    "parent_cache_enabled": parent_cache_enabled,
                    "parent_ttl_seconds": parent_ttl_seconds,
                    "query_cache_enabled": True,
                    "query_ttl_seconds": 120,
                }
            ),
            "models": base.models.model_copy(update={"model_device": "cpu"}),
        }
    )

    class _FakeMilvus:
        embedding_dimension = config.part_i.embedding_dimension

        async def initialize(self) -> None:
            return None

        async def search_dense(
            self,
            query_vector: object,
            *,
            filter_expression: str,
            limit: int,
        ) -> tuple[MilvusSearchHit, ...]:
            return (child_hit,)

        async def search_bm25(
            self,
            query_text: str,
            *,
            filter_expression: str,
            limit: int,
        ) -> tuple[MilvusSearchHit, ...]:
            return (child_hit,)

    class _FakeEmbedding:
        identity = EmbeddingModelIdentity(
            model_id=config.models.embedding_model_id,
            model_revision=config.models.embedding_model_revision,
            tokenizer_id=config.models.embedding_tokenizer_id,
            tokenizer_revision=config.models.embedding_tokenizer_revision,
            dimension=config.part_i.embedding_dimension,
            device="cpu",
        )

        async def embed_query(self, query: str) -> tuple[float, ...]:
            return (1.0,) * config.part_i.embedding_dimension

    class _FakeReranker:
        identity = RerankerModelIdentity(
            model_id="bge",
            model_revision="r1",
            tokenizer_id="bge",
            tokenizer_revision="r1",
            device="cpu",
            dtype="float32",
            max_input_tokens=8192,
        )

        async def score(
            self, query: str, passages: tuple[str, ...]
        ) -> tuple[float, ...]:
            return tuple(0.95 for _ in passages)

    prompts: list[str] = []

    def _deepseek_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        user_content = body["messages"][1]["content"]
        prompts.append(user_content)
        if "UNTRUSTED_EVIDENCE_DATA_JSON" in user_content:
            content = json.dumps(
                {"answer_text": "Canonical answer [L1].", "used_citation_ids": ["L1"]}
            )
        else:
            content = json.dumps({"retrieval_query": "canonical query"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    class _FakeMinerU:
        async def parse(self, source: object, context: object) -> object:
            raise AssertionError

    shared = SharedResources(
        config=config,
        ownership=object(),  # type: ignore[arg-type]
        pool=object(),  # type: ignore[arg-type]
        milvus=_FakeMilvus(),  # type: ignore[arg-type]
        redis_provider=RedisCacheProvider(cast(Any, raw_redis)),
        embedding=_FakeEmbedding(),  # type: ignore[arg-type]
        reranker=_FakeReranker(),  # type: ignore[arg-type]
        guard=object(),  # type: ignore[arg-type]
        guarded_mineru=_FakeMinerU(),  # type: ignore[arg-type]
        knowledge_directory=Path("/tmp/atlasrag-production"),
        deepseek_http=httpx.AsyncClient(
            transport=httpx.MockTransport(_deepseek_handler)
        ),
        web_http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(500))
        ),
    )
    return shared, prompts


# AR5-01 — Production wiring and cache identity
@pytest.mark.asyncio
async def test_ar5_01_production_builder_wires_parent_cache_with_ttl_and_key_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeDatabase()
    doc, doc_id, rev_id = build_stored_document(content_seed="ar5-01-r1")
    parent_stored = build_stored_text_parent(
        document_id=doc_id,
        revision_id=rev_id,
        content="  Production Parent Context  ",
    )
    parent_domain = map_chunk_from_stored(parent_stored)
    child_stored = build_stored_text_child(parent_domain)
    db.documents[doc_id] = doc
    db.chunks[(doc_id, rev_id)] = (parent_stored, child_stored)
    factory = TrackedUnitOfWorkFactory(db)
    monkeypatch.setattr(production_module, "PostgresUnitOfWork", lambda pool: factory())

    child_hit = MilvusSearchHit(
        chunk_id=child_stored.chunk_id,
        document_id=doc_id,
        revision_id=rev_id,
        chunk_type=RetrievalChunkType.TEXT_CHILD,
        parent_id=parent_stored.chunk_id,
        content=child_stored.content,
        score=0.95,
    )

    raw_enabled = ParentFakeRawRedis()
    shared_enabled, prompts_enabled = _production_shared(
        raw_redis=raw_enabled,
        parent_cache_enabled=True,
        parent_ttl_seconds=4321,
        child_hit=child_hit,
    )
    bundle_enabled = await production_module.build_default_business(shared_enabled)
    try:
        r1_a = await bundle_enabled.retrieval(
            UserTurnRequest(session_id="session-a", query="what is parent?")
        )
        assert r1_a.answer_text == "Canonical answer [L1]."
        expected_parent_key = parent_cache_key(rev_id, parent_stored.chunk_id)
        assert expected_parent_key in raw_enabled.values
        assert (expected_parent_key, 4321) in raw_enabled.set_keys
        query_keys_after_a = [
            key for key in raw_enabled.values if key.startswith("atlasrag:query:")
        ]
        assert len(query_keys_after_a) == 1

        # Second session misses Session Query Cache, hits Parent Cache, and reads
        # canonical parent via lightweight header without full content read.
        before_full_reads = len(factory.list_by_ids_calls)
        r1_b = await bundle_enabled.retrieval(
            UserTurnRequest(session_id="session-b", query="what is parent?")
        )
        assert r1_b == r1_a
        assert len(factory.list_by_ids_calls) == before_full_reads
        assert len(factory.batch_header_calls) == 1
        assert "  Production Parent Context  " in prompts_enabled[-1]
        assert any(
            key.startswith("atlasrag:query:session-b:") for key in raw_enabled.values
        )
        # Same revision/context yields identical Parent Cache key; different revision differs.
        assert parent_cache_key(rev_id, parent_stored.chunk_id) == expected_parent_key
        assert (
            parent_cache_key("rev_" + "f" * 64, parent_stored.chunk_id)
            != expected_parent_key
        )
        assert query_cache_key("s", "f", "h").startswith("atlasrag:query:")
    finally:
        await bundle_enabled.close()
        await shared_enabled.deepseek_http.aclose()
        await shared_enabled.web_http.aclose()

    raw_disabled = ParentFakeRawRedis()
    shared_disabled, _ = _production_shared(
        raw_redis=raw_disabled,
        parent_cache_enabled=False,
        parent_ttl_seconds=4321,
        child_hit=child_hit,
    )
    bundle_disabled = await production_module.build_default_business(shared_disabled)
    try:
        await bundle_disabled.retrieval(
            UserTurnRequest(session_id="session-c", query="what is parent?")
        )
        assert not any(
            key.startswith("atlasrag:parent:") for key in raw_disabled.get_keys
        )
        assert not any(
            key.startswith("atlasrag:parent:") for key, _ in raw_disabled.set_keys
        )
        assert any(key.startswith("atlasrag:query:") for key in raw_disabled.values)
    finally:
        await bundle_disabled.close()
        await shared_disabled.deepseek_http.aclose()
        await shared_disabled.web_http.aclose()


# AR5-02 — Valid TEXT_PARENT hit (protocol-only header reader & partial-header fallback)
@pytest.mark.asyncio
async def test_ar5_02_valid_hit_works_with_single_header_reader_and_partial_header_fallback() -> (
    None
):
    rig_single = build_parent_cache_rig(support_batch_headers=False)
    doc, doc_id, rev_id = build_stored_document()
    parent = build_stored_text_parent(
        document_id=doc_id, revision_id=rev_id, content="  canonical text  "
    )
    rig_single.db.documents[doc_id] = doc
    rig_single.db.chunks[(doc_id, rev_id)] = (parent,)
    rig_single.raw.seed(
        {
            parent_cache_key(rev_id, parent.chunk_id): cached_payload_for(
                parent, content="  cached text  "
            ).model_dump_json()
        }
    )

    ref = ref_for(parent, EvidenceType.TEXT_PARENT)
    result = await rig_single.service.expand((ref,))
    assert len(result) == 1
    assert result[0].content == "  cached text  "
    assert result[0].provenance == LocalProvenance(
        file_name=doc.file_name,
        relative_source_path=doc.relative_source_path,
        section_path=parent.section_path,
        source_anchor=None,
    )
    assert rig_single.factory.header_calls == [(doc_id, rev_id, parent.chunk_id)]
    assert rig_single.factory.list_by_ids_calls == []
    assert rig_single.factory.get_by_id_calls == []
    assert rig_single.raw.set_calls == 0

    # When a lightweight header fake omits metadata columns, ParentContextService
    # safely falls back to a full canonical read rather than skipping validation.
    rig_partial = build_parent_cache_rig(omit_header_metadata=True)
    rig_partial.db.documents[doc_id] = doc
    rig_partial.db.chunks[(doc_id, rev_id)] = (parent,)
    rig_partial.raw.seed(
        {
            parent_cache_key(rev_id, parent.chunk_id): cached_payload_for(
                parent, content="  cached text  "
            ).model_dump_json()
        }
    )
    fallback_result = await rig_partial.service.expand((ref,))
    assert fallback_result[0].content == "  canonical text  "
    assert rig_partial.factory.list_by_ids_calls == [(parent.chunk_id,)]
    assert rig_partial.raw.set_calls == 0


# AR5-03 — Miss and canonical fill
@pytest.mark.asyncio
async def test_ar5_03_miss_reads_canonical_fills_exact_payload_ttl_and_hits_next() -> (
    None
):
    rig = build_parent_cache_rig(ttl_seconds=7200)
    doc, doc_id, rev_id = build_stored_document()
    parent = build_stored_text_parent(
        document_id=doc_id,
        revision_id=rev_id,
        content="  Exact canonical parent.\nSecond line\t中文  ",
    )
    rig.db.documents[doc_id] = doc
    rig.db.chunks[(doc_id, rev_id)] = (parent,)
    ref = ref_for(parent, EvidenceType.TEXT_PARENT)
    expected_key = parent_cache_key(rev_id, parent.chunk_id)

    disabled_evidence = await rig.disabled_service.expand((ref,))
    first = await rig.service.expand((ref,))
    assert first == disabled_evidence
    assert expected_key in rig.raw.values
    assert rig.raw.set_keys == [(expected_key, 7200)]

    stored_payload = CachedParentContext.model_validate_json(
        rig.raw.values[expected_key]
    )
    assert stored_payload == CachedParentContext(
        evidence_type=EvidenceType.TEXT_PARENT,
        context_id=parent.chunk_id,
        document_id=doc_id,
        revision_id=rev_id,
        content=parent.content,
        provenance=first[0].provenance,
    )

    rig.factory.list_by_ids_calls.clear()
    second = await rig.service.expand((ref,))
    assert second == first
    assert rig.factory.list_by_ids_calls == []
    assert rig.factory.batch_header_calls == [(parent.chunk_id,)]
    assert rig.raw.set_calls == 1


# AR5-04 — Redis failure and invalid payload
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "get_fault",
    ["unavailable", "timeout", "bad_json", "bad_schema", "blank_content"],
)
async def test_ar5_04_get_faults_and_malformed_payloads_fall_back_to_canonical(
    get_fault: str,
) -> None:
    rig = build_parent_cache_rig()
    doc, doc_id, rev_id = build_stored_document()
    parent = build_stored_text_parent(document_id=doc_id, revision_id=rev_id)
    rig.db.documents[doc_id] = doc
    rig.db.chunks[(doc_id, rev_id)] = (parent,)
    key = parent_cache_key(rev_id, parent.chunk_id)

    if get_fault in {"unavailable", "timeout"}:
        rig.raw.get_modes.append(get_fault)
    elif get_fault == "bad_json":
        rig.raw.seed({key: "{not-json"})
    elif get_fault == "bad_schema":
        rig.raw.seed({key: "{}"})
    else:
        forged = cached_payload_for(parent).model_dump(mode="json")
        forged["content"] = "   "
        rig.raw.seed({key: json.dumps(forged)})

    ref = ref_for(parent, EvidenceType.TEXT_PARENT)
    result = await rig.service.expand((ref,))
    assert result == await rig.disabled_service.expand((ref,))
    assert rig.raw.set_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload_mismatch",
    ["wrong_doc", "wrong_rev", "wrong_ctx", "wrong_type"],
)
async def test_ar5_04_mismatched_cached_identity_or_type_falls_back_to_canonical(
    payload_mismatch: str,
) -> None:
    rig = build_parent_cache_rig()
    doc, doc_id, rev_id = build_stored_document()
    parent = build_stored_text_parent(document_id=doc_id, revision_id=rev_id)
    rig.db.documents[doc_id] = doc
    rig.db.chunks[(doc_id, rev_id)] = (parent,)
    key = parent_cache_key(rev_id, parent.chunk_id)

    payload = cached_payload_for(parent).model_dump(mode="json")
    if payload_mismatch == "wrong_doc":
        payload["document_id"] = "doc_" + "9" * 64
    elif payload_mismatch == "wrong_rev":
        payload["revision_id"] = "rev_" + "9" * 64
    elif payload_mismatch == "wrong_ctx":
        payload["context_id"] = "chunk_" + "9" * 64
    else:
        payload["evidence_type"] = EvidenceType.TABLE.value
    rig.raw.seed({key: json.dumps(payload)})

    ref = ref_for(parent, EvidenceType.TEXT_PARENT)
    result = await rig.service.expand((ref,))
    assert result == await rig.disabled_service.expand((ref,))
    assert rig.raw.set_calls == 1
    assert CachedParentContext.model_validate_json(rig.raw.values[key]).document_id == (
        doc_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "set_mode",
    ["unavailable_before", "apply_timeout", "apply_bad_ack", "reject"],
)
async def test_ar5_04_put_failures_and_ambiguous_ack_still_return_canonical_evidence(
    set_mode: str,
) -> None:
    rig = build_parent_cache_rig()
    doc, doc_id, rev_id = build_stored_document()
    parent = build_stored_text_parent(document_id=doc_id, revision_id=rev_id)
    rig.db.documents[doc_id] = doc
    rig.db.chunks[(doc_id, rev_id)] = (parent,)
    rig.raw.set_mode = set_mode

    ref = ref_for(parent, EvidenceType.TEXT_PARENT)
    result = await rig.service.expand((ref,))
    assert result == await rig.disabled_service.expand((ref,))


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["get", "put"])
async def test_ar5_04_cancellation_during_redis_propagates(phase: str) -> None:
    rig = build_parent_cache_rig()
    doc, doc_id, rev_id = build_stored_document()
    parent = build_stored_text_parent(document_id=doc_id, revision_id=rev_id)
    rig.db.documents[doc_id] = doc
    rig.db.chunks[(doc_id, rev_id)] = (parent,)
    if phase == "get":
        rig.raw.get_mode = "delay"
    else:
        rig.raw.set_mode = "delay"

    task = asyncio.create_task(
        rig.service.expand((ref_for(parent, EvidenceType.TEXT_PARENT),))
    )
    if phase == "get":
        await rig.raw.get_started.wait()
    else:
        await rig.raw.set_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.factory.active_uows == 0


# AR5-05 — Current revision, existence and lineage
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "condition",
    [
        "missing_document",
        "missing_chunk",
        "stale_revision",
        "unpublished_revision",
        "wrong_canonical_document",
        "wrong_canonical_chunk_id",
        "canonical_text_child",
        "canonical_table",
        "invalid_parent_id",
    ],
)
async def test_ar5_05_redis_cannot_resurrect_missing_stale_unpublished_or_wrong_lineage(
    condition: str,
) -> None:
    rig = build_parent_cache_rig()
    doc, doc_id, rev_id = build_stored_document(content_seed="r1")
    parent = build_stored_text_parent(document_id=doc_id, revision_id=rev_id)
    ref = ref_for(parent, EvidenceType.TEXT_PARENT)
    rig.raw.seed(
        {
            parent_cache_key(rev_id, parent.chunk_id): cached_payload_for(
                parent
            ).model_dump_json()
        }
    )

    if condition == "missing_document":
        pass
    elif condition == "missing_chunk":
        rig.db.documents[doc_id] = doc
    elif condition == "stale_revision":
        _, _, rev_2 = build_stored_document(content_seed="r2")
        rig.db.documents[doc_id] = doc.model_copy(update={"current_revision_id": rev_2})
        rig.db.chunks[(doc_id, rev_id)] = (parent,)
    elif condition == "unpublished_revision":
        unpub_doc, _, _ = build_stored_document(published=False)
        rig.db.documents[doc_id] = unpub_doc
        rig.db.chunks[(doc_id, rev_id)] = (parent,)
    elif condition == "wrong_canonical_document":
        other_doc, other_doc_id, other_rev = build_stored_document(
            relative_source_path="docs/other.md",
            file_name="other.md",
            content_seed="other",
        )
        rig.db.documents[doc_id] = doc
        rig.db.documents[other_doc_id] = other_doc
        rig.db.chunks[(other_doc_id, other_rev)] = (
            parent.model_copy(update={"document_id": other_doc_id}),
        )
    elif condition == "wrong_canonical_chunk_id":
        rig.db.documents[doc_id] = doc
        other_parent = build_stored_text_parent(
            document_id=doc_id, revision_id=rev_id, content="different"
        )
        rig.db.chunks[(doc_id, rev_id)] = (other_parent,)
    elif condition == "canonical_text_child":
        rig.db.documents[doc_id] = doc
        child = build_stored_text_child(map_chunk_from_stored(parent))
        rig.db.chunks[(doc_id, rev_id)] = (parent, child)
        ref = ref_for(child, EvidenceType.TEXT_PARENT)
        rig.raw.seed(
            {
                parent_cache_key(rev_id, child.chunk_id): cached_payload_for(
                    child
                ).model_dump_json()
            }
        )
    elif condition == "canonical_table":
        rig.db.documents[doc_id] = doc
        table = build_stored_table(document_id=doc_id, revision_id=rev_id)
        rig.db.chunks[(doc_id, rev_id)] = (table,)
        ref = ref_for(table, EvidenceType.TEXT_PARENT)
        rig.raw.seed(
            {
                parent_cache_key(rev_id, table.chunk_id): cached_payload_for(
                    table
                ).model_dump_json()
            }
        )
    else:
        rig.db.documents[doc_id] = doc
        rig.db.chunks[(doc_id, rev_id)] = (
            parent.model_copy(
                update={
                    "chunk_type": StoredChunkType.TEXT_CHILD,
                    "parent_id": "chunk_" + "a" * 64,
                }
            ),
        )

    with pytest.raises(CanonicalDataError):
        await rig.disabled_service.expand((ref,))
    with pytest.raises(CanonicalDataError):
        await rig.service.expand((ref,))


# AR5-06 — Canonical metadata and provenance equivalence
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        "empty_metadata",
        "bad_metadata_schema_version",
        "bad_content_hash",
        "empty_strategy_metadata",
        "bad_strategy_schema_version",
        "unpinned_default_profile",
        "wrong_retrieval_role",
        "wrong_chunking_strategy",
        "wrong_provenance_type",
        "unexpected_table_slice",
        "unexpected_source_anchor",
        "unexpected_sheet_name",
        "non_single_line_section_path",
        "mismatched_chunk_id_digest",
    ],
)
async def test_ar5_06_cache_hit_matches_full_canonical_metadata_validation(
    corruption: str,
) -> None:
    rig = build_parent_cache_rig()
    doc, doc_id, rev_id = build_stored_document()
    parent = build_stored_text_parent(document_id=doc_id, revision_id=rev_id)
    meta = dict(parent.metadata)
    strategy = dict(parent.strategy_metadata)

    if corruption == "empty_metadata":
        bad = parent.model_copy(update={"metadata": {}})
    elif corruption == "bad_metadata_schema_version":
        bad = parent.model_copy(update={"metadata": {**meta, "schema_version": 2}})
    elif corruption == "bad_content_hash":
        bad = parent.model_copy(update={"metadata": {**meta, "content_hash": "0" * 64}})
    elif corruption == "empty_strategy_metadata":
        bad = parent.model_copy(update={"strategy_metadata": {}})
    elif corruption == "bad_strategy_schema_version":
        bad = parent.model_copy(
            update={"strategy_metadata": {**strategy, "schema_version": 2}}
        )
    elif corruption == "unpinned_default_profile":
        bad = parent.model_copy(
            update={"strategy_metadata": {**strategy, "profile_fingerprint": "0" * 64}}
        )
    elif corruption == "wrong_retrieval_role":
        bad = parent.model_copy(
            update={"metadata": {**meta, "retrieval_role": "RETRIEVABLE"}}
        )
    elif corruption == "wrong_chunking_strategy":
        bad = parent.model_copy(
            update={"strategy_metadata": {**strategy, "chunking_strategy": "RECURSIVE"}}
        )
    elif corruption == "wrong_provenance_type":
        provenance_payload = cast(dict[str, Any], meta["provenance"])
        bad = parent.model_copy(
            update={
                "metadata": {
                    **meta,
                    "provenance": {
                        "source_spans": provenance_payload["source_spans"],
                        "parent_char_start": 0,
                        "parent_char_end": len(parent.content),
                    },
                }
            }
        )
    elif corruption == "unexpected_table_slice":
        table_slice = TableSlice(
            mode=TableSliceMode.FULL,
            source_element_id=ELEMENT_A,
            headers=("A",),
            rows=(("1",),),
            row_start=0,
            row_end=1,
        ).model_dump(mode="json")
        bad = parent.model_copy(
            update={"metadata": {**meta, "table_slice": table_slice}}
        )
    elif corruption == "unexpected_source_anchor":
        bad = parent.model_copy(update={"source_anchor": SourceAnchor(page_number=1)})
    elif corruption == "unexpected_sheet_name":
        bad = parent.model_copy(update={"sheet_name": "Sheet1"})
    elif corruption == "non_single_line_section_path":
        bad = parent.model_copy(update={"section_path": ("Bad\nSection",)})
    else:
        bad = parent.model_copy(update={"chunk_id": "chunk_" + "0" * 64})

    rig.db.documents[doc_id] = doc
    rig.db.chunks[(doc_id, rev_id)] = (bad,)
    rig.raw.seed(
        {
            parent_cache_key(rev_id, bad.chunk_id): CachedParentContext(
                evidence_type=EvidenceType.TEXT_PARENT,
                context_id=bad.chunk_id,
                document_id=doc_id,
                revision_id=rev_id,
                content=bad.content,
                provenance=LocalProvenance(
                    file_name="canonical.md", relative_source_path="canonical.md"
                ),
            ).model_dump_json()
        }
    )

    ref = ref_for(bad, EvidenceType.TEXT_PARENT)
    with pytest.raises(CanonicalDataError):
        await rig.disabled_service.expand((ref,))
    with pytest.raises(CanonicalDataError):
        await rig.service.expand((ref,))


@pytest.mark.asyncio
async def test_ar5_06_authoritative_provenance_overrides_cached_provenance() -> None:
    rig = build_parent_cache_rig()
    doc, doc_id, rev_id = build_stored_document(
        relative_source_path="canonical/manual.md", file_name="manual.md"
    )
    parent = build_stored_text_parent(
        document_id=doc_id,
        revision_id=rev_id,
        section_path=("Canonical", "Heading"),
    )
    rig.db.documents[doc_id] = doc
    rig.db.chunks[(doc_id, rev_id)] = (parent,)
    disagreeing = CachedParentContext(
        evidence_type=EvidenceType.TEXT_PARENT,
        context_id=parent.chunk_id,
        document_id=doc_id,
        revision_id=rev_id,
        content="  cached text  ",
        provenance=LocalProvenance(
            file_name="forged.md",
            relative_source_path="forged/manual.md",
            section_path=("Forged",),
            source_anchor=SourceAnchor(page_number=99),
        ),
    )
    rig.raw.seed(
        {parent_cache_key(rev_id, parent.chunk_id): disagreeing.model_dump_json()}
    )

    result = await rig.service.expand((ref_for(parent, EvidenceType.TEXT_PARENT),))
    assert result[0].provenance == LocalProvenance(
        file_name="manual.md",
        relative_source_path="canonical/manual.md",
        section_path=("Canonical", "Heading"),
        source_anchor=None,
    )


@pytest.mark.asyncio
async def test_ar5_06_repository_header_validation_errors_normalize_to_canonical() -> (
    None
):
    class _Cursor:
        def __init__(self, row: object) -> None:
            self._row = row

        async def fetchone(self) -> object:
            return self._row

    class _Connection:
        def __init__(self, row: object) -> None:
            self._row = row

        async def execute(self, query: str, params: object = None) -> _Cursor:
            return _Cursor(self._row)

    repository = ChunkRepository(cast(Any, _Connection({"chunk_id": "broken"})))
    with pytest.raises(CanonicalDataError):
        await repository.get_canonical_context_header("doc-1", "rev-1", "broken")


# AR5-07 — Content preservation and cacheability
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expect_cached"),
    [
        ("  leading and trailing  ", True),
        ("line1\n\nline2\n", True),
        ("\tindented\tcolumn\t", True),
        ("中文摘要 — résumé — 日本語 — Ελληνικά", True),
        ("   ", False),
        (" \n\t ", False),
    ],
)
async def test_ar5_07_exact_content_preservation_and_whitespace_cacheability(
    content: str,
    expect_cached: bool,
) -> None:
    rig = build_parent_cache_rig()
    doc, doc_id, rev_id = build_stored_document()
    parent = build_stored_text_parent(
        document_id=doc_id, revision_id=rev_id, content=content
    )
    rig.db.documents[doc_id] = doc
    rig.db.chunks[(doc_id, rev_id)] = (parent,)
    ref = ref_for(parent, EvidenceType.TEXT_PARENT)
    key = parent_cache_key(rev_id, parent.chunk_id)

    expected = await rig.disabled_service.expand((ref,))
    first = await rig.service.expand((ref,))
    assert first == expected
    assert first[0].content == content

    if expect_cached:
        assert key in rig.raw.values
        rig.factory.list_by_ids_calls.clear()
        second = await rig.service.expand((ref,))
        assert second == first
        assert second[0].content == content
        assert rig.factory.list_by_ids_calls == []
    else:
        assert key not in rig.raw.values
        assert rig.raw.set_calls == 0


# AR5-08 — Ordering, duplicates and mixed evidence
@pytest.mark.asyncio
async def test_ar5_08_ordering_mixed_hits_misses_multi_doc_and_duplicates() -> None:
    rig = build_parent_cache_rig()
    doc_a, doc_a_id, rev_a = build_stored_document(
        relative_source_path="docs/a.md", file_name="a.md", content_seed="a1"
    )
    doc_b, doc_b_id, rev_b = build_stored_document(
        relative_source_path="docs/b.md", file_name="b.md", content_seed="b1"
    )
    parent_1 = build_stored_text_parent(
        document_id=doc_a_id, revision_id=rev_a, content="Parent 1 (hit)", order=0
    )
    parent_2 = build_stored_text_parent(
        document_id=doc_b_id, revision_id=rev_b, content="Parent 2 (miss)", order=0
    )
    table_1 = build_stored_table(document_id=doc_a_id, revision_id=rev_a, order=1)
    parent_3 = build_stored_text_parent(
        document_id=doc_a_id, revision_id=rev_a, content="Parent 3 (hit)", order=2
    )
    rig.db.documents[doc_a_id] = doc_a
    rig.db.documents[doc_b_id] = doc_b
    rig.db.chunks[(doc_a_id, rev_a)] = (parent_1, table_1, parent_3)
    rig.db.chunks[(doc_b_id, rev_b)] = (parent_2,)

    # Seed parent_1 and parent_3 as cache hits; parent_2 misses; table_1 is canonical.
    rig.raw.seed(
        {
            parent_cache_key(rev_a, parent_1.chunk_id): cached_payload_for(
                parent_1
            ).model_dump_json(),
            parent_cache_key(rev_a, parent_3.chunk_id): cached_payload_for(
                parent_3
            ).model_dump_json(),
        }
    )

    p1_ref = ref_for(parent_1, EvidenceType.TEXT_PARENT)
    p2_ref = ref_for(parent_2, EvidenceType.TEXT_PARENT)
    t1_ref = ref_for(table_1, EvidenceType.TABLE)
    p3_ref = ref_for(parent_3, EvidenceType.TEXT_PARENT)

    sequences = (
        (p1_ref, p2_ref),
        (t1_ref,),
        (p1_ref, t1_ref),
        (t1_ref, p2_ref),
        (p1_ref, t1_ref, p3_ref),
        (p3_ref, t1_ref, p2_ref, p1_ref),
    )
    for seq in sequences:
        assert await rig.service.expand(seq) == await rig.disabled_service.expand(seq)
        # Re-clear cached parent_2 so next sequence still tests mixed hit/miss:
        rig.raw.values.pop(parent_cache_key(rev_b, parent_2.chunk_id), None)

    rig.raw.get_keys.clear()
    rig.raw.set_keys.clear()
    rig.factory.list_by_ids_calls.clear()
    mixed = (p3_ref, t1_ref, p2_ref, p1_ref)
    result = await rig.service.expand(mixed)
    assert tuple(item.context_id for item in result) == tuple(
        ref.context_id for ref in mixed
    )
    assert parent_cache_key(rev_b, parent_2.chunk_id) in rig.raw.values
    assert parent_cache_key(rev_a, table_1.chunk_id) not in rig.raw.values
    assert set(rig.factory.list_by_ids_calls[0]) == {
        t1_ref.context_id,
        p2_ref.context_id,
    }

    # Duplicate references across complete input fail before Redis or DB access.
    rig.raw.get_calls = 0
    rig.raw.set_calls = 0
    before_uows = len(rig.factory.unit_of_works)
    for dup in (
        (p1_ref, p1_ref),
        (t1_ref, t1_ref),
        (p1_ref, t1_ref, ref_for(parent_1, EvidenceType.TABLE)),
    ):
        with pytest.raises(CanonicalDataError):
            await rig.service.expand(dup)
    assert rig.raw.get_calls == 0
    assert rig.raw.set_calls == 0
    assert len(rig.factory.unit_of_works) == before_uows


# AR5-09 — TABLE canonical expansion
@pytest.mark.asyncio
async def test_ar5_09_table_remains_canonical_and_ignores_unrelated_redis_keys() -> (
    None
):
    rig = build_parent_cache_rig()
    doc, doc_id, rev_id = build_stored_document(
        relative_source_path="sheets/data.xlsx",
        file_name="data.xlsx",
        source_type=SourceType.XLSX,
    )
    parent = build_stored_text_parent(document_id=doc_id, revision_id=rev_id, order=0)
    table = build_stored_table(
        document_id=doc_id,
        revision_id=rev_id,
        order=1,
        sheet_name="Q4",
        source_anchor=SourceAnchor(sheet_name="Q4", cell_range="A1:B3"),
    )
    rig.db.documents[doc_id] = doc
    rig.db.chunks[(doc_id, rev_id)] = (parent, table)
    table_key = parent_cache_key(rev_id, table.chunk_id)
    # Seed a forged entry under the table chunk key; service must ignore it completely.
    rig.raw.seed(
        {
            table_key: CachedParentContext(
                evidence_type=EvidenceType.TABLE,
                context_id=table.chunk_id,
                document_id=doc_id,
                revision_id=rev_id,
                content="forged cached table",
                provenance=LocalProvenance(
                    file_name="forged.xlsx", relative_source_path="forged.xlsx"
                ),
            ).model_dump_json()
        }
    )

    t_ref = ref_for(table, EvidenceType.TABLE)
    p_ref = ref_for(parent, EvidenceType.TEXT_PARENT)
    table_only = await rig.service.expand((t_ref,))
    assert len(table_only) == 1
    assert isinstance(table_only[0], LocalEvidence)
    assert table_only[0].evidence_type is EvidenceType.TABLE
    assert table_only[0].content == table.content
    assert table_only[0].provenance == LocalProvenance(
        file_name="data.xlsx",
        relative_source_path="sheets/data.xlsx",
        section_path=("Tables",),
        source_anchor=SourceAnchor(sheet_name="Q4", cell_range="A1:B3"),
    )
    assert rig.raw.get_keys == []
    assert rig.raw.set_keys == []

    mixed = await rig.service.expand((t_ref, p_ref))
    assert mixed[0] == table_only[0]
    assert table_key not in rig.raw.get_keys
    assert all(key != table_key for key, _ in rig.raw.set_keys)

    # Missing or invalid canonical TABLE fails closed.
    rig.db.chunks[(doc_id, rev_id)] = (
        table.model_copy(
            update={
                "metadata": {**dict(table.metadata), "table_slice": None},
            }
        ),
    )
    with pytest.raises(CanonicalDataError):
        await rig.service.expand((t_ref,))

    rig.db.chunks[(doc_id, rev_id)] = (parent,)
    with pytest.raises(CanonicalDataError):
        await rig.service.expand((ref_for(parent, EvidenceType.TABLE),))


# AR5-10 — Disabled Parent Cache
@pytest.mark.asyncio
async def test_ar5_10_disabled_and_no_provider_skip_all_redis_ops() -> None:
    rig = build_parent_cache_rig(cache_enabled=False)
    doc, doc_id, rev_id = build_stored_document()
    parent = build_stored_text_parent(document_id=doc_id, revision_id=rev_id, order=0)
    table = build_stored_table(document_id=doc_id, revision_id=rev_id, order=1)
    rig.db.documents[doc_id] = doc
    rig.db.chunks[(doc_id, rev_id)] = (parent, table)

    key = parent_cache_key(rev_id, parent.chunk_id)
    rig.raw.seed(
        {key: cached_payload_for(parent, content="stale cached").model_dump_json()}
    )

    refs = (
        ref_for(parent, EvidenceType.TEXT_PARENT),
        ref_for(table, EvidenceType.TABLE),
    )
    disabled_result = await rig.service.expand(refs)
    no_provider_disabled = await ParentContextService(
        cast(Any, rig.factory), cache=None, cache_enabled=False
    ).expand(refs)
    no_provider_enabled = await ParentContextService(
        cast(Any, rig.factory), cache=None, cache_enabled=True
    ).expand(refs)

    assert disabled_result == no_provider_disabled == no_provider_enabled
    assert disabled_result[0].content == parent.content
    assert disabled_result[1].content == table.content
    assert rig.raw.get_calls == 0
    assert rig.raw.set_calls == 0
    assert rig.raw.delete_calls == 0
    assert rig.raw.unlink_calls == 0
