"""Provider-neutral reranking boundary for CrossEncoder/ColBERT/RankLLM."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Protocol

from pydantic import Field, StrictInt, StrictStr

from atlasrag.domain.base import FrozenModel
from atlasrag.domain.errors import DependencyUnavailableError


class RerankerModelIdentity(FrozenModel):
    model_id: StrictStr
    model_revision: StrictStr
    tokenizer_id: StrictStr
    tokenizer_revision: StrictStr
    device: StrictStr
    dtype: StrictStr
    max_input_tokens: Annotated[StrictInt, Field(ge=1)]


class RerankerInputLimitError(ValueError):
    """A bounded reranker view exceeds the explicit token capacity."""


class RerankerUnavailableError(DependencyUnavailableError):
    """Typed runtime failure at the reranker provider boundary."""


class RerankerProvider(Protocol):
    @property
    def identity(self) -> RerankerModelIdentity: ...

    async def initialize(self, *, allow_download: bool = False) -> None: ...

    async def score(self, query: str, passages: Sequence[str]) -> tuple[float, ...]: ...
