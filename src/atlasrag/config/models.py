"""Frozen, explicit runtime configuration contracts."""

from typing import Annotated
from urllib.parse import urlsplit

from pydantic import (
    AnyHttpUrl,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
)

from atlasrag.domain.base import FrozenModel, StrictReal

_HTTP_URL_ADAPTER: TypeAdapter[AnyHttpUrl] = TypeAdapter(AnyHttpUrl)
PositiveStrictInt = Annotated[StrictInt, Field(gt=0)]
Port = Annotated[StrictInt, Field(ge=1, le=65535)]
PositiveReal = Annotated[StrictReal, Field(gt=0)]
SampleRate = Annotated[StrictReal, Field(ge=0, le=1)]


def _validate_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _validate_http_url_without_userinfo(value: str) -> str:
    _validate_nonblank(value)
    _HTTP_URL_ADAPTER.validate_python(value, strict=True)
    if "@" in urlsplit(value).netloc:
        raise ValueError("URL userinfo is not permitted")
    return value


class InfrastructureConfig(FrozenModel):
    """Deployment coordinates excluded from answer-semantics fingerprints."""

    knowledge_directory: StrictStr
    postgres_host: StrictStr
    postgres_port: Port
    redis_host: StrictStr
    redis_port: Port
    milvus_host: StrictStr
    milvus_port: Port
    searxng_url: StrictStr

    _nonblank_values = field_validator(
        "knowledge_directory", "postgres_host", "redis_host", "milvus_host"
    )(_validate_nonblank)
    _valid_searxng_url = field_validator("searxng_url")(
        _validate_http_url_without_userinfo
    )


class ModelConfig(FrozenModel):
    """Required model identities and immutable revisions."""

    embedding_model_id: StrictStr
    embedding_model_revision: StrictStr
    reranker_model_id: StrictStr
    reranker_model_revision: StrictStr
    query_model_id: StrictStr
    query_model_revision: StrictStr
    generation_model_id: StrictStr
    generation_model_revision: StrictStr

    _nonblank_values = field_validator("*")(_validate_nonblank)


class PartIConfig(FrozenModel):
    """Knowledge-build semantics that determine index compatibility."""

    loader_behavior_version: StrictStr
    parser_behavior_version: StrictStr
    chunker_behavior_version: StrictStr
    representation_version: StrictStr
    embedding_dimension: PositiveStrictInt
    bm25_analyzer: StrictStr
    milvus_schema_version: StrictStr
    milvus_index_version: StrictStr

    _nonblank_values = field_validator(
        "loader_behavior_version",
        "parser_behavior_version",
        "chunker_behavior_version",
        "representation_version",
        "bm25_analyzer",
        "milvus_schema_version",
        "milvus_index_version",
    )(_validate_nonblank)


class RetrievalConfig(FrozenModel):
    """Answer-affecting retrieval and evidence-selection semantics."""

    query_construction_version: StrictStr
    retrieval_semantics_version: StrictStr
    sufficiency_strategy: StrictStr
    sufficiency_version: StrictStr
    reranker_strategy: StrictStr

    _nonblank_values = field_validator("*")(_validate_nonblank)


class AnswerConfig(FrozenModel):
    """Answer generation, evidence representation, and output semantics."""

    generation_prompt_version: StrictStr
    web_evidence_strategy: StrictStr
    compression_strategy: StrictStr
    citation_policy_version: StrictStr
    output_policy_version: StrictStr

    _nonblank_values = field_validator("*")(_validate_nonblank)


class CacheConfig(FrozenModel):
    """Operational cache controls excluded from both fingerprints."""

    query_cache_enabled: StrictBool
    parent_cache_enabled: StrictBool
    query_ttl_seconds: PositiveStrictInt
    parent_ttl_seconds: PositiveStrictInt


class MaintenanceConfig(FrozenModel):
    """Operational maintenance and shutdown limits."""

    max_document_concurrency: PositiveStrictInt
    drain_timeout_seconds: PositiveReal
    shutdown_timeout_seconds: PositiveReal


class ObservabilityConfig(FrozenModel):
    """Operational tracing and logging controls."""

    trace_sample_rate: SampleRate
    log_level: StrictStr
    log_directory: StrictStr
    log_queries: StrictBool

    _nonblank_values = field_validator("log_level", "log_directory")(_validate_nonblank)


class RuntimeConfig(FrozenModel):
    """Validated aggregate configuration frozen at runtime startup."""

    infrastructure: InfrastructureConfig
    models: ModelConfig
    part_i: PartIConfig
    retrieval: RetrievalConfig
    answer: AnswerConfig
    cache: CacheConfig
    maintenance: MaintenanceConfig
    observability: ObservabilityConfig
