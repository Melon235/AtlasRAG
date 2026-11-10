"""SDK-free embedding protocol and untrusted output validation."""

from collections.abc import Sequence
from math import isfinite
from typing import Protocol

from atlasrag.domain.indexing.failures import EmbeddingOutputError
from atlasrag.domain.indexing.models import EmbeddingModelIdentity


class EmbeddingProvider(Protocol):
    @property
    def identity(self) -> EmbeddingModelIdentity:
        """Identity established from loaded model and tokenizer assets."""
        ...

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Return one vector per input, in the same order."""
        ...


def validate_embedding_output(
    raw: object, *, expected_count: int, dimension: int
) -> tuple[tuple[float, ...], ...]:
    """Freeze ordered vectors only after checking count, dimension and finiteness.

    Positional correspondence is the provider contract; this boundary refuses
    unordered containers and preserves their order without sorting or slicing.
    """
    if type(expected_count) is not int or expected_count < 1:
        raise ValueError("expected_count must be a positive integer")
    if type(dimension) is not int or dimension < 1:
        raise ValueError("dimension must be a positive integer")
    if not isinstance(raw, (list, tuple)) or len(raw) != expected_count:
        raise EmbeddingOutputError("embedding output count or container is invalid")
    vectors: list[tuple[float, ...]] = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) != dimension:
            raise EmbeddingOutputError("embedding output dimension is invalid")
        values: list[float] = []
        for value in row:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EmbeddingOutputError("embedding output value is not numeric")
            try:
                number = float(value)
            except OverflowError:
                raise EmbeddingOutputError(
                    "embedding output value is not finite"
                ) from None
            if not isfinite(number):
                raise EmbeddingOutputError("embedding output value is not finite")
            values.append(number)
        vectors.append(tuple(values))
    return tuple(vectors)
