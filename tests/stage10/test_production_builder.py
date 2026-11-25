from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from support import runtime_config

from atlasrag.runtime.composition import SharedResources
from atlasrag.runtime.production import build_default_business


class _Pool:
    pass


class _Milvus:
    embedding_dimension = 8

    async def initialize(self) -> None:
        return None

    async def health(self) -> None:
        return None


class _Redis:
    pass


class _Embedding:
    identity = object()

    async def initialize(self, *, allow_download: bool = False) -> None:
        return None


class _MinerU:
    async def parse(self, source: object, context: object) -> object:
        raise AssertionError


@pytest.mark.asyncio
async def test_default_builder_constructs_existing_part_i_and_part_ii_services() -> (
    None
):
    config = runtime_config()
    resources = SharedResources(
        config=config,
        ownership=object(),  # type: ignore[arg-type]
        pool=_Pool(),  # type: ignore[arg-type]
        milvus=_Milvus(),  # type: ignore[arg-type]
        redis_provider=_Redis(),  # type: ignore[arg-type]
        embedding=_Embedding(),  # type: ignore[arg-type]
        reranker=None,
        guard=object(),  # type: ignore[arg-type]
        guarded_mineru=_MinerU(),  # type: ignore[arg-type]
        knowledge_directory=Path("/tmp/atlasrag-production"),
        deepseek_http=httpx.AsyncClient(),
        web_http=httpx.AsyncClient(),
    )
    bundle = await build_default_business(resources)
    assert callable(bundle.retrieval)
    assert callable(bundle.sync)
    await bundle.close()
    await resources.deepseek_http.aclose()
    await resources.web_http.aclose()
