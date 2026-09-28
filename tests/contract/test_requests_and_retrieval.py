"""Contract tests for user requests and typed retrieval boundaries."""

from __future__ import annotations

from collections.abc import Callable
from itertools import product
from math import inf, nan

import pytest
from pydantic import BaseModel, ValidationError

from atlasrag.domain.enums import (
    CandidateOrdering,
    RetrievalBranch,
    RetrievalBranchStatus,
    RetrievalChunkType,
    RetrievalMode,
    SourceType,
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


def _unordered_collection(kind: str, values: tuple[object, ...]) -> object:
    if kind == "set":
        return set(values)
    if kind == "frozenset":
        return frozenset(values)
    if kind == "mapping":
        return dict.fromkeys(values)
    if kind == "generator":
        return (value for value in values)
    raise AssertionError(f"unsupported unordered collection kind: {kind}")


def _assert_extra_field_rejected(
    model_type: type[BaseModel], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        model_type.model_validate({**payload, "unexpected": True})

    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"


def _assert_frozen(model_factory: Callable[[], BaseModel], field_name: str) -> None:
    model = model_factory()

    with pytest.raises(ValidationError) as exc_info:
        setattr(model, field_name, "changed")

    assert exc_info.value.errors()[0]["type"] == "frozen_instance"


def test_task2_models_are_importable_from_domain_public_api() -> None:
    from atlasrag.domain import (
        CandidatePool,
        CandidateScores,
        EvidenceRef,
        InternalRetrievalFilters,
        LocalEvidence,
        LocalEvidenceResult,
        LocalProvenance,
        LocalRetrievalRequest,
        PreparedWebSource,
        QueryFilters,
        ResolvedRetrievalScope,
        RetrievalBranchResult,
        RetrievalCandidate,
        SourceAnchor,
        UserTurnRequest,
        WebEvidence,
        WebEvidenceRequest,
        WebEvidenceResult,
        WebSearchResult,
    )

    public_models = (
        QueryFilters,
        UserTurnRequest,
        LocalRetrievalRequest,
        InternalRetrievalFilters,
        ResolvedRetrievalScope,
        CandidateScores,
        RetrievalCandidate,
        RetrievalBranchResult,
        CandidatePool,
        SourceAnchor,
        LocalProvenance,
        EvidenceRef,
        LocalEvidence,
        LocalEvidenceResult,
        WebEvidenceRequest,
        WebSearchResult,
        PreparedWebSource,
        WebEvidence,
        WebEvidenceResult,
    )

    assert tuple(model.__name__ for model in public_models) == (
        "QueryFilters",
        "UserTurnRequest",
        "LocalRetrievalRequest",
        "InternalRetrievalFilters",
        "ResolvedRetrievalScope",
        "CandidateScores",
        "RetrievalCandidate",
        "RetrievalBranchResult",
        "CandidatePool",
        "SourceAnchor",
        "LocalProvenance",
        "EvidenceRef",
        "LocalEvidence",
        "LocalEvidenceResult",
        "WebEvidenceRequest",
        "WebSearchResult",
        "PreparedWebSource",
        "WebEvidence",
        "WebEvidenceResult",
    )


def test_request_and_scope_models_have_exact_field_sets() -> None:
    assert tuple(QueryFilters.model_fields) == (
        "file_name",
        "source_type",
        "sheet_name",
    )
    assert tuple(UserTurnRequest.model_fields) == ("session_id", "query", "filters")
    assert tuple(LocalRetrievalRequest.model_fields) == (
        "original_query",
        "retrieval_query",
        "filters",
    )
    assert tuple(InternalRetrievalFilters.model_fields) == ("chunk_types",)
    assert tuple(ResolvedRetrievalScope.model_fields) == (
        "file_name",
        "source_type",
        "sheet_name",
        "chunk_types",
    )


def test_user_turn_request_canonicalizes_session_id_and_preserves_query() -> None:
    request = UserTurnRequest(session_id="  session-1  ", query="  What is C++?  ")

    assert request.session_id == "session-1"
    assert request.query == "  What is C++?  "
    assert request.filters is None


@pytest.mark.parametrize("field_name", ("session_id", "query"))
@pytest.mark.parametrize("blank", ("", " ", "\t\n"))
def test_user_turn_request_rejects_blank_required_strings(
    field_name: str, blank: str
) -> None:
    payload = {"session_id": "session-1", "query": "question"}
    payload[field_name] = blank

    with pytest.raises(ValidationError, match=field_name):
        UserTurnRequest.model_validate(payload)


@pytest.mark.parametrize("field_name", ("session_id", "query"))
def test_user_turn_request_rejects_byte_strings(field_name: str) -> None:
    payload: dict[str, object] = {"session_id": "session-1", "query": "question"}
    payload[field_name] = b"bytes are not text"

    with pytest.raises(ValidationError, match=field_name):
        UserTurnRequest.model_validate(payload)


def test_request_json_keeps_string_enums_and_raw_query_compatible() -> None:
    request = UserTurnRequest.model_validate_json(
        '{"session_id":"  session-1  ","query":"  raw query  ",'
        '"filters":{"source_type":"PDF"}}'
    )

    assert request.session_id == "session-1"
    assert request.query == "  raw query  "
    assert request.filters == QueryFilters(source_type=SourceType.PDF)


def test_query_filters_are_a_typed_external_allowlist() -> None:
    filters = QueryFilters.model_validate(
        {
            "file_name": " report.pdf ",
            "source_type": "PDF",
            "sheet_name": " Q1 ",
        }
    )

    assert filters.file_name == " report.pdf "
    assert filters.source_type is SourceType.PDF
    assert filters.sheet_name == " Q1 "
    assert not hasattr(filters, "chunk_type")
    assert not hasattr(filters, "options")
    assert not hasattr(filters, "top_k")


@pytest.mark.parametrize("field_name", ("file_name", "sheet_name"))
def test_query_filters_reject_blank_optional_strings(field_name: str) -> None:
    with pytest.raises(ValidationError, match=field_name):
        QueryFilters.model_validate({field_name: " \t "})


def test_query_filters_reject_unknown_source_types() -> None:
    with pytest.raises(ValidationError) as exc_info:
        QueryFilters(source_type="HTML")  # type: ignore[arg-type]

    assert exc_info.value.errors()[0]["type"] == "enum"


def test_query_filters_reject_byte_source_type() -> None:
    with pytest.raises(ValidationError, match="source_type"):
        QueryFilters.model_validate({"source_type": b"PDF"})


@pytest.mark.parametrize("field_name", ("original_query", "retrieval_query"))
def test_local_retrieval_request_rejects_blank_queries(field_name: str) -> None:
    payload = {
        "original_query": "original question",
        "retrieval_query": "rewritten question",
    }
    payload[field_name] = "  "

    with pytest.raises(ValidationError, match=field_name):
        LocalRetrievalRequest.model_validate(payload)


def test_local_retrieval_request_keeps_original_and_retrieval_queries_distinct() -> (
    None
):
    request = LocalRetrievalRequest(
        original_query="  original question  ",
        retrieval_query="  rewritten question  ",
        filters=QueryFilters(file_name="notes.md"),
    )

    assert request.original_query == "  original question  "
    assert request.retrieval_query == "  rewritten question  "
    assert request.filters == QueryFilters(file_name="notes.md")
    assert not hasattr(request, "session_id")
    assert not hasattr(request, "attempt")
    assert not hasattr(request, "protected_terms")


def test_internal_retrieval_filters_use_a_nonempty_unique_typed_tuple() -> None:
    filters = InternalRetrievalFilters.model_validate(
        {"chunk_types": ["TEXT_CHILD", "TABLE"]}
    )

    assert filters.chunk_types == (
        RetrievalChunkType.TEXT_CHILD,
        RetrievalChunkType.TABLE,
    )
    assert isinstance(filters.chunk_types, tuple)


@pytest.mark.parametrize(
    "chunk_types",
    ((), (RetrievalChunkType.TABLE, RetrievalChunkType.TABLE)),
)
def test_internal_retrieval_filters_reject_empty_or_duplicate_chunk_types(
    chunk_types: tuple[RetrievalChunkType, ...],
) -> None:
    with pytest.raises(ValidationError, match="chunk_types"):
        InternalRetrievalFilters(chunk_types=chunk_types)


def test_resolved_scope_is_flattened_and_requires_retrievable_chunk_types() -> None:
    scope = ResolvedRetrievalScope.model_validate(
        {
            "file_name": "notes.md",
            "source_type": SourceType.MD,
            "sheet_name": None,
            "chunk_types": [
                RetrievalChunkType.TEXT_CHILD,
                RetrievalChunkType.TABLE,
            ],
        }
    )

    assert scope.model_dump() == {
        "file_name": "notes.md",
        "source_type": SourceType.MD,
        "sheet_name": None,
        "chunk_types": (
            RetrievalChunkType.TEXT_CHILD,
            RetrievalChunkType.TABLE,
        ),
    }
    assert isinstance(scope.chunk_types, tuple)
    assert not hasattr(scope, "filters")
    assert not hasattr(scope, "options")


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"chunk_types": ()},
        {"chunk_types": (RetrievalChunkType.TABLE, RetrievalChunkType.TABLE)},
    ),
)
def test_resolved_scope_rejects_missing_empty_or_duplicate_chunk_types(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="chunk_types"):
        ResolvedRetrievalScope.model_validate(payload)


