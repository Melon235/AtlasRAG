"""Injected Stage 6 service bundle; no SDK/model lifecycle in Graph State."""

from __future__ import annotations

from dataclasses import dataclass

from atlasrag.services.local_evidence.parent_context import ParentContextService
from atlasrag.services.local_evidence.profile import LocalEvidenceProfile
from atlasrag.services.local_evidence.reranking import RerankerService
from atlasrag.services.local_evidence.retrieval import HybridRetrievalService
from atlasrag.services.local_evidence.selection import UniqueEvidenceSelector
from atlasrag.services.local_evidence.sufficiency import (
    LocalEvidenceSufficiencyStrategy,
)


@dataclass(frozen=True)
class LocalEvidenceServices:
    retrieval: HybridRetrievalService
    reranker: RerankerService
    selector: UniqueEvidenceSelector
    parent_context: ParentContextService
    sufficiency: LocalEvidenceSufficiencyStrategy
    profile: LocalEvidenceProfile
