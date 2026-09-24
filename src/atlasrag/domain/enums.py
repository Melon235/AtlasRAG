"""Stable string vocabularies shared by AtlasRAG domain contracts."""

from enum import StrEnum


class SourceType(StrEnum):
    PDF = "PDF"
    DOCX = "DOCX"
    XLSX = "XLSX"
    MD = "MD"
    TXT = "TXT"


class RetrievalChunkType(StrEnum):
    TEXT_CHILD = "TEXT_CHILD"
    TABLE = "TABLE"


class EvidenceType(StrEnum):
    TEXT_PARENT = "TEXT_PARENT"
    TABLE = "TABLE"


class RetrievalBranch(StrEnum):
    DENSE = "DENSE"
    BM25 = "BM25"


class RetrievalBranchStatus(StrEnum):
    OK = "OK"
    EMPTY = "EMPTY"
    UNAVAILABLE = "UNAVAILABLE"


class RetrievalMode(StrEnum):
    HYBRID = "HYBRID"
    DENSE_ONLY = "DENSE_ONLY"
    BM25_ONLY = "BM25_ONLY"
    UNAVAILABLE = "UNAVAILABLE"


class CandidateOrdering(StrEnum):
    RRF = "RRF"
    DENSE = "DENSE"
    BM25 = "BM25"
    RERANKER = "RERANKER"


class LocalExecutionStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class LocalEvidenceStatus(StrEnum):
    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT = "INSUFFICIENT"
    AMBIGUOUS = "AMBIGUOUS"


class WebContentOrigin(StrEnum):
    FETCHED_PAGE = "FETCHED_PAGE"
    SEARCH_SNIPPET = "SEARCH_SNIPPET"


class WebEvidenceRepresentation(StrEnum):
    SUMMARY = "SUMMARY"
    SANITIZED_CONTENT = "SANITIZED_CONTENT"
    SEARCH_SNIPPET = "SEARCH_SNIPPET"


class WebExecutionStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    EMPTY = "EMPTY"
    UNAVAILABLE = "UNAVAILABLE"


class LocalAnswerEvidenceStatus(StrEnum):
    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT_BEST_MATCH = "INSUFFICIENT_BEST_MATCH"
    NONE = "NONE"


class OutputReviewDecision(StrEnum):
    ACCEPT = "ACCEPT"
    REGENERATE = "REGENERATE"
    BLOCK = "BLOCK"


class OutputReviewReasonCode(StrEnum):
    UNKNOWN_CITATION = "UNKNOWN_CITATION"
    CITATION_SET_MISMATCH = "CITATION_SET_MISMATCH"
    INVALID_OUTPUT_SCHEMA = "INVALID_OUTPUT_SCHEMA"
    FORGED_SOURCE_REFERENCE = "FORGED_SOURCE_REFERENCE"
    SOURCE_BOUNDARY_VIOLATION = "SOURCE_BOUNDARY_VIOLATION"
    CONTROL_LEAKAGE = "CONTROL_LEAKAGE"
    PROTOCOL_CONTENT = "PROTOCOL_CONTENT"


class SourceKind(StrEnum):
    LOCAL = "LOCAL"
    WEB = "WEB"


class RagCompletionStatus(StrEnum):
    SUCCESS = "SUCCESS"
    DEGRADED_SUCCESS = "DEGRADED_SUCCESS"
    SAFE_FAILURE = "SAFE_FAILURE"


class AnswerExecutionOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    SAFE_FAILURE = "SAFE_FAILURE"


class TraceStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
