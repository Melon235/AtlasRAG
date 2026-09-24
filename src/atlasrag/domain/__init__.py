"""Public AtlasRAG domain contracts."""

from atlasrag.domain.evidence import (
    EvidenceRef,
    LocalEvidence,
    LocalEvidenceResult,
    LocalProvenance,
    SourceAnchor,
)
from atlasrag.domain.requests import (
    InternalRetrievalFilters,
    LocalRetrievalRequest,
    QueryFilters,
    ResolvedRetrievalScope,
    UserTurnRequest,
)
from atlasrag.domain.retrieval import (
    CandidatePool,
    CandidateScores,
    RetrievalBranchResult,
    RetrievalCandidate,
)
from atlasrag.domain.web import (
    PreparedWebSource,
    WebEvidence,
    WebEvidenceRequest,
    WebEvidenceResult,
    WebSearchResult,
)

__all__ = [
    "CandidatePool",
    "CandidateScores",
    "EvidenceRef",
    "InternalRetrievalFilters",
    "LocalEvidence",
    "LocalEvidenceResult",
    "LocalProvenance",
    "LocalRetrievalRequest",
    "PreparedWebSource",
    "QueryFilters",
    "ResolvedRetrievalScope",
    "RetrievalBranchResult",
    "RetrievalCandidate",
    "SourceAnchor",
    "UserTurnRequest",
    "WebEvidence",
    "WebEvidenceRequest",
    "WebEvidenceResult",
    "WebSearchResult",
]
