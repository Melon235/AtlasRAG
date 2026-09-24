"""Shared Pydantic foundations for AtlasRAG domain contracts."""

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict


def validate_ordered_collection_input(value: object) -> object:
    """Admit only order-preserving inputs before tuple coercion."""
    if not isinstance(value, (list, tuple)):
        raise ValueError("ordered collection input must be a list or tuple")
    return value


def validate_string_enum_input(value: object) -> object:
    """Reject byte and scalar coercion while retaining string-enum parsing."""
    if not isinstance(value, str):
        raise ValueError("enum input must be a string")
    return value


def validate_real_number_input(value: object) -> object:
    """Accept real int/float scores without bool or string coercion."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("score input must be an integer or float")
    return value


StrictReal = Annotated[float, BeforeValidator(validate_real_number_input)]


class FrozenModel(BaseModel):
    """Immutable model base with strict field and finite-number boundaries."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
