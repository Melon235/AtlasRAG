from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from atlasrag.config.models import RuntimeConfig
from atlasrag.runtime.composition import SharedResources
from atlasrag.runtime.production import build_default_business


def runtime_config() -> RuntimeConfig:
    return RuntimeConfig.model_validate(
        {
            "infrastructure": {
                "knowledge_directory": "/tmp/atlasrag-production",
                "postgres_host": "postgres",
                "postgres_port": 5432,
                "postgres_database": "atlasrag",
                "postgres_user": "atlasrag",
                "postgres_password": "postgres-secret",
                "redis_host": "redis",
                "redis_port": 6379,
                "milvus_host": "milvus",
                "milvus_port": 19530,
                "searxng_url": "http://searxng:8080",
            },
            "models": {
                "embedding_model_id": "BAAI/bge-m3",
                "embedding_model_revision": "embed-r1",
                "embedding_tokenizer_id": "BAAI/bge-m3",
                "embedding_tokenizer_revision": "tokenizer-r1",
                "reranker_model_id": "BAAI/bge-reranker-v2-m3",
                "reranker_model_revision": "rerank-r1",
                "query_model_id": "deepseek-query",
                "query_model_revision": "query-r1",
                "generation_model_id": "deepseek-generation",
                "generation_model_revision": "generation-r1",
                "deepseek_api_key": "deepseek-secret",
                "model_device": "cuda",
            },
            "part_i": {
                "loader_behavior_version": "loader-v1",
                "parser_behavior_version": "parser-v1",
                "chunker_behavior_version": "chunker-v1",
                "representation_version": "representation-v1",
                "embedding_dimension": 1024,
                "bm25_analyzer": "standard-v1",
                "milvus_schema_version": "schema-v1",
                "milvus_index_version": "index-v1",
            },
            "retrieval": {
                "query_construction_version": "query-construction-v1",
                "retrieval_semantics_version": "retrieval-v1",
                "sufficiency_strategy": "evidence-coverage",
                "sufficiency_version": "sufficiency-v1",
                "reranker_strategy": "cross-encoder",
            },
            "answer": {
                "generation_prompt_version": "generation-v1",
                "web_evidence_strategy": "sanitized-content",
                "compression_strategy": "lossless-v1",
                "citation_policy_version": "citation-v1",
                "output_policy_version": "output-v1",
            },
            "cache": {
                "query_cache_enabled": True,
                "parent_cache_enabled": True,
                "query_ttl_seconds": 600,
                "parent_ttl_seconds": 3600,
            },
            "maintenance": {
                "max_document_concurrency": 2,
                "drain_timeout_seconds": 30.0,
                "shutdown_timeout_seconds": 10.0,
            },
            "observability": {
                "trace_sample_rate": 0.25,
                "log_level": "INFO",
                "log_directory": "/var/log/atlasrag",
                "log_queries": False,
            },
            "runtime": {"postgres_pool_max_size": 16},
        }
    )


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
