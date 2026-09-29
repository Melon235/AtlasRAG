"""Pure tests for the frozen Milvus schema and index contract."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping

import pytest
from pymilvus import DataType, FunctionType  # type: ignore[import-untyped]

from atlasrag.domain.errors import InvariantViolationError
from atlasrag.providers.index.schema import (
    BM25_FUNCTION_NAME,
    COLLECTION_NAME,
    DENSE_INDEX_NAME,
    SPARSE_INDEX_NAME,
    build_collection_schema,
    build_index_params,
    collection_schema_mismatches,
    index_schema_mismatches,
    normalize_collection_description,
    normalize_index_descriptions,
)


def expected_collection_description(
    embedding_dimension: int = 3,
    *,
    analyzer: str = "standard",
) -> dict[str, object]:
    description = build_collection_schema(
        embedding_dimension,
        analyzer=analyzer,
    ).to_dict()
    return {"collection_name": COLLECTION_NAME, **description}


def expected_index_descriptions() -> list[dict[str, object]]:
    return [parameter.to_dict() for parameter in build_index_params()]


def realistic_collection_description() -> dict[str, object]:
    description = copy.deepcopy(expected_collection_description())
    fields = description["fields"]
    functions = description["functions"]
    assert isinstance(fields, list)
    assert isinstance(functions, list)
    fields.reverse()
    functions.reverse()
    description.update(
        {
            "aliases": ["ignored-alias"],
            "collection_id": 991,
            "num_shards": 2,
            "num_partitions": 1,
            "consistency_level": 2,
            "consistency_level_name": "Bounded",
            "properties": {},
            "created_timestamp": 123,
            "update_timestamp": 456,
        }
    )
    for index, field in enumerate(fields):
        assert isinstance(field, dict)
        field["field_id"] = index + 100
        field["description"] = "server-side descriptive noise"
        data_type = field["type"]
        assert isinstance(data_type, DataType)
        field["type"] = data_type.value
        params = field.get("params")
        if isinstance(params, dict):
            if "dim" in params:
                params["dim"] = str(params["dim"])
            if "max_length" in params:
                params["max_length"] = str(params["max_length"])
            if "enable_analyzer" in params:
                params["enable_analyzer"] = "true"
    for function in functions:
        assert isinstance(function, dict)
        function["id"] = 88
        function["description"] = "server-side descriptive noise"
        function["input_field_ids"] = [108]
        function["output_field_ids"] = [109]
        function_type = function["type"]
        assert isinstance(function_type, FunctionType)
        function["type"] = function_type.value
    return description


def realistic_index_descriptions() -> list[dict[str, object]]:
    return [
        {
            "field_name": "sparse_vector",
            "index_name": SPARSE_INDEX_NAME,
            "index_type": "SPARSE_INVERTED_INDEX",
            "metric_type": "BM25",
            "params": json.dumps(
                {
                    "inverted_index_algo": "DAAT_MAXSCORE",
                    "bm25_k1": "1.2",
                    "bm25_b": "0.75",
                }
            ),
            "total_rows": 17,
            "indexed_rows": 17,
            "pending_index_rows": 0,
            "state": "Finished",
        },
        {
            "field_name": "dense_vector",
            "index_name": DENSE_INDEX_NAME,
            "index_type": "HNSW",
            "metric_type": "COSINE",
            "params": {"M": "32", "efConstruction": "200"},
            "total_rows": 17,
            "indexed_rows": 17,
            "pending_index_rows": 0,
            "state": "Finished",
        },
    ]


def test_collection_schema_is_the_exact_frozen_baseline() -> None:
    schema = build_collection_schema(1024)
    schema.verify()
    raw = schema.to_dict()
    fields = {field["name"]: field for field in raw["fields"]}

    assert raw["auto_id"] is False
    assert raw["enable_dynamic_field"] is False
    assert set(fields) == {
        "chunk_id",
        "document_id",
        "revision_id",
        "file_name",
        "parent_id",
        "chunk_type",
        "source_type",
        "content",
        "sparse_text",
        "dense_vector",
        "sparse_vector",
        "structured_metadata",
    }
    assert fields["chunk_id"]["type"] is DataType.VARCHAR
    assert fields["chunk_id"]["is_primary"] is True
    assert fields["chunk_id"]["auto_id"] is False
    assert fields["parent_id"]["nullable"] is True
    assert fields["dense_vector"]["type"] is DataType.FLOAT_VECTOR
    assert fields["dense_vector"]["params"]["dim"] == 1024
    assert fields["sparse_text"]["params"]["enable_analyzer"] is True
    assert json.loads(fields["sparse_text"]["params"]["analyzer_params"]) == {
        "type": "standard"
    }
    assert fields["sparse_vector"]["type"] is DataType.SPARSE_FLOAT_VECTOR
    assert fields["sparse_vector"]["is_function_output"] is True
    assert fields["structured_metadata"]["type"] is DataType.JSON

    assert raw["functions"] == [
        {
            "name": BM25_FUNCTION_NAME,
            "description": "",
            "type": FunctionType.BM25,
            "input_field_names": ["sparse_text"],
            "output_field_names": ["sparse_vector"],
            "params": {},
        }
    ]


def test_collection_schema_accepts_an_explicit_analyzer_contract() -> None:
    fields = {
        field["name"]: field
        for field in build_collection_schema(3, analyzer="english").to_dict()["fields"]
    }

    assert json.loads(fields["sparse_text"]["params"]["analyzer_params"]) == {
        "type": "english"
    }


@pytest.mark.parametrize(
    ("dimension", "analyzer"),
    [
        (0, "standard"),
        (1, "standard"),
        (-1, "standard"),
        (32_769, "standard"),
        (True, "standard"),
        (3, ""),
    ],
)
def test_collection_schema_rejects_invalid_runtime_contract(
    dimension: int,
    analyzer: str,
) -> None:
    with pytest.raises(InvariantViolationError):
        build_collection_schema(dimension, analyzer=analyzer)


def test_index_params_are_the_exact_frozen_baseline() -> None:
    indexes = {
        parameter.index_name: parameter.to_dict() for parameter in build_index_params()
    }

    assert indexes == {
        DENSE_INDEX_NAME: {
            "field_name": "dense_vector",
            "index_type": "HNSW",
            "index_name": DENSE_INDEX_NAME,
            "M": 32,
            "efConstruction": 200,
            "metric_type": "COSINE",
        },
        SPARSE_INDEX_NAME: {
            "field_name": "sparse_vector",
            "index_type": "SPARSE_INVERTED_INDEX",
            "index_name": SPARSE_INDEX_NAME,
            "inverted_index_algo": "DAAT_MAXSCORE",
            "bm25_k1": 1.2,
            "bm25_b": 0.75,
            "metric_type": "BM25",
        },
    }


def test_collection_normalization_ignores_only_order_and_response_noise() -> None:
    actual = realistic_collection_description()
    before = copy.deepcopy(actual)

    normalized = normalize_collection_description(actual)

    assert actual == before
    assert normalized == normalize_collection_description(
        expected_collection_description()
    )
    assert collection_schema_mismatches(actual, embedding_dimension=3) == ()


def test_collection_comparison_reports_every_incompatibility() -> None:
    actual = realistic_collection_description()
    fields = actual["fields"]
    functions = actual["functions"]
    assert isinstance(fields, list)
    assert isinstance(functions, list)
    actual["collection_name"] = "wrong_collection"
    fields_by_name = {
        field["name"]: field for field in fields if isinstance(field, dict)
    }
    dense_params = fields_by_name["dense_vector"]["params"]
    sparse_params = fields_by_name["sparse_text"]["params"]
    assert isinstance(dense_params, dict)
    assert isinstance(sparse_params, dict)
    dense_params["dim"] = "4"
    sparse_params["analyzer_params"] = '{"type":"english"}'
    fields_by_name["parent_id"]["nullable"] = False
    fields.append(
        {
            "name": "unexpected",
            "type": DataType.VARCHAR.value,
            "params": {"max_length": 8},
        }
    )
    assert isinstance(functions[0], dict)
    functions[0]["type"] = FunctionType.UNKNOWN.value

    mismatches = collection_schema_mismatches(actual, embedding_dimension=3)
    message = "\n".join(mismatches)

    assert len(mismatches) >= 6
    assert "collection_name" in message
    assert "dense_vector" in message and "dim" in message
    assert "sparse_text" in message and "analyzer_params" in message
    assert "parent_id" in message and "nullable" in message
    assert "unexpected" in message
    assert BM25_FUNCTION_NAME in message and "type" in message


def test_collection_comparison_fails_closed_for_malformed_descriptions() -> None:
    assert collection_schema_mismatches(None, embedding_dimension=3)
    assert collection_schema_mismatches([], embedding_dimension=3)
    malformed = expected_collection_description()
    malformed["fields"] = "not-a-field-list"
    mismatches = collection_schema_mismatches(malformed, embedding_dimension=3)
    assert mismatches
    assert "fields" in "\n".join(mismatches)


def test_collection_comparison_rejects_semantic_collection_properties() -> None:
    actual = realistic_collection_description()
    actual["properties"] = {"collection.ttl.seconds": "1"}

    mismatches = collection_schema_mismatches(actual, embedding_dimension=3)

    assert mismatches
    message = "\n".join(mismatches)
    assert "properties" in message
    assert "collection.ttl.seconds" in message


def test_index_normalization_accepts_real_describe_shapes_and_noise() -> None:
    actual = realistic_index_descriptions()
    before = copy.deepcopy(actual)

    normalized = normalize_index_descriptions(actual)

    assert actual == before
    assert normalized == normalize_index_descriptions(expected_index_descriptions())
    assert index_schema_mismatches(actual) == ()


def test_index_comparison_reports_every_incompatibility() -> None:
    actual = realistic_index_descriptions()
    dense = actual[1]
    sparse = actual[0]
    dense["index_type"] = "IVF_FLAT"
    dense_params = dense["params"]
    sparse_params = sparse["params"]
    assert isinstance(dense_params, dict)
    assert isinstance(sparse_params, str)
    dense_params["M"] = "16"
    sparse["params"] = {
        "inverted_index_algo": "DAAT_WAND",
        "bm25_k1": "2.0",
        "bm25_b": "0.5",
    }
    actual.append(
        {
            "field_name": "content",
            "index_name": "unexpected_index",
            "index_type": "INVERTED",
        }
    )

    mismatches = index_schema_mismatches(actual)
    message = "\n".join(mismatches)

    assert len(mismatches) >= 6
    assert DENSE_INDEX_NAME in message and "index_type" in message
    assert DENSE_INDEX_NAME in message and ".M" in message
    assert SPARSE_INDEX_NAME in message and "inverted_index_algo" in message
    assert SPARSE_INDEX_NAME in message and "bm25_k1" in message
    assert SPARSE_INDEX_NAME in message and "bm25_b" in message
    assert "unexpected_index" in message


def test_index_comparison_rejects_conflicting_nested_identity_params() -> None:
    actual = realistic_index_descriptions()
    dense = actual[1]
    dense["params"] = {
        "index_type": "IVF_FLAT",
        "metric_type": "L2",
        "M": "32",
        "efConstruction": "200",
    }

    mismatches = index_schema_mismatches(actual)
    message = "\n".join(mismatches)

    assert mismatches
    assert DENSE_INDEX_NAME in message
    assert "conflict:index_type" in message
    assert "conflict:metric_type" in message


@pytest.mark.parametrize("payload", [None, {}, "indexes", [None]])
def test_index_comparison_fails_closed_for_malformed_descriptions(
    payload: object,
) -> None:
    assert index_schema_mismatches(payload)


def test_normalized_contracts_use_name_maps_not_response_order() -> None:
    collection = normalize_collection_description(expected_collection_description())
    indexes = normalize_index_descriptions(expected_index_descriptions())

    fields = collection["fields"]
    functions = collection["functions"]
    assert isinstance(fields, Mapping)
    assert isinstance(functions, Mapping)
    assert set(fields) == {
        "chunk_id",
        "document_id",
        "revision_id",
        "file_name",
        "parent_id",
        "chunk_type",
        "source_type",
        "content",
        "sparse_text",
        "dense_vector",
        "sparse_vector",
        "structured_metadata",
    }
    assert set(functions) == {BM25_FUNCTION_NAME}
    assert set(indexes) == {DENSE_INDEX_NAME, SPARSE_INDEX_NAME}