@pytest.mark.parametrize(
    ("model_type", "payload"),
    (
        (QueryFilters, {}),
        (UserTurnRequest, {"session_id": "session-1", "query": "question"}),
        (
            LocalRetrievalRequest,
            {
                "original_query": "question",
                "retrieval_query": "rewritten question",
            },
        ),
        (InternalRetrievalFilters, {}),
        (ResolvedRetrievalScope, {"chunk_types": ["TABLE"]}),
    ),
)
def test_request_and_scope_models_reject_unknown_fields(
    model_type: type[BaseModel], payload: dict[str, object]
) -> None:
    _assert_extra_field_rejected(model_type, payload)


@pytest.mark.parametrize(
    ("model_factory", "field_name"),
    (
        (QueryFilters, "file_name"),
        (lambda: UserTurnRequest(session_id="s", query="q"), "query"),
        (
            lambda: LocalRetrievalRequest(
                original_query="q", retrieval_query="rewritten"
            ),
            "retrieval_query",
        ),
        (InternalRetrievalFilters, "chunk_types"),
        (
            lambda: ResolvedRetrievalScope(chunk_types=(RetrievalChunkType.TABLE,)),
            "chunk_types",
        ),
    ),
)
def test_request_and_scope_models_are_immutable(
    model_factory: Callable[[], BaseModel], field_name: str
) -> None:
    _assert_frozen(model_factory, field_name)


