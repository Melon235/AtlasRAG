"""Shared Pydantic foundations for AtlasRAG domain contracts."""

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    """Immutable model base with strict field and finite-number boundaries."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
