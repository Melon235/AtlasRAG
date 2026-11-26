from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import redis.asyncio as redis
from support import runtime_config

from atlasrag.domain.indexing.models import EmbeddingModelIdentity
from atlasrag.providers.embedding.qwen import (
    QWEN_DIMENSION,
    QWEN_MODEL_ID,
    QWEN_REVISION,
)
from atlasrag.providers.reranker.base import RerankerModelIdentity
from atlasrag.providers.reranker.bge import (
    BGE_RERANKER_MODEL_ID,
    BGE_RERANKER_REVISION,
)
from atlasrag.runtime import composition as composition_module
from atlasrag.runtime import production as production_module


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


class _Pool:
    count = 0

    def __init__(self, *args: object, **kwargs: object) -> None:
        type(self).count += 1

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def health(self) -> None:
        return None


class _Milvus:
    count = 0

    def __init__(self, *args: object, **kwargs: object) -> None:
        type(self).count += 1
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


class _Redis:
    count = 0

    def __init__(self, *args: object, **kwargs: object) -> None:
        type(self).count += 1
        self.values: dict[str, str] = {}

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.values[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.values:
                self.values.pop(key)
                removed += 1
        return removed

    async def scan(
        self, cursor: int = 0, match: str | None = None, count: int | None = None
    ) -> tuple[int, list[bytes]]:
        return 0, []

    async def unlink(self, *keys: str) -> int:
        return await self.delete(*keys)

    async def eval(self, script: str, numkeys: int, *args: object) -> object:
        if numkeys == 1 and len(args) == 2:
            key = str(args[0])
            candidate = str(args[1])
            return self.values.setdefault(key, candidate)
        if numkeys == 2 and len(args) == 2:
            return [self.values.get(str(args[1])), self.values.get(str(args[0]))]
        return 1

    async def aclose(self) -> None:
        return None


class _Embedding:
    count = 0

    def __init__(self, *args: object, **kwargs: object) -> None:
        type(self).count += 1
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


class _Reranker:
    count = 0

    def __init__(self, *args: object, **kwargs: object) -> None:
        type(self).count += 1
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


class _HTTP:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def aclose(self) -> None:
        return None


class _Ownership:
    def __init__(self) -> None:
        self.acquired = 0
        self.released = 0

    def acquire(self) -> None:
        self.acquired += 1

    def release(self) -> None:
        self.released += 1


@pytest.mark.asyncio
async def test_default_production_startup_and_shutdown_with_fake_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composition_module, "PostgresPool", _Pool)
    monkeypatch.setattr(composition_module, "MilvusClient", lambda **kwargs: object())
    monkeypatch.setattr(composition_module, "MilvusIndexProvider", _Milvus)
    monkeypatch.setattr(composition_module, "QwenEmbeddingProvider", _Embedding)
    monkeypatch.setattr(composition_module, "BgeRerankerProvider", _Reranker)
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: _Redis())
    monkeypatch.setattr(httpx, "AsyncClient", _HTTP)
    ownership = _Ownership()
    monkeypatch.setattr(composition_module, "RuntimeOwnershipLock", lambda: ownership)
    monkeypatch.setattr(production_module, "PostgresUnitOfWork", lambda pool: _Unit())
    monkeypatch.setattr(composition_module, "PostgresUnitOfWork", lambda pool: _Unit())

    Path("/tmp/atlasrag-production").mkdir(parents=True, exist_ok=True)
    config = runtime_config()
    runtime = await composition_module.ApplicationComposition(config).start()
    assert runtime.gate.mode.value == "SERVING"
    assert ownership.acquired == 1
    assert _Pool.count == 1
    assert _Milvus.count == 1
    assert _Redis.count == 1
    assert _Embedding.count == 1
    assert _Reranker.count == 1
    await runtime.shutdown()
    assert ownership.released == 1


@pytest.mark.asyncio
async def test_bootstrap_failure_rolls_back_and_releases_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingMilvus(_Milvus):
        async def initialize(self) -> None:
            raise RuntimeError("milvus bootstrap failed")

    monkeypatch.setattr(composition_module, "PostgresPool", _Pool)
    monkeypatch.setattr(composition_module, "MilvusClient", lambda **kwargs: object())
    monkeypatch.setattr(composition_module, "MilvusIndexProvider", FailingMilvus)
    monkeypatch.setattr(composition_module, "QwenEmbeddingProvider", _Embedding)
    monkeypatch.setattr(composition_module, "BgeRerankerProvider", _Reranker)
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: _Redis())
    monkeypatch.setattr(httpx, "AsyncClient", _HTTP)
    ownership = _Ownership()
    monkeypatch.setattr(composition_module, "RuntimeOwnershipLock", lambda: ownership)
    monkeypatch.setattr(production_module, "PostgresUnitOfWork", lambda pool: _Unit())
    monkeypatch.setattr(composition_module, "PostgresUnitOfWork", lambda pool: _Unit())

    with pytest.raises(RuntimeError):
        await composition_module.ApplicationComposition(runtime_config()).start()
    assert ownership.acquired == 1
    assert ownership.released == 1


@pytest.mark.asyncio
async def test_cpu_config_degrades_reranker_without_cuda_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composition_module, "PostgresPool", _Pool)
    monkeypatch.setattr(composition_module, "MilvusClient", lambda **kwargs: object())
    monkeypatch.setattr(composition_module, "MilvusIndexProvider", _Milvus)
    monkeypatch.setattr(composition_module, "QwenEmbeddingProvider", _Embedding)
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: _Redis())
    monkeypatch.setattr(httpx, "AsyncClient", _HTTP)
    ownership = _Ownership()
    monkeypatch.setattr(composition_module, "RuntimeOwnershipLock", lambda: ownership)
    monkeypatch.setattr(production_module, "PostgresUnitOfWork", lambda pool: _Unit())
    monkeypatch.setattr(composition_module, "PostgresUnitOfWork", lambda pool: _Unit())
    base = runtime_config()
    config = base.model_copy(
        update={
            "models": base.models.model_copy(update={"model_device": "cpu"}),
        }
    )
    Path("/tmp/atlasrag-production").mkdir(parents=True, exist_ok=True)
    runtime = await composition_module.ApplicationComposition(config).start()
    assert runtime.gate.mode.value == "SERVING"
    await runtime.shutdown()
