"""Unique evidence identity/order tests."""

from __future__ import annotations

from atlasrag.domain.enums import (
    CandidateOrdering,
    EvidenceType,
    RetrievalChunkType,
    RetrievalMode,
)
from atlasrag.domain.retrieval import CandidatePool, CandidateScores, RetrievalCandidate
from atlasrag.services.local_evidence.profile import LocalEvidenceProfile
from atlasrag.services.local_evidence.selection import UniqueEvidenceSelector


def candidate(
    cid: str,
    parent: str | None,
    kind: RetrievalChunkType = RetrievalChunkType.TEXT_CHILD,
) -> RetrievalCandidate:
    return RetrievalCandidate(
        chunk_id=cid,
        document_id="doc-1",
        revision_id="rev-1",
        chunk_type=kind,
        parent_id=parent,
        retrieval_text="text",
        scores=CandidateScores(),
    )


def test_duplicate_children_collapse_to_first_parent_in_rank_order() -> None:
    pool = CandidatePool(
        candidates=(
            candidate("c1", "p1"),
            candidate("c2", "p1"),
            candidate("c3", "p2"),
            candidate("t1", None, RetrievalChunkType.TABLE),
        ),
        retrieval_mode=RetrievalMode.HYBRID,
        ordering=CandidateOrdering.RRF,
        degraded=False,
    )
    refs = UniqueEvidenceSelector(profile=LocalEvidenceProfile(max_evidence=3)).select(
        pool
    )
    assert [(ref.evidence_type, ref.context_id) for ref in refs] == [
        (EvidenceType.TEXT_PARENT, "p1"),
        (EvidenceType.TEXT_PARENT, "p2"),
        (EvidenceType.TABLE, "t1"),
    ]
    assert all(not hasattr(ref, "child_id") for ref in refs)
