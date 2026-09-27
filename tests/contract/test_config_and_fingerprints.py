"""Configuration and deterministic fingerprint contracts."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from atlasrag.config.fingerprint import pipeline_fingerprint, runtime_fingerprint
from atlasrag.config.models import (
    AnswerConfig,
    CacheConfig,
    InfrastructureConfig,
    MaintenanceConfig,
    ModelConfig,
    ObservabilityConfig,
    PartIConfig,
    RetrievalConfig,
    RuntimeConfig,
)


def infrastructure_config(**changes: object) -> InfrastructureConfig:
    values: dict[str, object] = {
        "knowledge_directory": "/srv/atlasrag/knowledge",
        "postgres_host": "postgres",
        "postgres_port": 5432,
        "redis_host": "redis",
        "redis_port": 6379,
        "milvus_host": "milvus",
        "milvus_port": 19530,
        "searxng_url": "http://searxng:8080",
    }
    values.update(changes)
    return InfrastructureConfig.model_validate(values)


def model_config(**changes: object) -> ModelConfig:
    values: dict[str, object] = {
        "embedding_model_id": "BAAI/bge-m3",
        "embedding_model_revision": "embed-r1",
        "reranker_model_id": "BAAI/bge-reranker-v2-m3",
        "reranker_model_revision": "rerank-r1",
        "query_model_id": "deepseek-query",
        "query_model_revision": "query-r1",
        "generation_model_id": "deepseek-generation",
        "generation_model_revision": "generation-r1",
    }
    values.update(changes)
    return ModelConfig.model_validate(values)


def part_i_config(**changes: object) -> PartIConfig:
    values: dict[str, object] = {
        "loader_behavior_version": "loader-v1",
        "parser_behavior_version": "parser-v1",
        "chunker_behavior_version": "chunker-v1",
        "representation_version": "representation-v1",
        "embedding_dimension": 1024,
        "bm25_analyzer": "standard-v1",
        "milvus_schema_version": "schema-v1",
        "milvus_index_version": "index-v1",
    }
    values.update(changes)
    return PartIConfig.model_validate(values)


def retrieval_config(**changes: object) -> RetrievalConfig:
    values: dict[str, object] = {
        "query_construction_version": "query-construction-v1",
        "retrieval_semantics_version": "retrieval-v1",
        "sufficiency_strategy": "evidence-coverage",
        "sufficiency_version": "sufficiency-v1",
        "reranker_strategy": "cross-encoder",
    }
    values.update(changes)
    return RetrievalConfig.model_validate(values)


def answer_config(**changes: object) -> AnswerConfig:
    values: dict[str, object] = {
        "generation_prompt_version": "generation-v1",
        "web_evidence_strategy": "sanitized-content",
        "compression_strategy": "lossless-v1",
        "citation_policy_version": "citation-v1",
        "output_policy_version": "output-v1",
    }
    values.update(changes)
    return AnswerConfig.model_validate(values)


def cache_config(**changes: object) -> CacheConfig:
    values: dict[str, object] = {
        "query_cache_enabled": True,
        "parent_cache_enabled": True,
        "query_ttl_seconds": 600,
        "parent_ttl_seconds": 3600,
    }
    values.update(changes)
    return CacheConfig.model_validate(values)


def maintenance_config(**changes: object) -> MaintenanceConfig:
    values: dict[str, object] = {
        "max_document_concurrency": 2,
        "drain_timeout_seconds": 30.0,
        "shutdown_timeout_seconds": 10.0,
    }
    values.update(changes)
    return MaintenanceConfig.model_validate(values)


def observability_config(**changes: object) -> ObservabilityConfig:
    values: dict[str, object] = {
        "trace_sample_rate": 0.25,
        "log_level": "INFO",
        "log_directory": "/var/log/atlasrag",
        "log_queries": False,
    }
    values.update(changes)
    return ObservabilityConfig.model_validate(values)


def runtime_config(**changes: object) -> RuntimeConfig:
    values: dict[str, object] = {
        "infrastructure": infrastructure_config(),
        "models": model_config(),
        "part_i": part_i_config(),
        "retrieval": retrieval_config(),
        "answer": answer_config(),
        "cache": cache_config(),
        "maintenance": maintenance_config(),
        "observability": observability_config(),
    }
    values.update(changes)
    return RuntimeConfig.model_validate(values)


@pytest.mark.parametrize(
    ("model_type", "expected_fields"),
    [
        (
            InfrastructureConfig,
            {
                "knowledge_directory",
                "postgres_host",
                "postgres_port",
                "redis_host",
                "redis_port",
                "milvus_host",
                "milvus_port",
                "searxng_url",
            },
        ),
        (
            ModelConfig,
            {
                "embedding_model_id",
                "embedding_model_revision",
                "reranker_model_id",
                "reranker_model_revision",
                "query_model_id",
                "query_model_revision",
                "generation_model_id",
                "generation_model_revision",
            },
        ),
        (
            PartIConfig,
            {
                "loader_behavior_version",
                "parser_behavior_version",
                "chunker_behavior_version",
                "representation_version",
                "embedding_dimension",
                "bm25_analyzer",
                "milvus_schema_version",
                "milvus_index_version",
            },
        ),
        (
            RetrievalConfig,
            {
                "query_construction_version",
                "retrieval_semantics_version",
                "sufficiency_strategy",
                "sufficiency_version",
                "reranker_strategy",
            },
        ),
        (
            AnswerConfig,
            {
                "generation_prompt_version",
                "web_evidence_strategy",
                "compression_strategy",
                "citation_policy_version",
                "output_policy_version",
            },
        ),
        (
            CacheConfig,
            {
                "query_cache_enabled",
                "parent_cache_enabled",
                "query_ttl_seconds",
                "parent_ttl_seconds",
            },
        ),
        (
            MaintenanceConfig,
            {
                "max_document_concurrency",
                "drain_timeout_seconds",
                "shutdown_timeout_seconds",
            },
        ),
        (
            ObservabilityConfig,
            {
                "trace_sample_rate",
                "log_level",
                "log_directory",
                "log_queries",
            },
        ),
        (
            RuntimeConfig,
            {
                "infrastructure",
                "models",
                "part_i",
                "retrieval",
                "answer",
                "cache",
                "maintenance",
                "observability",
            },
        ),
    ],
)
def test_config_models_have_exact_fields(
    model_type: type[object], expected_fields: set[str]
) -> None:
    assert set(model_type.model_fields) == expected_fields  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "factory",
    [
        infrastructure_config,
        model_config,
        part_i_config,
        retrieval_config,
        answer_config,
        cache_config,
        maintenance_config,
        observability_config,
        runtime_config,
    ],
)
def test_config_models_are_frozen_and_forbid_extra_fields(
    factory: Callable[..., object],
) -> None:
    config = factory()
    field_name = next(iter(config.__class__.model_fields))  # type: ignore[attr-defined]

    with pytest.raises(ValidationError, match="frozen"):
        setattr(config, field_name, object())

    values = config.model_dump()  # type: ignore[attr-defined]
    values["unexpected"] = "forbidden"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        config.__class__.model_validate(values)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("factory", "field_name"),
    [
        (infrastructure_config, "knowledge_directory"),
        (infrastructure_config, "postgres_host"),
        (infrastructure_config, "redis_host"),
        (infrastructure_config, "milvus_host"),
        (infrastructure_config, "searxng_url"),
        (model_config, "embedding_model_id"),
        (model_config, "embedding_model_revision"),
        (model_config, "reranker_model_id"),
        (model_config, "reranker_model_revision"),
        (model_config, "query_model_id"),
        (model_config, "query_model_revision"),
        (model_config, "generation_model_id"),
        (model_config, "generation_model_revision"),
        (part_i_config, "loader_behavior_version"),
        (part_i_config, "parser_behavior_version"),
        (part_i_config, "chunker_behavior_version"),
        (part_i_config, "representation_version"),
        (part_i_config, "bm25_analyzer"),
        (part_i_config, "milvus_schema_version"),
        (part_i_config, "milvus_index_version"),
        (retrieval_config, "query_construction_version"),
        (retrieval_config, "retrieval_semantics_version"),
        (retrieval_config, "sufficiency_strategy"),
        (retrieval_config, "sufficiency_version"),
        (retrieval_config, "reranker_strategy"),
        (answer_config, "generation_prompt_version"),
        (answer_config, "web_evidence_strategy"),
        (answer_config, "compression_strategy"),
        (answer_config, "citation_policy_version"),
        (answer_config, "output_policy_version"),
        (observability_config, "log_level"),
        (observability_config, "log_directory"),
    ],
)
def test_semantic_text_fields_require_strict_nonblank_strings(
    factory: Callable[..., object], field_name: str
) -> None:
    with pytest.raises(ValidationError):
        factory(**{field_name: " \t"})
    with pytest.raises(ValidationError):
        factory(**{field_name: b"bytes"})
    with pytest.raises(ValidationError):
        factory(**{field_name: 123})


@pytest.mark.parametrize("field_name", ["postgres_port", "redis_port", "milvus_port"])
@pytest.mark.parametrize("invalid_value", [0, 65536, True, "5432"])
def test_infrastructure_ports_are_strict_and_bounded(
    field_name: str, invalid_value: object
) -> None:
    with pytest.raises(ValidationError):
        infrastructure_config(**{field_name: invalid_value})


@pytest.mark.parametrize(
    "invalid_url",
    [
        "ftp://searxng.example.test",
        "searxng.example.test",
        "https://user@searxng.example.test",
        "https://user:password@searxng.example.test",
    ],
)
def test_searxng_url_requires_http_without_userinfo(invalid_url: str) -> None:
    with pytest.raises(ValidationError):
        infrastructure_config(searxng_url=invalid_url)


@pytest.mark.parametrize(
    ("factory", "field_name"),
    [
        (part_i_config, "embedding_dimension"),
        (cache_config, "query_ttl_seconds"),
        (cache_config, "parent_ttl_seconds"),
        (maintenance_config, "max_document_concurrency"),
    ],
)
@pytest.mark.parametrize("invalid_value", [0, -1, True, "1"])
def test_positive_integer_fields_are_strict(
    factory: Callable[..., object], field_name: str, invalid_value: object
) -> None:
    with pytest.raises(ValidationError):
        factory(**{field_name: invalid_value})


@pytest.mark.parametrize("field_name", ["query_cache_enabled", "parent_cache_enabled"])
@pytest.mark.parametrize("invalid_value", [0, 1, "true"])
def test_cache_flags_are_strict_booleans(
    field_name: str, invalid_value: object
) -> None:
    with pytest.raises(ValidationError):
        cache_config(**{field_name: invalid_value})


@pytest.mark.parametrize(
    "field_name", ["drain_timeout_seconds", "shutdown_timeout_seconds"]
)
@pytest.mark.parametrize(
    "invalid_value", [0, -0.1, True, "1", float("nan"), float("inf")]
)
def test_maintenance_timeouts_are_positive_finite_real_numbers(
    field_name: str, invalid_value: object
) -> None:
    with pytest.raises(ValidationError):
        maintenance_config(**{field_name: invalid_value})


@pytest.mark.parametrize("valid_value", [0, 0.0, 1, 1.0])
def test_trace_sample_rate_accepts_inclusive_real_endpoints(
    valid_value: int | float,
) -> None:
    assert observability_config(
        trace_sample_rate=valid_value
    ).trace_sample_rate == float(valid_value)


@pytest.mark.parametrize(
    "invalid_value", [-0.01, 1.01, True, "0.5", float("nan"), float("inf")]
)
def test_trace_sample_rate_rejects_invalid_values(invalid_value: object) -> None:
    with pytest.raises(ValidationError):
        observability_config(trace_sample_rate=invalid_value)


def test_runtime_config_revalidates_constructed_nested_models() -> None:
    invalid_part_i = PartIConfig.model_construct(
        **part_i_config().model_dump(exclude={"embedding_dimension"}),
        embedding_dimension=0,
    )

    with pytest.raises(ValidationError, match="embedding_dimension"):
        runtime_config(part_i=invalid_part_i)


def test_fingerprints_are_repeatable_lowercase_sha256_hex() -> None:
    config = runtime_config()

    first_pipeline = pipeline_fingerprint(config)
    second_pipeline = pipeline_fingerprint(config)
    first_runtime = runtime_fingerprint(config)
    second_runtime = runtime_fingerprint(config)

    assert first_pipeline == second_pipeline
    assert first_runtime == second_runtime
    assert len(first_pipeline) == 64
    assert len(first_runtime) == 64
    assert set(first_pipeline) <= set("0123456789abcdef")
    assert set(first_runtime) <= set("0123456789abcdef")


def _reverse_mappings(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _reverse_mappings(item) for key, item in reversed(tuple(value.items()))
        }
    if isinstance(value, list):
        return [_reverse_mappings(item) for item in value]
    return value


def test_fingerprints_ignore_mapping_and_model_construction_order() -> None:
    original = runtime_config()
    reversed_payload = _reverse_mappings(original.model_dump(mode="python"))
    reconstructed = RuntimeConfig.model_validate(reversed_payload)

    assert reconstructed == original
    assert pipeline_fingerprint(reconstructed) == pipeline_fingerprint(original)
    assert runtime_fingerprint(reconstructed) == runtime_fingerprint(original)


@pytest.mark.parametrize(
    ("section", "field_name", "changed_value"),
    [
        ("models", "embedding_model_id", "BAAI/new-embedding"),
        ("models", "embedding_model_revision", "embed-r2"),
        ("part_i", "loader_behavior_version", "loader-v2"),
        ("part_i", "parser_behavior_version", "parser-v2"),
        ("part_i", "chunker_behavior_version", "chunker-v2"),
        ("part_i", "representation_version", "representation-v2"),
        ("part_i", "embedding_dimension", 768),
        ("part_i", "bm25_analyzer", "language-v2"),
        ("part_i", "milvus_schema_version", "schema-v2"),
        ("part_i", "milvus_index_version", "index-v2"),
    ],
)
def test_pipeline_inputs_change_only_pipeline_fingerprint(
    section: str, field_name: str, changed_value: object
) -> None:
    original = runtime_config()
    changed_section = (
        model_config(**{field_name: changed_value})
        if section == "models"
        else part_i_config(**{field_name: changed_value})
    )
    changed = runtime_config(**{section: changed_section})

    assert pipeline_fingerprint(changed) != pipeline_fingerprint(original)
    assert runtime_fingerprint(changed) == runtime_fingerprint(original)


@pytest.mark.parametrize(
    ("section", "field_name", "changed_value"),
    [
        ("models", "reranker_model_id", "BAAI/new-reranker"),
        ("models", "reranker_model_revision", "rerank-r2"),
        ("models", "query_model_id", "new-query-model"),
        ("models", "query_model_revision", "query-r2"),
        ("models", "generation_model_id", "new-generation-model"),
        ("models", "generation_model_revision", "generation-r2"),
        ("retrieval", "query_construction_version", "query-construction-v2"),
        ("retrieval", "retrieval_semantics_version", "retrieval-v2"),
        ("retrieval", "sufficiency_strategy", "claim-coverage"),
        ("retrieval", "sufficiency_version", "sufficiency-v2"),
        ("retrieval", "reranker_strategy", "late-interaction"),
        ("answer", "generation_prompt_version", "generation-v2"),
        ("answer", "web_evidence_strategy", "summary"),
        ("answer", "compression_strategy", "semantic-v2"),
        ("answer", "citation_policy_version", "citation-v2"),
        ("answer", "output_policy_version", "output-v2"),
    ],
)
def test_runtime_inputs_change_only_runtime_fingerprint(
    section: str, field_name: str, changed_value: object
) -> None:
    original = runtime_config()
    factories: dict[str, Callable[..., object]] = {
        "models": model_config,
        "retrieval": retrieval_config,
        "answer": answer_config,
    }
    changed = runtime_config(
        **{section: factories[section](**{field_name: changed_value})}
    )

    assert pipeline_fingerprint(changed) == pipeline_fingerprint(original)
    assert runtime_fingerprint(changed) != runtime_fingerprint(original)


@pytest.mark.parametrize(
    ("section", "field_name", "changed_value"),
    [
        ("infrastructure", "knowledge_directory", "/different/knowledge"),
        ("infrastructure", "postgres_host", "postgres-2"),
        ("infrastructure", "postgres_port", 15432),
        ("infrastructure", "redis_host", "redis-2"),
        ("infrastructure", "redis_port", 16379),
        ("infrastructure", "milvus_host", "milvus-2"),
        ("infrastructure", "milvus_port", 29530),
        ("infrastructure", "searxng_url", "https://search.example.test"),
        ("cache", "query_cache_enabled", False),
        ("cache", "parent_cache_enabled", False),
        ("cache", "query_ttl_seconds", 1200),
        ("cache", "parent_ttl_seconds", 7200),
        ("maintenance", "max_document_concurrency", 4),
        ("maintenance", "drain_timeout_seconds", 60.0),
        ("maintenance", "shutdown_timeout_seconds", 20.0),
        ("observability", "trace_sample_rate", 0.75),
        ("observability", "log_level", "DEBUG"),
        ("observability", "log_directory", "/tmp/atlasrag-logs"),
        ("observability", "log_queries", True),
    ],
)
def test_operational_inputs_change_neither_fingerprint(
    section: str, field_name: str, changed_value: object
) -> None:
    original = runtime_config()
    factories: dict[str, Callable[..., object]] = {
        "infrastructure": infrastructure_config,
        "cache": cache_config,
        "maintenance": maintenance_config,
        "observability": observability_config,
    }
    changed = runtime_config(
        **{section: factories[section](**{field_name: changed_value})}
    )

    assert pipeline_fingerprint(changed) == pipeline_fingerprint(original)
    assert runtime_fingerprint(changed) == runtime_fingerprint(original)


def test_config_contracts_are_available_from_the_public_package() -> None:
    import atlasrag.config as public_config

    expected_exports = {
        "AnswerConfig": AnswerConfig,
        "CacheConfig": CacheConfig,
        "InfrastructureConfig": InfrastructureConfig,
        "MaintenanceConfig": MaintenanceConfig,
        "ModelConfig": ModelConfig,
        "ObservabilityConfig": ObservabilityConfig,
        "PartIConfig": PartIConfig,
        "RetrievalConfig": RetrievalConfig,
        "RuntimeConfig": RuntimeConfig,
        "pipeline_fingerprint": pipeline_fingerprint,
        "runtime_fingerprint": runtime_fingerprint,
    }

    assert set(public_config.__all__) == set(expected_exports)
    for name, expected in expected_exports.items():
        assert getattr(public_config, name) is expected