def _candidate(
    *,
    chunk_id: str = "chunk-1",
    chunk_type: RetrievalChunkType = RetrievalChunkType.TEXT_CHILD,
    parent_id: str | None = "parent-1",
) -> RetrievalCandidate:
    return RetrievalCandidate(
        chunk_id=chunk_id,
        document_id="document-1",
        revision_id="revision-1",
        chunk_type=chunk_type,
        parent_id=parent_id,
        retrieval_text="retrievable text",
        scores=CandidateScores(),
    )


def test_retrieval_models_have_exact_field_sets() -> None:
    assert tuple(CandidateScores.model_fields) == (
        "dense_score",
        "bm25_score",
        "fusion_score",
        "rerank_score",
    )
    assert tuple(RetrievalCandidate.model_fields) == (
        "chunk_id",
        "document_id",
        "revision_id",
        "chunk_type",
        "parent_id",
        "retrieval_text",
        "scores",
    )
    assert tuple(RetrievalBranchResult.model_fields) == (
        "branch",
        "status",
        "candidates",
    )
    assert tuple(CandidatePool.model_fields) == (
        "candidates",
        "retrieval_mode",
        "ordering",
        "degraded",
    )


def test_candidate_scores_are_independently_optional() -> None:
    assert CandidateScores().model_dump() == {
        "dense_score": None,
        "bm25_score": None,
        "fusion_score": None,
        "rerank_score": None,
    }

    scores = CandidateScores(
        dense_score=0.8,
        bm25_score=12.0,
        fusion_score=0.03,
        rerank_score=-0.2,
    )

    assert scores.dense_score == 0.8
    assert scores.bm25_score == 12.0
    assert scores.fusion_score == 0.03
    assert scores.rerank_score == -0.2
    assert not hasattr(scores, "score")


