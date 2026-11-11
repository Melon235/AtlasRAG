"""Approved Stage 3 → Stage 6 whitespace-preserving retrieval admission."""

from __future__ import annotations

from stage3.test_chunk_contracts import ELEMENT_ID, child_chunk, parent_chunk

from atlasrag.domain.enums import RetrievalChunkType
from atlasrag.domain.ingestion.chunks import (
    ElementSpan,
    TextChildProvenance,
    TextParentProvenance,
)
from atlasrag.domain.retrieval import CandidateScores, RetrievalCandidate


def test_stage3_whitespace_child_maps_to_exact_retrieval_candidate() -> None:
    span = ElementSpan(
        element_id=ELEMENT_ID,
        element_order=0,
        char_start=2,
        char_end=5,
    )
    parent = parent_chunk(
        raw_content="   ",
        provenance=TextParentProvenance(source_spans=(span,)),
    )
    child = child_chunk(
        parent,
        raw_content="   ",
        provenance=TextChildProvenance(
            source_spans=(span,),
            parent_char_start=0,
            parent_char_end=3,
        ),
    )
    assert child.raw_content == "   "
    candidate = RetrievalCandidate(
        chunk_id=child.chunk_id,
        document_id=child.document_id,
        revision_id=child.revision_id,
        chunk_type=RetrievalChunkType.TEXT_CHILD,
        parent_id=child.parent_id,
        retrieval_text=child.raw_content,
        scores=CandidateScores(),
    )
    assert candidate.retrieval_text == "   "
    assert candidate.parent_id == parent.chunk_id
