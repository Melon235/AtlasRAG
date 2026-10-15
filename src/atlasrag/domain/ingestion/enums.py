"""Stable Stage 3 canonical parsing and element vocabularies."""

from enum import StrEnum


class ParseOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class ParseReliability(StrEnum):
    RELIABLE = "RELIABLE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class ElementType(StrEnum):
    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    LIST = "LIST"
    CODE = "CODE"
    TABLE = "TABLE"


class ListKind(StrEnum):
    ORDERED = "ORDERED"
    UNORDERED = "UNORDERED"
