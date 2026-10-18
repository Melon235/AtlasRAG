"""Lossless scalar/JSON observations for independent candidate verification."""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated

from pydantic import PlainSerializer, PlainValidator, StrictStr

from atlasrag.domain.base import FrozenModel
from atlasrag.domain.errors import ProviderResponseError


class IndexWriteRejectedError(ProviderResponseError):
    """The SDK deterministically rejected candidate data or its schema."""

    safe_message = "The index rejected candidate data."


def _freeze_json(value: object, active: set[int]) -> object:
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, (Mapping, list, tuple)):
        identity = id(value)
        if identity in active:
            raise ValueError("cyclic JSON")
        active.add(identity)
        try:
            if isinstance(value, Mapping):
                if any(type(key) is not str for key in value):
                    raise ValueError("JSON keys must be strings")
                return MappingProxyType(
                    {key: _freeze_json(item, active) for key, item in value.items()}
                )
            return tuple(_freeze_json(item, active) for item in value)
        finally:
            active.remove(identity)
    raise ValueError("invalid JSON value")


def _metadata(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("metadata must be a JSON object")
    frozen = _freeze_json(value, set())
    assert isinstance(frozen, Mapping)
    return frozen


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


class MilvusRevisionProjection(FrozenModel):
    """Observed row, without admission or lineage rules and without vectors.

    Arbitrary string IDs, types, content and JSON metadata remain observable;
    the independent verifier decides whether they match canonical records.
    """

    chunk_id: StrictStr
    document_id: StrictStr
    revision_id: StrictStr
    parent_id: StrictStr | None
    chunk_type: StrictStr
    source_type: StrictStr
    file_name: StrictStr
    content: StrictStr
    sparse_text: StrictStr
    structured_metadata: Annotated[
        Mapping[str, object],
        PlainValidator(_metadata),
        PlainSerializer(_thaw_json),
    ]


PROJECTION_FIELDS = tuple(MilvusRevisionProjection.model_fields)
