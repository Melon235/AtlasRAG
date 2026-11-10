"""Precisely classified deterministic ingestion input failures."""


class ChunkInputLimitError(ValueError):
    """A table cannot fit the chunk capacity without losing canonical input.

    Internal CHUNK classification; ordinary configuration and algorithm errors
    remain unclassified ValueError instances. No public ErrorCode is added.
    """
