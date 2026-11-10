"""Internal immutable execution bounds, separate from frozen RuntimeConfig."""

from typing import Annotated

from pydantic import Field, StrictInt

from atlasrag.domain.base import FrozenModel


class EmbeddingExecutionProfile(FrozenModel):
    """Bound one inference call; token limits include tokenizer special tokens."""

    batch_size: Annotated[StrictInt, Field(ge=1)] = 1
    max_input_tokens: Annotated[StrictInt, Field(ge=1, le=32768)] = 8192
