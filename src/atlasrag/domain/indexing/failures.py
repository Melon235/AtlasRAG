"""Deterministic indexing integrity failures without provider dependencies."""


class EmbeddingInputLimitError(ValueError):
    """Deterministic EMBEDDING failure: the complete input exceeds its token cap."""

    def __init__(self, *, actual_tokens: int, max_tokens: int) -> None:
        self.actual_tokens = actual_tokens
        self.max_tokens = max_tokens
        super().__init__(
            f"embedding requires {actual_tokens} tokens; limit is {max_tokens}"
        )


class EmbeddingOutputError(ValueError):
    """Deterministic EMBEDDING failure: successful output violates vector shape."""


class RepresentationInputLimitError(ValueError):
    """A lossless representation exceeds the admitted UTF-8 capacity."""

    def __init__(self, *, actual_bytes: int, max_bytes: int) -> None:
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"representation requires {actual_bytes} UTF-8 bytes; limit is {max_bytes}"
        )
