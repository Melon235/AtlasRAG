"""Deterministic indexing integrity failures without provider dependencies."""


class RepresentationInputLimitError(ValueError):
    """A lossless representation exceeds the admitted UTF-8 capacity."""

    def __init__(self, *, actual_bytes: int, max_bytes: int) -> None:
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"representation requires {actual_bytes} UTF-8 bytes; limit is {max_bytes}"
        )
