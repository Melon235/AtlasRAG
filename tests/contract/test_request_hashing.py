"""Deterministic user-request hashing contracts."""

from __future__ import annotations

import pytest

from atlasrag.application.hashing import request_hash
from atlasrag.domain.enums import SourceType
from atlasrag.domain.requests import QueryFilters, UserTurnRequest


def user_request(
    *,
    session_id: str = "session-a",
    query: str = "What is the leave policy?",
    filters: QueryFilters | None = None,
) -> UserTurnRequest:
    return UserTurnRequest(session_id=session_id, query=query, filters=filters)


def test_request_hash_is_repeatable_lowercase_sha256_hex() -> None:
    request = user_request(
        query="请总结休假政策",
        filters=QueryFilters(file_name="员工手册.pdf"),
    )

    first = request_hash(request)
    second = request_hash(request)

    assert first == second
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")


def test_request_hash_excludes_session_id() -> None:
    assert request_hash(user_request(session_id="session-a")) == request_hash(
        user_request(session_id="session-b")
    )


def test_request_hash_normalizes_absent_and_all_none_filters() -> None:
    without_filters = user_request(filters=None)
    with_semantically_empty_filters = user_request(
        filters=QueryFilters(file_name=None, source_type=None, sheet_name=None)
    )

    assert request_hash(without_filters) == request_hash(
        with_semantically_empty_filters
    )


def test_request_hash_is_independent_of_filter_construction_order() -> None:
    first_filters = QueryFilters(
        file_name="handbook.pdf",
        source_type=SourceType.PDF,
        sheet_name="Policy",
    )
    reverse_order_payload = {
        "sheet_name": "Policy",
        "source_type": "PDF",
        "file_name": "handbook.pdf",
    }
    second_filters = QueryFilters.model_validate(reverse_order_payload)

    assert request_hash(user_request(filters=first_filters)) == request_hash(
        user_request(filters=second_filters)
    )


@pytest.mark.parametrize(
    "changed_request",
    [
        user_request(query="What is the parental leave policy?"),
        user_request(filters=QueryFilters(file_name="handbook.pdf")),
        user_request(filters=QueryFilters(source_type=SourceType.PDF)),
        user_request(filters=QueryFilters(sheet_name="Policy")),
    ],
)
def test_query_or_any_supplied_filter_changes_request_hash(
    changed_request: UserTurnRequest,
) -> None:
    assert request_hash(changed_request) != request_hash(user_request())


def test_request_hash_preserves_validated_raw_query_text() -> None:
    assert request_hash(user_request(query="question")) != request_hash(
        user_request(query=" question ")
    )


def test_request_hash_preserves_supplied_filter_text() -> None:
    assert request_hash(
        user_request(filters=QueryFilters(file_name="handbook.pdf"))
    ) != request_hash(user_request(filters=QueryFilters(file_name=" handbook.pdf ")))


def test_request_hash_is_available_from_the_public_application_package() -> None:
    import atlasrag.application as public_application

    assert public_application.__all__ == ["request_hash"]
    assert public_application.request_hash is request_hash
