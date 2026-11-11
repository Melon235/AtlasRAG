"""Stage 6 scope/filter security tests."""

from __future__ import annotations

import pytest

from atlasrag.domain.enums import RetrievalChunkType, SourceType
from atlasrag.domain.requests import LocalRetrievalRequest, QueryFilters
from atlasrag.services.local_evidence.scope import (
    PublishedRevision,
    PublishedRevisionSet,
    RetrievalFilterCompiler,
    RetrievalScopeBuilder,
)


def revision(
    document: str, revision_id: str, file_name: str = "a.md"
) -> PublishedRevision:
    return PublishedRevision(
        document_id=document,
        revision_id=revision_id,
        file_name=file_name,
        source_type=SourceType.MD.value,
    )


def test_scope_builder_flattens_user_filters_and_internal_chunk_types() -> None:
    request = LocalRetrievalRequest(
        original_query="question",
        retrieval_query="rewritten",
        filters=QueryFilters(
            file_name="a.md", source_type=SourceType.MD, sheet_name="S1"
        ),
    )
    scope = RetrievalScopeBuilder().build(request)
    assert scope.file_name == "a.md"
    assert scope.source_type is SourceType.MD
    assert scope.sheet_name == "S1"
    assert scope.chunk_types == (
        RetrievalChunkType.TEXT_CHILD,
        RetrievalChunkType.TABLE,
    )


def test_filter_compiler_uses_only_allowlisted_fields_and_pairs() -> None:
    scope = RetrievalScopeBuilder().build(
        LocalRetrievalRequest(
            original_query="q",
            retrieval_query="r",
            filters=QueryFilters(file_name='a" OR true OR "', sheet_name="Sheet 1"),
        )
    )
    published = PublishedRevisionSet(
        revisions=(revision("doc_" + "a" * 64, "rev_" + "b" * 64),)
    )
    compiled = RetrievalFilterCompiler().compile(scope, published)
    assert not compiled.empty
    assert 'file_name == "a\\" OR true OR \\""' in compiled.expression
    assert "sheet_name ==" not in compiled.expression
    assert (
        'structured_metadata["provenance"]["sheet_name"] == "Sheet 1"'
        in compiled.expression
    )
    assert 'chunk_type in ["TEXT_CHILD", "TABLE"]' in compiled.expression
    assert (
        '(document_id == "doc_'
        + "a" * 64
        + '" and revision_id == "rev_'
        + "b" * 64
        + '")'
        in compiled.expression
    )
    assert compiled.published_revisions == 1


def test_empty_published_scope_compiles_to_no_search() -> None:
    scope = RetrievalScopeBuilder().build(
        LocalRetrievalRequest(original_query="q", retrieval_query="r")
    )
    compiled = RetrievalFilterCompiler().compile(scope, PublishedRevisionSet())
    assert compiled.empty
    assert compiled.expression == ""


def test_published_revision_rejects_noncanonical_identity() -> None:
    with pytest.raises(ValueError):
        PublishedRevision(
            document_id="not-a-document",
            revision_id="rev_" + "a" * 64,
            file_name="a.md",
            source_type="MD",
        )