@pytest.mark.parametrize("score_name", tuple(CandidateScores.model_fields))
@pytest.mark.parametrize("non_finite", (inf, -inf, nan))
def test_candidate_scores_reject_non_finite_values(
    score_name: str, non_finite: float
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        CandidateScores.model_validate({score_name: non_finite})

    assert exc_info.value.errors()[0]["type"] == "finite_number"


@pytest.mark.parametrize("score_name", tuple(CandidateScores.model_fields))
@pytest.mark.parametrize("malformed_score", (True, "1"))
def test_candidate_scores_reject_boolean_and_string_inputs(
    score_name: str, malformed_score: object
) -> None:
    with pytest.raises(ValidationError, match=score_name):
        CandidateScores.model_validate({score_name: malformed_score})


def test_candidate_scores_accept_real_integer_and_float_inputs() -> None:
    scores = CandidateScores.model_validate({"dense_score": 1, "bm25_score": 2.5})

    assert scores.dense_score == 1.0
    assert scores.bm25_score == 2.5


def test_text_child_requires_a_nonblank_parent_id() -> None:
    with pytest.raises(ValidationError, match="parent_id"):
        _candidate(parent_id=None)

    with pytest.raises(ValidationError, match="parent_id"):
        _candidate(parent_id="  ")


def test_table_candidate_may_omit_parent_id() -> None:
    candidate = _candidate(
        chunk_type=RetrievalChunkType.TABLE,
        parent_id=None,
    )

    assert candidate.chunk_type is RetrievalChunkType.TABLE
    assert candidate.parent_id is None


def test_text_parent_is_not_a_retrieval_candidate_type() -> None:
    payload: dict[str, object] = {
        "chunk_id": "chunk-1",
        "document_id": "document-1",
        "revision_id": "revision-1",
        "chunk_type": "TEXT_PARENT",
        "parent_id": None,
        "retrieval_text": "text",
        "scores": {},
    }

    with pytest.raises(ValidationError) as exc_info:
        RetrievalCandidate.model_validate(payload)

    assert exc_info.value.errors()[0]["type"] == "enum"


@pytest.mark.parametrize(
    "field_name",
    ("chunk_id", "document_id", "revision_id", "retrieval_text"),
)
def test_retrieval_candidate_rejects_blank_identity_and_text_fields(
    field_name: str,
) -> None:
    payload: dict[str, object] = {
        "chunk_id": "chunk-1",
        "document_id": "document-1",
        "revision_id": "revision-1",
        "chunk_type": RetrievalChunkType.TABLE,
        "parent_id": None,
        "retrieval_text": "text",
        "scores": CandidateScores(),
    }
    payload[field_name] = " \t "

    with pytest.raises(ValidationError, match=field_name):
        RetrievalCandidate.model_validate(payload)


@pytest.mark.parametrize(
    "field_name",
    ("chunk_id", "document_id", "revision_id", "parent_id", "retrieval_text"),
)
def test_retrieval_candidate_rejects_byte_text_fields(field_name: str) -> None:
    payload: dict[str, object] = {
        "chunk_id": "chunk-1",
        "document_id": "document-1",
        "revision_id": "revision-1",
        "chunk_type": RetrievalChunkType.TEXT_CHILD,
        "parent_id": "parent-1",
        "retrieval_text": "text",
        "scores": CandidateScores(),
    }
    payload[field_name] = b"bytes are not text"

    with pytest.raises(ValidationError, match=field_name):
        RetrievalCandidate.model_validate(payload)


def test_retrieval_candidate_has_no_provenance_or_rank_history() -> None:
    candidate = _candidate()

    assert not hasattr(candidate, "file_name")
    assert not hasattr(candidate, "source_type")
    assert not hasattr(candidate, "dense_rank")
    assert not hasattr(candidate, "bm25_rank")
    assert not hasattr(candidate, "rerank_rank")


def test_branch_result_converts_candidates_to_an_ordered_tuple() -> None:
    first = _candidate(chunk_id="chunk-1")
    second = _candidate(chunk_id="chunk-2")

    result = RetrievalBranchResult.model_validate(
        {
            "branch": RetrievalBranch.DENSE,
            "status": RetrievalBranchStatus.OK,
            "candidates": [first, second],
        }
    )

    assert isinstance(result.candidates, tuple)
    assert tuple(candidate.chunk_id for candidate in result.candidates) == (
        "chunk-1",
        "chunk-2",
    )


@pytest.mark.parametrize(
    "status", (RetrievalBranchStatus.EMPTY, RetrievalBranchStatus.UNAVAILABLE)
)
def test_empty_and_unavailable_branches_are_distinct_empty_outcomes(
    status: RetrievalBranchStatus,
) -> None:
    result = RetrievalBranchResult(
        branch=RetrievalBranch.BM25,
        status=status,
        candidates=(),
    )

    assert result.status is status
    assert result.candidates == ()
    assert not hasattr(result, "error")
    assert not hasattr(result, "message")


@pytest.mark.parametrize(
    ("status", "candidates"),
    (
        (RetrievalBranchStatus.OK, ()),
        (RetrievalBranchStatus.EMPTY, (_candidate(),)),
        (RetrievalBranchStatus.UNAVAILABLE, (_candidate(),)),
    ),
)
def test_branch_result_enforces_status_cardinality(
    status: RetrievalBranchStatus,
    candidates: tuple[RetrievalCandidate, ...],
) -> None:
    with pytest.raises(ValidationError, match="candidates"):
        RetrievalBranchResult(
            branch=RetrievalBranch.DENSE,
            status=status,
            candidates=candidates,
        )


@pytest.mark.parametrize(
    ("retrieval_mode", "ordering"),
    tuple(
        product(
            (
                RetrievalMode.HYBRID,
                RetrievalMode.DENSE_ONLY,
                RetrievalMode.BM25_ONLY,
            ),
            tuple(CandidateOrdering),
        )
    ),
)
def test_available_candidate_pool_modes_accept_every_declared_ordering(
    retrieval_mode: RetrievalMode, ordering: CandidateOrdering
) -> None:
    pool = CandidatePool.model_validate(
        {
            "candidates": [_candidate()],
            "retrieval_mode": retrieval_mode,
            "ordering": ordering,
            "degraded": True,
        }
    )

    assert pool.retrieval_mode is retrieval_mode
    assert pool.ordering is ordering
    assert pool.degraded is True
    assert isinstance(pool.candidates, tuple)


@pytest.mark.parametrize("malformed_degraded", ("false", 0))
def test_candidate_pool_requires_a_real_boolean_degraded_flag(
    malformed_degraded: object,
) -> None:
    with pytest.raises(ValidationError, match="degraded"):
        CandidatePool.model_validate(
            {
                "candidates": (_candidate(),),
                "retrieval_mode": RetrievalMode.HYBRID,
                "ordering": CandidateOrdering.RRF,
                "degraded": malformed_degraded,
            }
        )


@pytest.mark.parametrize("ordering", tuple(CandidateOrdering))
def test_unavailable_candidate_pool_is_empty_without_constraining_ordering(
    ordering: CandidateOrdering,
) -> None:
    pool = CandidatePool(
        candidates=(),
        retrieval_mode=RetrievalMode.UNAVAILABLE,
        ordering=ordering,
        degraded=True,
    )

    assert pool.candidates == ()
    assert pool.retrieval_mode is RetrievalMode.UNAVAILABLE
    assert pool.ordering is ordering


@pytest.mark.parametrize(
    "retrieval_mode",
    (
        RetrievalMode.HYBRID,
        RetrievalMode.DENSE_ONLY,
        RetrievalMode.BM25_ONLY,
    ),
)
def test_available_candidate_pool_modes_allow_empty_candidates(
    retrieval_mode: RetrievalMode,
) -> None:
    pool = CandidatePool(
        candidates=(),
        retrieval_mode=retrieval_mode,
        ordering=CandidateOrdering.RRF,
        degraded=False,
    )

    assert pool.candidates == ()
    assert pool.retrieval_mode is retrieval_mode


def test_unavailable_candidate_pool_rejects_candidates() -> None:
    with pytest.raises(ValidationError, match="candidates"):
        CandidatePool(
            candidates=(_candidate(),),
            retrieval_mode=RetrievalMode.UNAVAILABLE,
            ordering=CandidateOrdering.RRF,
            degraded=False,
        )


@pytest.mark.parametrize("unordered_kind", ("set", "frozenset", "mapping", "generator"))
@pytest.mark.parametrize(
    ("model_type", "payload", "field_name", "values"),
    (
        (
            InternalRetrievalFilters,
            {},
            "chunk_types",
            (RetrievalChunkType.TEXT_CHILD, RetrievalChunkType.TABLE),
        ),
        (
            ResolvedRetrievalScope,
            {},
            "chunk_types",
            (RetrievalChunkType.TEXT_CHILD, RetrievalChunkType.TABLE),
        ),
        (
            RetrievalBranchResult,
            {
                "branch": RetrievalBranch.DENSE,
                "status": RetrievalBranchStatus.OK,
            },
            "candidates",
            (_candidate(chunk_id="chunk-1"), _candidate(chunk_id="chunk-2")),
        ),
        (
            CandidatePool,
            {
                "retrieval_mode": RetrievalMode.HYBRID,
                "ordering": CandidateOrdering.RRF,
                "degraded": False,
            },
            "candidates",
            (_candidate(chunk_id="chunk-1"), _candidate(chunk_id="chunk-2")),
        ),
    ),
)
def test_ordered_request_and_retrieval_collections_reject_unordered_iterables(
    model_type: type[BaseModel],
    payload: dict[str, object],
    field_name: str,
    values: tuple[object, ...],
    unordered_kind: str,
) -> None:
    with pytest.raises(ValidationError, match=field_name):
        model_type.model_validate(
            {
                **payload,
                field_name: _unordered_collection(unordered_kind, values),
            }
        )


@pytest.mark.parametrize(
    ("model", "field_name"),
    (
        (
            InternalRetrievalFilters(
                chunk_types=(RetrievalChunkType.TEXT_CHILD, RetrievalChunkType.TABLE)
            ),
            "chunk_types",
        ),
        (
            ResolvedRetrievalScope(
                chunk_types=(RetrievalChunkType.TEXT_CHILD, RetrievalChunkType.TABLE)
            ),
            "chunk_types",
        ),
        (
            RetrievalBranchResult(
                branch=RetrievalBranch.DENSE,
                status=RetrievalBranchStatus.OK,
                candidates=(_candidate(),),
            ),
            "candidates",
        ),
        (
            CandidatePool(
                candidates=(_candidate(),),
                retrieval_mode=RetrievalMode.HYBRID,
                ordering=CandidateOrdering.RRF,
                degraded=False,
            ),
            "candidates",
        ),
    ),
)
def test_ordered_request_and_retrieval_collections_accept_json_arrays(
    model: BaseModel, field_name: str
) -> None:
    restored = type(model).model_validate_json(model.model_dump_json())

    assert isinstance(getattr(restored, field_name), tuple)


@pytest.mark.parametrize(
    ("model_type", "payload"),
    (
        (CandidateScores, {}),
        (
            RetrievalCandidate,
            {
                "chunk_id": "chunk-1",
                "document_id": "document-1",
                "revision_id": "revision-1",
                "chunk_type": "TABLE",
                "retrieval_text": "text",
                "scores": {},
            },
        ),
        (
            RetrievalBranchResult,
            {"branch": "DENSE", "status": "OK", "candidates": [_candidate()]},
        ),
        (
            CandidatePool,
            {
                "candidates": [_candidate()],
                "retrieval_mode": "HYBRID",
                "ordering": "RRF",
                "degraded": False,
            },
        ),
    ),
)
def test_retrieval_models_reject_unknown_fields(
    model_type: type[BaseModel], payload: dict[str, object]
) -> None:
    _assert_extra_field_rejected(model_type, payload)


@pytest.mark.parametrize(
    ("model_factory", "field_name"),
    (
        (CandidateScores, "dense_score"),
        (_candidate, "retrieval_text"),
        (
            lambda: RetrievalBranchResult(
                branch=RetrievalBranch.DENSE,
                status=RetrievalBranchStatus.OK,
                candidates=(_candidate(),),
            ),
            "candidates",
        ),
        (
            lambda: CandidatePool(
                candidates=(_candidate(),),
                retrieval_mode=RetrievalMode.HYBRID,
                ordering=CandidateOrdering.RRF,
                degraded=False,
            ),
            "ordering",
        ),
    ),
)
def test_retrieval_models_are_immutable(
    model_factory: Callable[[], BaseModel], field_name: str
) -> None:
    _assert_frozen(model_factory, field_name)
