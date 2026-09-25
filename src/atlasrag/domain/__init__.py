"""Public AtlasRAG domain contracts."""

from atlasrag.domain.answer import (
    AnswerInput,
    AnswerLocalEvidence,
    AnswerWebEvidence,
    GenerationResult,
    OutputReviewResult,
    WorkingEvidenceBundle,
)
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
from atlasrag.domain.results import (
    FinalCitation,
    FinalLocalCitation,
    FinalResponse,
    FinalWebCitation,
    RagCoreResult,
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
    "AnswerInput",
    "AnswerLocalEvidence",
    "AnswerWebEvidence",
    "CandidatePool",
    "CandidateScores",
    "EvidenceRef",
    "FinalCitation",
    "FinalLocalCitation",
    "FinalResponse",
    "FinalWebCitation",
    "GenerationResult",
    "InternalRetrievalFilters",
    "LocalEvidence",
    "LocalEvidenceResult",
    "LocalProvenance",
    "LocalRetrievalRequest",
    "OutputReviewResult",
    "PreparedWebSource",
    "QueryFilters",
    "RagCoreResult",
    "ResolvedRetrievalScope",
    "RetrievalBranchResult",
    "RetrievalCandidate",
    "SourceAnchor",
    "UserTurnRequest",
    "WebEvidence",
    "WebEvidenceRequest",
    "WebEvidenceResult",
    "WebSearchResult",
    "WorkingEvidenceBundle",
]
