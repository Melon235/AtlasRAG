"""Rank-first unique EvidenceRef selection; no child-level evidence fields."""

from __future__ import annotations

from atlasrag.domain.enums import EvidenceType, RetrievalChunkType
from atlasrag.domain.evidence import EvidenceRef
from atlasrag.domain.retrieval import CandidatePool
from atlasrag.services.local_evidence.profile import LocalEvidenceProfile


class UniqueEvidenceSelector:
    def __init__(self, *, profile: LocalEvidenceProfile) -> None:
        self._profile = profile

    def select(self, pool: CandidatePool) -> tuple[EvidenceRef, ...]:
        pool = CandidatePool.model_validate(pool)
        selected: list[EvidenceRef] = []
        seen: set[tuple[str, str, EvidenceType, str]] = set()
        for candidate in pool.candidates:
            if candidate.chunk_type is RetrievalChunkType.TEXT_CHILD:
                if candidate.parent_id is None:
                    raise ValueError("TEXT_CHILD candidates require parent_id")
                evidence_type = EvidenceType.TEXT_PARENT
                context_id = candidate.parent_id
            elif candidate.chunk_type is RetrievalChunkType.TABLE:
                evidence_type = EvidenceType.TABLE
                context_id = candidate.chunk_id
            else:
                raise ValueError("unsupported retrieval candidate type")
            identity = (
                candidate.document_id,
                candidate.revision_id,
                evidence_type,
                context_id,
            )
            if identity in seen:
                continue
            seen.add(identity)
            selected.append(
                EvidenceRef(
                    evidence_type=evidence_type,
                    context_id=context_id,
                    document_id=candidate.document_id,
                    revision_id=candidate.revision_id,
                )
            )
            if len(selected) >= self._profile.max_evidence:
                break
        return tuple(selected)
