"""Frozen Milvus collection/index builders and fail-closed comparisons."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import IntEnum
from typing import TypeVar

from pymilvus import (  # type: ignore[import-untyped]
    CollectionSchema,
    DataType,
    Function,
    FunctionType,
    MilvusClient,
)
from pymilvus.milvus_client.index import IndexParams  # type: ignore[import-untyped]

from atlasrag._canonical import validate_unicode_scalar_text
from atlasrag.domain.errors import InvariantViolationError
from atlasrag.providers.index.models import (
    MILVUS_CONTENT_MAX_LENGTH,
    MILVUS_DENSE_VECTOR_MAX_DIMENSION,
    MILVUS_DENSE_VECTOR_MIN_DIMENSION,
    MILVUS_ENUM_MAX_LENGTH,
    MILVUS_FILE_NAME_MAX_LENGTH,
    MILVUS_IDENTIFIER_MAX_LENGTH,
)

COLLECTION_NAME = "atlas_chunks"
BM25_FUNCTION_NAME = "atlas_bm25"
DENSE_INDEX_NAME = "dense_hnsw"
SPARSE_INDEX_NAME = "sparse_bm25"

_COLLECTION_SEMANTIC_KEYS = frozenset(
    {
        "collection_name",
        "auto_id",
        "enable_dynamic_field",
        "enable_namespace",
        "fields",
        "functions",
        "properties",
    }
)
_COLLECTION_NOISE_KEYS = frozenset(
    {
        "description",
        "aliases",
        "collection_id",
        "num_shards",
        "num_partitions",
        "consistency_level",
        "consistency_level_name",
        "created_timestamp",
        "update_timestamp",
        "struct_array_fields",
    }
)
_FIELD_NOISE_KEYS = frozenset({"field_id", "description", "indexes"})
_FUNCTION_NOISE_KEYS = frozenset(
    {
        "id",
        "description",
        "input_field_ids",
        "output_field_ids",
    }
)
_INDEX_NOISE_KEYS = frozenset(
    {
        "total_rows",
        "indexed_rows",
        "pending_index_rows",
        "state",
        "index_id",
        "indexID",
        "index_state_fail_reason",
        "min_index_version",
        "max_index_version",
    }
)
_INDEX_IDENTITY_KEYS = frozenset(
    {"field_name", "index_name", "index_type", "metric_type", "params"}
)
_INT_PARAMETER_KEYS = frozenset(
    {"dim", "max_length", "max_capacity", "M", "efConstruction"}
)
_FLOAT_PARAMETER_KEYS = frozenset({"bm25_k1", "bm25_b"})
_BOOL_PARAMETER_KEYS = frozenset({"enable_analyzer", "enable_match", "mmap_enabled"})
_JSON_PARAMETER_KEYS = frozenset({"analyzer_params", "multi_analyzer_params"})

_IntEnumT = TypeVar("_IntEnumT", bound=IntEnum)


def _validated_dimension(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MILVUS_DENSE_VECTOR_MIN_DIMENSION
        <= value
        <= MILVUS_DENSE_VECTOR_MAX_DIMENSION
    ):
        raise InvariantViolationError
    return value


def _validated_analyzer(value: object) -> str:
    if not isinstance(value, str):
        raise InvariantViolationError
    try:
        value = validate_unicode_scalar_text(value)
    except ValueError as error:
        raise InvariantViolationError from error
    if (
        not value
        or value != value.strip()
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise InvariantViolationError
    return value


def build_collection_schema(
    embedding_dimension: int,
    *,
    analyzer: str = "standard",
) -> CollectionSchema:
    """Build the only collection schema AtlasRAG is allowed to create."""

    dimension = _validated_dimension(embedding_dimension)
    analyzer_name = _validated_analyzer(analyzer)
    schema = MilvusClient.create_schema(
        auto_id=False,
        enable_dynamic_field=False,
    )
    schema.add_field(
        field_name="chunk_id",
        datatype=DataType.VARCHAR,
        max_length=MILVUS_IDENTIFIER_MAX_LENGTH,
        is_primary=True,
        auto_id=False,
    )
    for field_name in ("document_id", "revision_id"):
        schema.add_field(
            field_name=field_name,
            datatype=DataType.VARCHAR,
            max_length=MILVUS_IDENTIFIER_MAX_LENGTH,
        )
    schema.add_field(
        field_name="file_name",
        datatype=DataType.VARCHAR,
        max_length=MILVUS_FILE_NAME_MAX_LENGTH,
    )
    schema.add_field(
        field_name="parent_id",
        datatype=DataType.VARCHAR,
        max_length=MILVUS_IDENTIFIER_MAX_LENGTH,
        nullable=True,
    )
    for field_name in ("chunk_type", "source_type"):
        schema.add_field(
            field_name=field_name,
            datatype=DataType.VARCHAR,
            max_length=MILVUS_ENUM_MAX_LENGTH,
        )
    schema.add_field(
        field_name="content",
        datatype=DataType.VARCHAR,
        max_length=MILVUS_CONTENT_MAX_LENGTH,
    )
    schema.add_field(
        field_name="sparse_text",
        datatype=DataType.VARCHAR,
        max_length=MILVUS_CONTENT_MAX_LENGTH,
        enable_analyzer=True,
        analyzer_params={"type": analyzer_name},
    )
    schema.add_field(
        field_name="dense_vector",
        datatype=DataType.FLOAT_VECTOR,
        dim=dimension,
    )
    schema.add_field(
        field_name="sparse_vector",
        datatype=DataType.SPARSE_FLOAT_VECTOR,
    )
    schema.add_field(
        field_name="structured_metadata",
        datatype=DataType.JSON,
    )
    schema.add_function(
        Function(
            name=BM25_FUNCTION_NAME,
            function_type=FunctionType.BM25,
            input_field_names=["sparse_text"],
            output_field_names=["sparse_vector"],
        )
    )
    schema.verify()
    return schema


def build_index_params() -> IndexParams:
    """Build the frozen dense HNSW and built-in BM25 index definitions."""

    indexes = MilvusClient.prepare_index_params()
    indexes.add_index(
        field_name="dense_vector",
        index_name=DENSE_INDEX_NAME,
        index_type="HNSW",
        metric_type="COSINE",
        params={"M": 32, "efConstruction": 200},
    )
    indexes.add_index(
        field_name="sparse_vector",
        index_name=SPARSE_INDEX_NAME,
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25",
        params={
            "inverted_index_algo": "DAAT_MAXSCORE",
            "bm25_k1": 1.2,
            "bm25_b": 0.75,
        },
    )
    return indexes


def _invalid(value: object) -> str:
    return f"<invalid {type(value).__name__}: {value!r}>"


def _normalize_bool(value: object) -> bool | str:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return _invalid(value)


def _normalize_int(value: object) -> int | str:
    if isinstance(value, bool):
        return _invalid(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    return _invalid(value)


def _normalize_float(value: object) -> float | str:
    if isinstance(value, bool):
        return _invalid(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
    return _invalid(value)


def _normalize_enum(value: object, enum_type: type[_IntEnumT]) -> str:
    if isinstance(value, enum_type):
        return value.name
    if isinstance(value, bool):
        return _invalid(value)
    if isinstance(value, int):
        try:
            return enum_type(value).name
        except ValueError:
            return _invalid(value)
    if isinstance(value, str):
        candidate = value.strip()
        if "." in candidate:
            candidate = candidate.rsplit(".", 1)[-1]
        if candidate.lstrip("-").isdigit():
            try:
                return enum_type(int(candidate)).name
            except ValueError:
                return _invalid(value)
        try:
            return enum_type[candidate.upper()].name
        except KeyError:
            return _invalid(value)
    return _invalid(value)


def _normalize_json(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in sorted(value.items(), key=lambda pair: repr(pair[0])):
            if not isinstance(key, str):
                normalized[_invalid(key)] = _normalize_json(item)
            else:
                normalized[key] = _normalize_json(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return tuple(_normalize_json(item) for item in value)
    return _invalid(value)


def _normalize_parameter(key: str, value: object) -> object:
    if key in _INT_PARAMETER_KEYS:
        return _normalize_int(value)
    if key in _FLOAT_PARAMETER_KEYS:
        return _normalize_float(value)
    if key in _BOOL_PARAMETER_KEYS:
        return _normalize_bool(value)
    if key in _JSON_PARAMETER_KEYS and isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return _invalid(value)
    if key in {"index_type", "metric_type", "inverted_index_algo"} and isinstance(
        value, str
    ):
        return value.upper()
    return _normalize_json(value)


def _normalize_params(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {"<invalid-params>": _invalid(value)}
    normalized: dict[str, object] = {}
    for key, item in sorted(value.items(), key=lambda pair: repr(pair[0])):
        if not isinstance(key, str):
            normalized[_invalid(key)] = _normalize_json(item)
        else:
            normalized[key] = _normalize_parameter(key, item)
    return normalized


def _normalized_name(value: object, *, kind: str, position: int) -> str:
    if isinstance(value, str) and value:
        return value
    return f"<invalid-{kind}-{position}:{_invalid(value)}>"


def _insert_named(
    target: dict[str, object],
    name: str,
    value: object,
    *,
    kind: str,
) -> None:
    if name not in target:
        target[name] = value
        return
    suffix = 2
    duplicate_name = f"<duplicate-{kind}:{name}:{suffix}>"
    while duplicate_name in target:
        suffix += 1
        duplicate_name = f"<duplicate-{kind}:{name}:{suffix}>"
    target[duplicate_name] = value


def _normalize_field(value: object, position: int) -> tuple[str, object]:
    if not isinstance(value, Mapping):
        name = f"<invalid-field-{position}>"
        return name, {"invalid_payload": _invalid(value)}
    name = _normalized_name(value.get("name"), kind="field", position=position)
    params = _normalize_params(value.get("params", {}))
    normalized: dict[str, object] = {
        "type": _normalize_enum(value.get("type"), DataType),
        "params": params,
        "is_primary": _normalize_bool(value.get("is_primary", False)),
        "auto_id": _normalize_bool(value.get("auto_id", False)),
        "nullable": _normalize_bool(value.get("nullable", False)),
        "is_function_output": _normalize_bool(value.get("is_function_output", False)),
        "is_partition_key": _normalize_bool(value.get("is_partition_key", False)),
        "is_clustering_key": _normalize_bool(value.get("is_clustering_key", False)),
        "is_dynamic": _normalize_bool(value.get("is_dynamic", False)),
    }
    if "element_type" in value:
        normalized["element_type"] = _normalize_enum(value["element_type"], DataType)
    if "default_value" in value and value["default_value"] is not None:
        normalized["default_value"] = _normalize_json(value["default_value"])
    known_keys = set(normalized) | {"name"} | _FIELD_NOISE_KEYS
    unexpected = sorted(
        str(key) for key in value if not isinstance(key, str) or key not in known_keys
    )
    if unexpected:
        normalized["unexpected_keys"] = tuple(unexpected)
    return name, normalized


def _normalize_name_sequence(value: object) -> tuple[object, ...] | str:
    if not isinstance(value, (list, tuple)):
        return _invalid(value)
    return tuple(item if isinstance(item, str) else _invalid(item) for item in value)


def _normalize_function(value: object, position: int) -> tuple[str, object]:
    if not isinstance(value, Mapping):
        name = f"<invalid-function-{position}>"
        return name, {"invalid_payload": _invalid(value)}
    name = _normalized_name(value.get("name"), kind="function", position=position)
    normalized: dict[str, object] = {
        "type": _normalize_enum(value.get("type"), FunctionType),
        "input_field_names": _normalize_name_sequence(value.get("input_field_names")),
        "output_field_names": _normalize_name_sequence(value.get("output_field_names")),
        "params": _normalize_params(value.get("params", {})),
    }
    known_keys = set(normalized) | {"name"} | _FUNCTION_NOISE_KEYS
    unexpected = sorted(
        str(key) for key in value if not isinstance(key, str) or key not in known_keys
    )
    if unexpected:
        normalized["unexpected_keys"] = tuple(unexpected)
    return name, normalized


def _normalize_named_values(
    value: object,
    *,
    kind: str,
) -> dict[str, object]:
    if not isinstance(value, (list, tuple)):
        return {f"<invalid-{kind}-list>": _invalid(value)}
    normalized: dict[str, object] = {}
    for position, item in enumerate(value):
        if kind == "field":
            name, normalized_item = _normalize_field(item, position)
        else:
            name, normalized_item = _normalize_function(item, position)
        _insert_named(normalized, name, normalized_item, kind=kind)
    return dict(sorted(normalized.items()))


def normalize_collection_description(description: object) -> dict[str, object]:
    """Project a PyMilvus collection response onto compatibility semantics."""

    if not isinstance(description, Mapping):
        return {"invalid_payload": _invalid(description)}
    normalized: dict[str, object] = {
        "collection_name": description.get("collection_name", "<missing>"),
        "auto_id": _normalize_bool(description.get("auto_id", "<missing>")),
        "enable_dynamic_field": _normalize_bool(
            description.get("enable_dynamic_field", "<missing>")
        ),
        "enable_namespace": _normalize_bool(
            description.get("enable_namespace", "<missing>")
        ),
        "fields": _normalize_named_values(
            description.get("fields", "<missing>"), kind="field"
        ),
        "functions": _normalize_named_values(
            description.get("functions", "<missing>"), kind="function"
        ),
        "properties": _normalize_json(description.get("properties", {})),
    }
    unexpected = sorted(
        str(key)
        for key in description
        if not isinstance(key, str)
        or key not in _COLLECTION_SEMANTIC_KEYS | _COLLECTION_NOISE_KEYS
    )
    if unexpected:
        normalized["unexpected_keys"] = tuple(unexpected)
    return normalized


def _parse_nested_index_params(value: object) -> dict[str, object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {"<invalid-params>": _invalid(value)}
    return _normalize_params(value)


def _normalize_index(value: object, position: int) -> tuple[str, object]:
    if not isinstance(value, Mapping):
        name = f"<invalid-index-{position}>"
        return name, {"invalid_payload": _invalid(value)}
    name = _normalized_name(value.get("index_name"), kind="index", position=position)
    params = _parse_nested_index_params(value.get("params", {}))

    has_nested_index_type = "index_type" in params
    has_nested_metric_type = "metric_type" in params
    nested_index_type = params.pop("index_type", None)
    nested_metric_type = params.pop("metric_type", None)
    raw_index_type = value.get("index_type", nested_index_type or "<missing>")
    raw_metric_type = value.get("metric_type", nested_metric_type or "<missing>")
    normalized_index_type = _normalize_parameter("index_type", raw_index_type)
    normalized_metric_type = _normalize_parameter("metric_type", raw_metric_type)
    if (
        "index_type" in value
        and has_nested_index_type
        and normalized_index_type != nested_index_type
    ):
        params["<conflict:index_type>"] = {
            "top_level": normalized_index_type,
            "params": nested_index_type,
        }
    if (
        "metric_type" in value
        and has_nested_metric_type
        and normalized_metric_type != nested_metric_type
    ):
        params["<conflict:metric_type>"] = {
            "top_level": normalized_metric_type,
            "params": nested_metric_type,
        }
    for key, item in value.items():
        if not isinstance(key, str):
            params[_invalid(key)] = _normalize_json(item)
        elif key not in _INDEX_IDENTITY_KEYS | _INDEX_NOISE_KEYS:
            normalized_item = _normalize_parameter(key, item)
            if key in params and params[key] != normalized_item:
                params[f"<conflict:{key}>"] = normalized_item
            else:
                params[key] = normalized_item

    normalized = {
        "field_name": value.get("field_name", "<missing>"),
        "index_type": normalized_index_type,
        "metric_type": normalized_metric_type,
        "params": dict(sorted(params.items())),
    }
    return name, normalized


def normalize_index_descriptions(descriptions: object) -> dict[str, object]:
    """Project PyMilvus index responses onto compatibility semantics."""

    if not isinstance(descriptions, (list, tuple)):
        return {"<invalid-index-list>": _invalid(descriptions)}
    normalized: dict[str, object] = {}
    for position, description in enumerate(descriptions):
        name, normalized_index = _normalize_index(description, position)
        _insert_named(normalized, name, normalized_index, kind="index")
    return dict(sorted(normalized.items()))


def _expected_collection_description(
    embedding_dimension: int,
    *,
    analyzer: str,
) -> dict[str, object]:
    raw = build_collection_schema(
        embedding_dimension,
        analyzer=analyzer,
    ).to_dict()
    return {"collection_name": COLLECTION_NAME, **raw}


def _expected_index_descriptions() -> list[dict[str, object]]:
    return [parameter.to_dict() for parameter in build_index_params()]


def _mismatches(expected: object, actual: object, *, path: str) -> list[str]:
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        mismatches: list[str] = []
        expected_keys = {str(key) for key in expected}
        actual_keys = {str(key) for key in actual}
        for key in sorted(expected_keys - actual_keys):
            mismatches.append(f"{path}.{key}: missing")
        for key in sorted(actual_keys - expected_keys):
            mismatches.append(f"{path}.{key}: unexpected")
        for key in sorted(expected_keys & actual_keys):
            mismatches.extend(
                _mismatches(expected[key], actual[key], path=f"{path}.{key}")
            )
        return mismatches
    if expected != actual:
        return [f"{path}: expected {expected!r}, got {actual!r}"]
    return []


def collection_schema_mismatches(
    actual_description: object,
    *,
    embedding_dimension: int,
    analyzer: str = "standard",
) -> tuple[str, ...]:
    """Return every collection incompatibility; an empty tuple means compatible."""

    expected = normalize_collection_description(
        _expected_collection_description(
            embedding_dimension,
            analyzer=analyzer,
        )
    )
    actual = normalize_collection_description(actual_description)
    return tuple(_mismatches(expected, actual, path="collection"))


def index_schema_mismatches(actual_descriptions: object) -> tuple[str, ...]:
    """Return every index incompatibility; an empty tuple means compatible."""

    expected = normalize_index_descriptions(_expected_index_descriptions())
    actual = normalize_index_descriptions(actual_descriptions)
    return tuple(_mismatches(expected, actual, path="indexes"))
