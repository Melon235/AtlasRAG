"""Deterministic projections for AtlasRAG semantic fingerprints."""

from atlasrag._canonical import canonical_sha256
from atlasrag.config.models import RuntimeConfig


def pipeline_fingerprint(config: RuntimeConfig) -> str:
    """Hash only model and Part I values that determine built knowledge."""
    payload: dict[str, object] = {
        "models": {
            "embedding_model_id": config.models.embedding_model_id,
            "embedding_model_revision": config.models.embedding_model_revision,
            "embedding_tokenizer_id": config.models.embedding_tokenizer_id,
            "embedding_tokenizer_revision": (
                config.models.embedding_tokenizer_revision
            ),
        },
        "part_i": {
            "loader_behavior_version": config.part_i.loader_behavior_version,
            "parser_behavior_version": config.part_i.parser_behavior_version,
            "chunker_behavior_version": config.part_i.chunker_behavior_version,
            "representation_version": config.part_i.representation_version,
            "embedding_dimension": config.part_i.embedding_dimension,
            "bm25_analyzer": config.part_i.bm25_analyzer,
            "milvus_schema_version": config.part_i.milvus_schema_version,
            "milvus_index_version": config.part_i.milvus_index_version,
        },
    }
    return canonical_sha256(payload)


def runtime_fingerprint(config: RuntimeConfig) -> str:
    """Hash only answer-affecting Part II runtime semantics."""
    payload: dict[str, object] = {
        "models": {
            "reranker_model_id": config.models.reranker_model_id,
            "reranker_model_revision": config.models.reranker_model_revision,
            "query_model_id": config.models.query_model_id,
            "query_model_revision": config.models.query_model_revision,
            "generation_model_id": config.models.generation_model_id,
            "generation_model_revision": config.models.generation_model_revision,
        },
        "retrieval": {
            "query_construction_version": (config.retrieval.query_construction_version),
            "retrieval_semantics_version": (
                config.retrieval.retrieval_semantics_version
            ),
            "sufficiency_strategy": config.retrieval.sufficiency_strategy,
            "sufficiency_version": config.retrieval.sufficiency_version,
            "reranker_strategy": config.retrieval.reranker_strategy,
        },
        "answer": {
            "generation_prompt_version": config.answer.generation_prompt_version,
            "web_evidence_strategy": config.answer.web_evidence_strategy,
            "compression_strategy": config.answer.compression_strategy,
            "citation_policy_version": config.answer.citation_policy_version,
            "output_policy_version": config.answer.output_policy_version,
        },
    }
    return canonical_sha256(payload)
