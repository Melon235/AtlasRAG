"""Local evidence subgraph State schemas."""

from typing import NotRequired, TypedDict

from atlasrag.domain.evidence import EvidenceRef, LocalEvidence, LocalEvidenceResult
from atlasrag.domain.requests import LocalRetrievalRequest
from atlasrag.domain.retrieval import CandidatePool, RetrievalBranchResult


class LocalEvidenceSubgraphInput(TypedDict):
    """Input admitted by one local evidence attempt."""

    request: LocalRetrievalRequest


class LocalEvidenceState(TypedDict):
    """Minimal execution data owned by one local evidence attempt."""

    request: LocalRetrievalRequest
    dense_branch: NotRequired[RetrievalBranchResult]
    bm25_branch: NotRequired[RetrievalBranchResult]
    candidate_pool: NotRequired[CandidatePool]
    evidence_refs: NotRequired[tuple[EvidenceRef, ...]]
    local_evidence: NotRequired[tuple[LocalEvidence, ...]]
    result: NotRequired[LocalEvidenceResult]


class LocalEvidenceSubgraphOutput(TypedDict):
    """Business result returned by one local evidence attempt."""

    result: LocalEvidenceResult
