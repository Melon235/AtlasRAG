"""Shared Pydantic foundations for AtlasRAG domain contracts."""

from collections.abc import Mapping
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, model_validator

from atlasrag._canonical import validate_unicode_scalar_text


def validate_ordered_collection_input(value: object) -> object:
    """Admit only order-preserving inputs before tuple coercion."""
    if not isinstance(value, (list, tuple)):
        raise ValueError("ordered collection input must be a list or tuple")
    return value


def validate_string_enum_input(value: object) -> object:
    """Reject byte and scalar coercion while retaining string-enum parsing."""
    if not isinstance(value, str):
        raise ValueError("enum input must be a string")
    return validate_unicode_scalar_text(value)


def validate_real_number_input(value: object) -> object:
    """Accept real int/float scores without bool or string coercion."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("score input must be an integer or float")
    return value


StrictReal = Annotated[float, BeforeValidator(validate_real_number_input)]


def _validate_unicode_scalar_structure(
    value: object, visited: set[int] | None = None
) -> None:
    """Reject surrogate text recursively, including forged nested models."""
    if isinstance(value, str):
        validate_unicode_scalar_text(value)
        return
    if visited is None:
        visited = set()
    if isinstance(value, BaseModel):
        identity = id(value)
        if identity in visited:
            return
        visited.add(identity)
        for item in vars(value).values():
            _validate_unicode_scalar_structure(item, visited)
        return
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in visited:
            return
        visited.add(identity)
        for key, item in value.items():
            _validate_unicode_scalar_structure(key, visited)
            _validate_unicode_scalar_structure(item, visited)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        identity = id(value)
        if identity in visited:
            return
        visited.add(identity)
        for item in value:
            _validate_unicode_scalar_structure(item, visited)


class FrozenModel(BaseModel):
    """Immutable model base with strict field and finite-number boundaries."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        allow_inf_nan=False,
        revalidate_instances="always",
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_unicode_surrogates_recursively(cls, value: object) -> object:
        _validate_unicode_scalar_structure(value)
        return value
