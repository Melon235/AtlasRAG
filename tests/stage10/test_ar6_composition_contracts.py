"""A-R6 production composition retry, degradation, schema and config contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import redis.asyncio as redis
from cache_support import FakeRawRedis
from support import runtime_config

from atlasrag.config.fingerprint import pipeline_fingerprint, runtime_fingerprint
from atlasrag.config.models import RuntimeConfig, RuntimeOperationalConfig
from atlasrag.domain.errors import (
    ConfigurationError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    RuntimeOwnershipConflictError,
)
from atlasrag.domain.indexing.models import EmbeddingModelIdentity
from atlasrag.providers.cache.redis import RedisCacheProvider
from atlasrag.providers.embedding.qwen import (
    QWEN_DIMENSION,
    QWEN_MODEL_ID,
    QWEN_REVISION,
)
from atlasrag.providers.index.schema import COLLECTION_NAME
from atlasrag.providers.reranker.base import RerankerModelIdentity
from atlasrag.providers.reranker.bge import (
    BGE_RERANKER_MODEL_ID,
    BGE_RERANKER_REVISION,
)
from atlasrag.runtime import composition as composition_module
from atlasrag.runtime import production as production_module
from atlasrag.runtime.composition import SharedResources
from atlasrag.runtime.config import load_runtime_config
from atlasrag.runtime.health import DependencyHealth
from atlasrag.runtime.modes import RuntimeMode
from atlasrag.runtime.ownership import RuntimeOwnershipLock
from atlasrag.runtime.resources import ResourceGuard


class _Metadata:
    query_cache_invalidation_required = False

    async def get(self) -> _Metadata:
        return self

    async def set_query_cache_invalidation_required(self, required: bool) -> _Metadata:
        return self


class _Documents:
    async def list(self) -> tuple[object, ...]:
        return ()


class _Unit:
    documents = _Documents()
    runtime_metadata = _Metadata()

    async def __aenter__(self) -> _Unit:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _RedisClient(FakeRawRedis):
    async def aclose(self) -> None:
        return None


class _MilvusStub:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.collection_name = kwargs.get("collection_name")
        self.embedding_dimension = int(kwargs.get("embedding_dimension", 1024))  # type: ignore[call-overload]
        self.analyzer = str(kwargs.get("analyzer", "standard"))

    async def initialize(self) -> None:
        return None

    async def health(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def iter_all_projection(self, **kwargs: object) -> Any:
        if False:
            yield None

    async def iter_revision_projection(self, **kwargs: object) -> Any:
        if False:
            yield None

    async def delete_revision(self, *args: object, **kwargs: object) -> int:
        return 0

    async def wait_revision_visible(self, *args: object, **kwargs: object) -> None:
        return None


class _EmbeddingStub:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self._device = str(kwargs.get("device", "cuda"))

    @property
    def identity(self) -> EmbeddingModelIdentity:
        return EmbeddingModelIdentity(
            model_id=QWEN_MODEL_ID,
            model_revision=QWEN_REVISION,
            tokenizer_id=QWEN_MODEL_ID,
            tokenizer_revision=QWEN_REVISION,
            dimension=QWEN_DIMENSION,
            device=self._device,
        )

    async def initialize(self, *, allow_download: bool = False) -> None:
        return None


class _RerankerStub:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self._device = str(kwargs.get("device", "cuda"))

    @property
    def identity(self) -> RerankerModelIdentity:
        return RerankerModelIdentity(
            model_id=BGE_RERANKER_MODEL_ID,
            model_revision=BGE_RERANKER_REVISION,
            tokenizer_id=BGE_RERANKER_MODEL_ID,
            tokenizer_revision=BGE_RERANKER_REVISION,
            device=self._device,
            dtype="float16",
            max_input_tokens=8192,
        )

    async def initialize(self, *, allow_download: bool = False) -> None:
        return None


class _PoolStub:
    def __init__(self, *args: object, **kwargs: object) -> None:
        return None

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def health(self) -> None:
        return None


class _MinerUStub:
    async def parse(self, source: object, context: object) -> object:
        raise AssertionError


class _OwnershipStub:
    def __init__(self) -> None:
        self.acquired = 0
        self.released = 0

    def acquire(self) -> None:
        self.acquired += 1

    def release(self) -> None:
        self.released += 1


def _install_composition_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pool_cls: type[Any] = _PoolStub,
    milvus_cls: type[Any] = _MilvusStub,
    embedding_cls: type[Any] = _EmbeddingStub,
    reranker_cls: type[Any] = _RerankerStub,
    searxng_status: int = 200,
    searxng_error: BaseException | None = None,
) -> tuple[_OwnershipStub, list[object]]:
    ownership = _OwnershipStub()
    milvus_instances: list[object] = []

    def _make_milvus(*args: object, **kwargs: object) -> object:
        instance = milvus_cls(*args, **kwargs)
        milvus_instances.append(instance)
        return instance

    def _web_transport(request: httpx.Request) -> httpx.Response:
        if searxng_error is not None:
            raise searxng_error
        return httpx.Response(searxng_status)

    orig_async_client = httpx.AsyncClient
    monkeypatch.setattr(composition_module, "PostgresPool", pool_cls)
    monkeypatch.setattr(composition_module, "MilvusClient", lambda **k: object())
    monkeypatch.setattr(composition_module, "MilvusIndexProvider", _make_milvus)
    monkeypatch.setattr(composition_module, "QwenEmbeddingProvider", embedding_cls)
    monkeypatch.setattr(composition_module, "BgeRerankerProvider", reranker_cls)
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: _RedisClient())
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: orig_async_client(
            transport=httpx.MockTransport(_web_transport)
        ),
    )
    monkeypatch.setattr(composition_module, "RuntimeOwnershipLock", lambda: ownership)
    monkeypatch.setattr(production_module, "PostgresUnitOfWork", lambda pool: _Unit())
    monkeypatch.setattr(composition_module, "PostgresUnitOfWork", lambda pool: _Unit())
    return ownership, milvus_instances


def _config_with_dir(tmp_path: Path, **runtime_kwargs: object) -> RuntimeConfig:
    base = runtime_config()
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    return base.model_copy(
        update={
            "infrastructure": base.infrastructure.model_copy(
                update={"knowledge_directory": str(knowledge_dir)}
            ),
            "runtime": RuntimeOperationalConfig(
                postgres_pool_max_size=16,
                startup_retry_delay_seconds=0.001,
                **runtime_kwargs,  # type: ignore[arg-type]
            ),
        }
    )


# AR6-06 — Startup retry, reranker degradation, Milvus wiring and SearXNG probe
@pytest.mark.asyncio
async def test_ar6_06_startup_retries_transient_hard_dependencies_and_cleans_up_on_exhaustion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pg_attempts = 0
    pg_closes = 0

    class FlakyPool:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def open(self) -> None:
            nonlocal pg_attempts
            pg_attempts += 1
            if pg_attempts < 3:
                raise DependencyUnavailableError

        async def close(self) -> None:
            nonlocal pg_closes
            pg_closes += 1

        async def health(self) -> None:
            return None

    milvus_attempts = 0

    class FlakyMilvus(_MilvusStub):
        async def initialize(self) -> None:
            nonlocal milvus_attempts
            milvus_attempts += 1
            if milvus_attempts == 1:
                raise DependencyTimeoutError

    embed_attempts = 0

    class FlakyEmbedding(_EmbeddingStub):
        async def initialize(self, *, allow_download: bool = False) -> None:
            nonlocal embed_attempts
            embed_attempts += 1
            if embed_attempts == 1:
                raise DependencyUnavailableError

    ownership, milvus_instances = _install_composition_fakes(
        monkeypatch,
        pool_cls=FlakyPool,
        milvus_cls=FlakyMilvus,
        embedding_cls=FlakyEmbedding,
        searxng_status=200,
    )
    config = _config_with_dir(tmp_path, startup_attempts=3)
    coordinator = await composition_module.ApplicationComposition(config).start()
    try:
        assert coordinator.gate.mode is RuntimeMode.SERVING
        assert pg_attempts == 3
        assert pg_closes == 2
        assert milvus_attempts == 2
        assert embed_attempts == 2
        assert len(milvus_instances) == 1
        milvus_obj = milvus_instances[0]
        assert getattr(milvus_obj, "collection_name") == COLLECTION_NAME
        assert getattr(milvus_obj, "collection_name") == "atlas_chunks"
        assert getattr(milvus_obj, "analyzer") == "standard"
        snapshot = await coordinator.health()
        assert snapshot.searxng is DependencyHealth.HEALTHY
        assert snapshot.reranker is DependencyHealth.HEALTHY
        assert snapshot.hard_healthy is True
    finally:
        await coordinator.shutdown()
    assert ownership.acquired == 1
    assert ownership.released == 1

    # Exhausting startup_attempts on PG cleans up partial pools & releases ownership.
    pg_fail_opens = 0
    pg_fail_closes = 0

    class AlwaysFailPool:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def open(self) -> None:
            nonlocal pg_fail_opens
            pg_fail_opens += 1
            raise DependencyUnavailableError

        async def close(self) -> None:
            nonlocal pg_fail_closes
            pg_fail_closes += 1

        async def health(self) -> None:
            return None

    ownership_pg, _ = _install_composition_fakes(monkeypatch, pool_cls=AlwaysFailPool)
    with pytest.raises(DependencyUnavailableError):
        await composition_module.ApplicationComposition(
            _config_with_dir(tmp_path, startup_attempts=3)
        ).start()
    assert pg_fail_opens == 3
    assert pg_fail_closes == 3
    assert ownership_pg.acquired == 1
    assert ownership_pg.released == 1

    # Exhausting startup_attempts cleans up & releases ownership.
    always_fail_calls = 0

    class AlwaysFailMilvus(_MilvusStub):
        async def initialize(self) -> None:
            nonlocal always_fail_calls
            always_fail_calls += 1
            raise DependencyUnavailableError

    ownership2, _ = _install_composition_fakes(monkeypatch, milvus_cls=AlwaysFailMilvus)
    with pytest.raises(DependencyUnavailableError):
        await composition_module.ApplicationComposition(
            _config_with_dir(tmp_path, startup_attempts=3)
        ).start()
    assert always_fail_calls == 3
    assert ownership2.acquired == 1
    assert ownership2.released == 1

    # Non-transient error fails immediately on attempt 1 without retry.
    prog_calls = 0

    class ProgrammingFailMilvus(_MilvusStub):
        async def initialize(self) -> None:
            nonlocal prog_calls
            prog_calls += 1
            raise RuntimeError("bug")

    ownership3, _ = _install_composition_fakes(
        monkeypatch, milvus_cls=ProgrammingFailMilvus
    )
    with pytest.raises(RuntimeError, match="bug"):
        await composition_module.ApplicationComposition(
            _config_with_dir(tmp_path, startup_attempts=4)
        ).start()
    assert prog_calls == 1
    assert ownership3.released == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        DependencyUnavailableError(),
        RuntimeError("missing reranker cache"),
        OSError("io error"),
    ],
)
async def test_ar6_06_reranker_failure_and_searxng_failure_soft_degrade_preserving_hard_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: BaseException
) -> None:
    class FailingReranker(_RerankerStub):
        async def initialize(self, *, allow_download: bool = False) -> None:
            raise error

    seen_shared: list[SharedResources] = []

    async def capture_business(shared: SharedResources) -> Any:
        seen_shared.append(shared)
        return await production_module.build_default_business(shared)

    _install_composition_fakes(
        monkeypatch,
        reranker_cls=FailingReranker,
        searxng_status=503,
    )
    coordinator = await composition_module.ApplicationComposition(
        _config_with_dir(tmp_path)
    ).start(build_business=capture_business)
    try:
        assert coordinator.gate.mode is RuntimeMode.SERVING
        assert seen_shared[0].reranker is None
        snapshot = await coordinator.health()
        assert snapshot.reranker is DependencyHealth.UNAVAILABLE
        assert snapshot.searxng is DependencyHealth.UNAVAILABLE
        assert snapshot.hard_healthy is True
        assert await coordinator.readiness() is True
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_ar6_07_production_builder_requires_write_fence_before_query_cache_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from parent_cache_support import (
        TrackedUnitOfWorkFactory,
        build_stored_document,
        build_stored_text_child,
        build_stored_text_parent,
    )
    from stage3.fakes import FakeDatabase

    from atlasrag.domain.enums import RetrievalChunkType
    from atlasrag.domain.requests import UserTurnRequest
    from atlasrag.providers.index.models import MilvusSearchHit
    from atlasrag.services.ingestion.mapping import map_chunk_from_stored

    db = FakeDatabase()
    doc, doc_id, rev_id = build_stored_document(content_seed="ar6-07-r1")
    parent_stored = build_stored_text_parent(
        document_id=doc_id, revision_id=rev_id, content="AR6 Parent"
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

    class _QueryMilvus(_MilvusStub):
        async def search_dense(
            self, query_vector: object, *, filter_expression: str, limit: int
        ) -> tuple[MilvusSearchHit, ...]:
            return (child_hit,)

        async def search_bm25(
            self, query_text: str, *, filter_expression: str, limit: int
        ) -> tuple[MilvusSearchHit, ...]:
            return (child_hit,)

    class _QueryEmbedding(_EmbeddingStub):
        async def embed_query(self, query: str) -> tuple[float, ...]:
            return (1.0,) * QWEN_DIMENSION

    class _QueryReranker(_RerankerStub):
        async def score(
            self, query: str, passages: tuple[str, ...]
        ) -> tuple[float, ...]:
            return tuple(0.95 for _ in passages)

    deepseek_calls = 0

    def _deepseek_handler(request: httpx.Request) -> httpx.Response:
        nonlocal deepseek_calls
        deepseek_calls += 1
        body = json.loads(request.content.decode("utf-8"))
        user_content = body["messages"][1]["content"]
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

    raw = _RedisClient()
    config = _config_with_dir(tmp_path)
    shared = SharedResources(
        config=config,
        ownership=object(),  # type: ignore[arg-type]
        pool=object(),  # type: ignore[arg-type]
        milvus=_QueryMilvus(),  # type: ignore[arg-type]
        redis_provider=RedisCacheProvider(raw),  # type: ignore[arg-type]
        embedding=_QueryEmbedding(),  # type: ignore[arg-type]
        reranker=_QueryReranker(),  # type: ignore[arg-type]
        guard=object(),  # type: ignore[arg-type]
        guarded_mineru=_MinerUStub(),  # type: ignore[arg-type]
        knowledge_directory=tmp_path,
        deepseek_http=httpx.AsyncClient(
            transport=httpx.MockTransport(_deepseek_handler)
        ),
        web_http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(500))
        ),
    )
    bundle = await production_module.build_default_business(shared)
    try:
        # Without an established fence, retrieval succeeds and skips query cache write.
        first = await bundle.retrieval(
            UserTurnRequest(session_id="s-ar6", query="hello")
        )
        assert first.answer_text == "Canonical answer [L1]."
        assert not any(key.startswith("atlasrag:query:") for key in raw.values)
        assert deepseek_calls == 2

        # Once fence is established, retrieval populates query cache and next call hits.
        await shared.redis_provider.establish_query_write_fence()
        second = await bundle.retrieval(
            UserTurnRequest(session_id="s-ar6", query="hello")
        )
        assert second.answer_text == "Canonical answer [L1]."
        assert any(key.startswith("atlasrag:query:") for key in raw.values)
        assert deepseek_calls == 4
        third = await bundle.retrieval(
            UserTurnRequest(session_id="s-ar6", query="hello")
        )
        assert third == second
        assert deepseek_calls == 4
    finally:
        await bundle.close()
        await shared.deepseek_http.aclose()
        await shared.web_http.aclose()


# AR6-07 — Unsupported model / Part I / Retrieval / Answer config rejection
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("section", "changes"),
    [
        ("models", {"embedding_model_id": "BAAI/bge-m3"}),
        ("models", {"embedding_model_revision": "bad-rev"}),
        ("models", {"embedding_tokenizer_id": "other-tokenizer"}),
        ("models", {"embedding_tokenizer_revision": "bad-tok-rev"}),
        ("models", {"reranker_model_id": "other/reranker"}),
        ("models", {"reranker_model_revision": "bad-rerank-rev"}),
        ("models", {"model_device": "mps"}),
        ("part_i", {"loader_behavior_version": "loader-v2"}),
        ("part_i", {"parser_behavior_version": "parser-v2"}),
        ("part_i", {"chunker_behavior_version": "chunker-v1"}),
        ("part_i", {"representation_version": "representation-v2"}),
        ("part_i", {"embedding_dimension": 768}),
        ("part_i", {"bm25_analyzer": "english"}),
        ("part_i", {"milvus_schema_version": "schema-v2"}),
        ("part_i", {"milvus_index_version": "index-v2"}),
        ("retrieval", {"query_construction_version": "query-construction-v2"}),
        ("retrieval", {"retrieval_semantics_version": "retrieval-v2"}),
        ("retrieval", {"sufficiency_strategy": "claim-coverage"}),
        ("retrieval", {"sufficiency_version": "sufficiency-v2"}),
        ("retrieval", {"reranker_strategy": "late-interaction"}),
        ("answer", {"generation_prompt_version": "generation-v2"}),
        ("answer", {"web_evidence_strategy": "unsupported-strategy"}),
        ("answer", {"compression_strategy": "semantic-v2"}),
        ("answer", {"citation_policy_version": "citation-v2"}),
        ("answer", {"output_policy_version": "output-v2"}),
    ],
)
async def test_ar6_07_unsupported_semantic_configs_fail_explicitly_with_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    section: str,
    changes: dict[str, object],
) -> None:
    ownership, _ = _install_composition_fakes(monkeypatch)
    base = _config_with_dir(tmp_path)
    section_value = getattr(base, section).model_copy(update=changes)
    invalid = base.model_copy(update={section: section_value})

    with pytest.raises(ConfigurationError):
        await composition_module.ApplicationComposition(invalid).start()
    assert ownership.acquired == 0

    valid_shared = SharedResources(
        config=invalid,
        ownership=object(),  # type: ignore[arg-type]
        pool=_PoolStub(),  # type: ignore[arg-type]
        milvus=_MilvusStub(),  # type: ignore[arg-type]
        redis_provider=RedisCacheProvider(_RedisClient()),  # type: ignore[arg-type]
        embedding=_EmbeddingStub(),  # type: ignore[arg-type]
        reranker=None,
        guard=object(),  # type: ignore[arg-type]
        guarded_mineru=object(),  # type: ignore[arg-type]
        knowledge_directory=tmp_path,
        deepseek_http=httpx.AsyncClient(),
        web_http=httpx.AsyncClient(),
    )
    try:
        with pytest.raises(ConfigurationError):
            await production_module.build_default_business(valid_shared)
    finally:
        await valid_shared.deepseek_http.aclose()
        await valid_shared.web_http.aclose()


@pytest.mark.asyncio
async def test_ar6_07_shared_resource_identity_and_index_contract_mismatches_raise_configuration_error(
    tmp_path: Path,
) -> None:
    base = _config_with_dir(tmp_path)

    async def _run_with(
        *,
        milvus: object | None = None,
        embedding: object | None = None,
        reranker: object | None = None,
        config: RuntimeConfig = base,
    ) -> None:
        shared = SharedResources(
            config=config,
            ownership=object(),  # type: ignore[arg-type]
            pool=_PoolStub(),  # type: ignore[arg-type]
            milvus=milvus if milvus is not None else _MilvusStub(),  # type: ignore[arg-type]
            redis_provider=RedisCacheProvider(_RedisClient()),  # type: ignore[arg-type]
            embedding=embedding if embedding is not None else _EmbeddingStub(),  # type: ignore[arg-type]
            reranker=reranker,  # type: ignore[arg-type]
            guard=object(),  # type: ignore[arg-type]
            guarded_mineru=object(),  # type: ignore[arg-type]
            knowledge_directory=tmp_path,
            deepseek_http=httpx.AsyncClient(),
            web_http=httpx.AsyncClient(),
        )
        try:
            with pytest.raises(ConfigurationError):
                await production_module.build_default_business(shared)
        finally:
            await shared.deepseek_http.aclose()
            await shared.web_http.aclose()

    class _WrongAnalyzer(_MilvusStub):
        def __init__(self) -> None:
            super().__init__(embedding_dimension=1024, analyzer="english")

    class _WrongDim(_MilvusStub):
        def __init__(self) -> None:
            super().__init__(embedding_dimension=768, analyzer="standard")

    class _WrongEmbed(_EmbeddingStub):
        @property
        def identity(self) -> EmbeddingModelIdentity:
            return EmbeddingModelIdentity(
                model_id="other/model",
                model_revision=QWEN_REVISION,
                tokenizer_id=QWEN_MODEL_ID,
                tokenizer_revision=QWEN_REVISION,
                dimension=QWEN_DIMENSION,
                device="cuda",
            )

    await _run_with(milvus=_WrongAnalyzer())
    await _run_with(milvus=_WrongDim())
    await _run_with(embedding=_WrongEmbed())

    cpu_config = base.model_copy(
        update={"models": base.models.model_copy(update={"model_device": "cpu"})}
    )
    await _run_with(
        config=cpu_config,
        embedding=_EmbeddingStub(device="cpu"),
        reranker=_RerankerStub(device="cuda"),
    )

    # Generic "cuda" config accepts resolved provider device "cuda:0", while explicit
    # "cuda:0" rejects "cuda:1" and "cuda".
    resolved_shared = SharedResources(
        config=base,
        ownership=object(),  # type: ignore[arg-type]
        pool=_PoolStub(),  # type: ignore[arg-type]
        milvus=_MilvusStub(),  # type: ignore[arg-type]
        redis_provider=RedisCacheProvider(_RedisClient()),  # type: ignore[arg-type]
        embedding=_EmbeddingStub(device="cuda:0"),  # type: ignore[arg-type]
        reranker=_RerankerStub(device="cuda:0"),  # type: ignore[arg-type]
        guard=object(),  # type: ignore[arg-type]
        guarded_mineru=object(),  # type: ignore[arg-type]
        knowledge_directory=tmp_path,
        deepseek_http=httpx.AsyncClient(),
        web_http=httpx.AsyncClient(),
    )
    try:
        composition_module.validate_shared_resource_contracts(resolved_shared)
    finally:
        await resolved_shared.deepseek_http.aclose()
        await resolved_shared.web_http.aclose()

    cuda0_config = base.model_copy(
        update={"models": base.models.model_copy(update={"model_device": "cuda:0"})}
    )
    await _run_with(
        config=cuda0_config,
        embedding=_EmbeddingStub(device="cuda:1"),
        reranker=_RerankerStub(device="cuda:0"),
    )
    await _run_with(
        config=cuda0_config,
        embedding=_EmbeddingStub(device="cuda:0"),
        reranker=_RerankerStub(device="cuda:1"),
    )


# AR6-08 — Pool lower bound, ownership, ResourceGuard, redaction and fingerprints
def test_ar6_08_pool_bound_ownership_lock_and_secret_redaction(tmp_path: Path) -> None:
    base = runtime_config()
    valid_min = RuntimeConfig.model_validate(
        {
            **base.model_dump(mode="python"),
            "models": {
                **base.models.model_dump(mode="python"),
                "deepseek_api_key": base.models.deepseek_api_key.get_secret_value(),
            },
            "infrastructure": {
                **base.infrastructure.model_dump(mode="python"),
                "postgres_password": (
                    base.infrastructure.postgres_password.get_secret_value()
                ),
            },
            "maintenance": {
                **base.maintenance.model_dump(mode="python"),
                "max_document_concurrency": 3,
            },
            "runtime": {
                **base.runtime.model_dump(mode="python"),
                "postgres_pool_max_size": 8,  # 2 * 3 + 2
            },
        }
    )
    assert valid_min.runtime.postgres_pool_max_size == 8

    with pytest.raises(Exception):
        RuntimeConfig.model_validate(
            {
                **valid_min.model_dump(mode="python"),
                "models": {
                    **base.models.model_dump(mode="python"),
                    "deepseek_api_key": base.models.deepseek_api_key.get_secret_value(),
                },
                "infrastructure": {
                    **base.infrastructure.model_dump(mode="python"),
                    "postgres_password": (
                        base.infrastructure.postgres_password.get_secret_value()
                    ),
                },
                "runtime": {
                    **valid_min.runtime.model_dump(mode="python"),
                    "postgres_pool_max_size": 7,
                },
            }
        )

    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(mode=0o700)
    lock_file = lock_dir / "runtime.lock"
    first = RuntimeOwnershipLock(lock_file)
    second = RuntimeOwnershipLock(lock_file)
    first.acquire()
    with pytest.raises(RuntimeOwnershipConflictError):
        second.acquire()
    first.release()
    second.acquire()
    second.release()

    # Invalid runtime config never leaks secrets in raised ConfigurationError.
    cfg_file = tmp_path / "cfg.json"
    payload = valid_min.model_dump(mode="python")
    payload["models"]["deepseek_api_key"] = "super-secret-deepseek-token"
    payload["infrastructure"]["postgres_password"] = "super-secret-pg-password"
    payload["runtime"]["postgres_pool_max_size"] = 2
    cfg_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc_info:
        load_runtime_config(config_file=cfg_file, environ={})
    text = f"{exc_info.value!r} {exc_info.value.__cause__!r}"
    assert "super-secret-deepseek-token" not in text
    assert "super-secret-pg-password" not in text


@pytest.mark.asyncio
async def test_ar6_08_resource_guard_bounds_concurrency_and_operational_fields_excluded_from_fingerprints() -> (
    None
):
    guard = ResourceGuard(capacity=1)
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def _first() -> None:
        async with guard.slot():
            first_entered.set()
            await release_first.wait()

    async def _second() -> None:
        async with guard.slot():
            second_entered.set()

    t1 = asyncio.create_task(_first())
    await asyncio.wait_for(first_entered.wait(), timeout=1.0)
    t2 = asyncio.create_task(_second())
    await asyncio.sleep(0)
    assert not second_entered.is_set()
    release_first.set()
    await asyncio.gather(t1, t2)
    assert second_entered.is_set()

    base = runtime_config()
    changed = base.model_copy(
        update={
            "runtime": RuntimeOperationalConfig(
                startup_attempts=7,
                startup_retry_delay_seconds=5.0,
                health_probe_timeout_seconds=9.0,
                health_refresh_interval_seconds=45.0,
                gpu_concurrency=2,
                cache_recovery_interval_seconds=25.0,
                redis_connect_timeout_seconds=8.0,
                redis_socket_timeout_seconds=12.0,
                redis_health_check_interval_seconds=90.0,
                postgres_pool_min_size=3,
                postgres_pool_max_size=24,
            )
        }
    )
    assert pipeline_fingerprint(changed) == pipeline_fingerprint(base)
    assert runtime_fingerprint(changed) == runtime_fingerprint(base)
