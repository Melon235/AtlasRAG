"""Cross-boundary JSON serialization characterization tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from atlasrag.domain.answer import (
    AnswerInput,
    AnswerLocalEvidence,
    AnswerWebEvidence,
    GenerationResult,
)
from atlasrag.domain.enums import (
    CandidateOrdering,
    EvidenceType,
    LocalAnswerEvidenceStatus,
    LocalEvidenceStatus,
    LocalExecutionStatus,
    RagCompletionStatus,
    RetrievalChunkType,
    RetrievalMode,
    SourceType,
    TraceStatus,
    WebEvidenceRepresentation,
    WebExecutionStatus,
)
from atlasrag.domain.errors import ErrorCode
from atlasrag.domain.evidence import (
    LocalEvidence,
    LocalEvidenceResult,
    LocalProvenance,
    SourceAnchor,
)
from atlasrag.domain.requests import QueryFilters, UserTurnRequest
from atlasrag.domain.results import (
    FinalLocalCitation,
    FinalResponse,
    FinalWebCitation,
    RagCoreResult,
)
from atlasrag.domain.retrieval import (
    CandidatePool,
    CandidateScores,
    RetrievalCandidate,
)
from atlasrag.domain.trace import SpanRecord
from atlasrag.domain.web import WebEvidence, WebEvidenceResult


def _local_evidence() -> LocalEvidence:
    return LocalEvidence(
        evidence_type=EvidenceType.TEXT_PARENT,
        context_id="章节-架构-一",
        document_id="文档-42",
        revision_id="版本-七",
        content="AtlasRAG 保留来源边界，并支持 Unicode：🧭",
        provenance=LocalProvenance(
            file_name="架构说明.md",
            relative_source_path="docs/architecture-guide.md",
            section_path=("第一部分", "检索与证据"),
            source_anchor=SourceAnchor(page_number=7, heading="证据边界"),
        ),
    )


def _web_evidence() -> WebEvidence:
    return WebEvidence.model_validate(
        {
            "title": "AtlasRAG 发布说明 🛰️",
            "url": "https://docs.example.com/atlasrag/releases?lang=zh",
            "domain": "docs.example.com",
            "search_rank": 2,
            "content": "经过清洗的公开网页证据。",
            "representation": WebEvidenceRepresentation.SANITIZED_CONTENT,
        }
    )


def _final_response() -> FinalResponse:
    return FinalResponse(
        answer_text="本地与网页证据共同支持该结论。[L1][W1]",
        citations=(
            FinalLocalCitation(
                citation_id="L1",
                file_name="架构说明.md",
                relative_source_path="docs/architecture-guide.md",
                evidence_type=EvidenceType.TEXT_PARENT,
                source_anchor=SourceAnchor(page_number=7, heading="证据边界"),
            ),
            FinalWebCitation.model_validate(
                {
                    "citation_id": "W1",
                    "title": "AtlasRAG 发布说明 🛰️",
                    "url": "https://docs.example.com/atlasrag/releases?lang=zh",
                    "domain": "docs.example.com",
                }
            ),
        ),
    )


def _roundtrip_cases() -> tuple[BaseModel, ...]:
    local_evidence = _local_evidence()
    web_evidence = _web_evidence()
    final_response = _final_response()
    started_at = datetime(2026, 9, 27, 8, 15, 30, tzinfo=UTC)

    return (
        UserTurnRequest(
            session_id="会话-42",
            query="AtlasRAG 如何保持证据可追溯？ 🧭",
            filters=QueryFilters(
                file_name="架构说明.md",
                source_type=SourceType.MD,
                sheet_name="摘要",
            ),
        ),
        CandidatePool(
            candidates=(
                RetrievalCandidate(
                    chunk_id="子块-1",
                    document_id="文档-42",
                    revision_id="版本-七",
                    chunk_type=RetrievalChunkType.TEXT_CHILD,
                    parent_id="章节-架构-一",
                    retrieval_text="检索文本：来源必须可追溯。",
                    scores=CandidateScores(
                        dense_score=0.91,
                        bm25_score=7.25,
                        fusion_score=0.73,
                        rerank_score=None,
                    ),
                ),
                RetrievalCandidate(
                    chunk_id="表格-2",
                    document_id="文档-42",
                    revision_id="版本-七",
                    chunk_type=RetrievalChunkType.TABLE,
                    parent_id=None,
                    retrieval_text="阶段 | 边界 | 状态",
                    scores=CandidateScores(
                        dense_score=0.77,
                        bm25_score=None,
                        fusion_score=0.61,
                        rerank_score=0.88,
                    ),
                ),
            ),
            retrieval_mode=RetrievalMode.HYBRID,
            ordering=CandidateOrdering.RRF,
            degraded=True,
        ),
        LocalEvidenceResult(
            execution_status=LocalExecutionStatus.DEGRADED,
            evidence_status=LocalEvidenceStatus.SUFFICIENT,
            selected_evidence=(local_evidence,),
            best_available_evidence=local_evidence,
        ),
        WebEvidenceResult(
            execution_status=WebExecutionStatus.DEGRADED,
            evidence=(web_evidence,),
        ),
        AnswerInput(
            original_query="AtlasRAG 如何保持证据可追溯？ 🧭",
            local_evidence_status=LocalAnswerEvidenceStatus.SUFFICIENT,
            local_evidence=(
                AnswerLocalEvidence(citation_id="L1", evidence=local_evidence),
            ),
            web_evidence=(AnswerWebEvidence(citation_id="W1", evidence=web_evidence),),
        ),
        GenerationResult(
            answer_text="答案引用本地与网页来源。[L1][W1] ✅",
            used_citation_ids=("L1", "W1"),
        ),
        final_response,
        RagCoreResult(
            final_response=final_response,
            completion_status=RagCompletionStatus.DEGRADED_SUCCESS,
        ),
        SpanRecord(
            trace_id="追踪-42",
            span_id="跨度-生成",
            parent_span_id="跨度-检索",
            stage="answer_generation",
            attempt=2,
            started_at=started_at,
            ended_at=started_at + timedelta(milliseconds=27.5),
            duration_ms=27.5,
            status=TraceStatus.DEGRADED,
            error_code=ErrorCode.DEPENDENCY_TIMEOUT,
            attributes={
                "locale": "zh-CN",
                "citation_ids": ("L1", "W1"),
                "metrics": {
                    "candidate_count": 2,
                    "cache_hit": False,
                    "scores": (0.91, 0.88),
                },
                "note": "结构化属性也支持 Unicode：✨",
            },
        ),
    )


@pytest.mark.parametrize(
    "model",
    _roundtrip_cases(),
    ids=lambda model: type(model).__name__,
)
def test_boundary_model_json_roundtrip_preserves_semantics(model: BaseModel) -> None:
    model_type = type(model)

    restored = model_type.model_validate_json(model.model_dump_json())

    assert type(restored) is model_type
    assert restored == model
