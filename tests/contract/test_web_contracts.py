"""Contract tests for Web search and evidence boundaries."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import BaseModel, HttpUrl, ValidationError

from atlasrag.domain.enums import (
    WebContentOrigin,
    WebEvidenceRepresentation,
    WebExecutionStatus,
)
from atlasrag.domain.web import (
    PreparedWebSource,
    WebEvidence,
    WebEvidenceRequest,
    WebEvidenceResult,
    WebSearchResult,
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


def _search_payload() -> dict[str, object]:
    return {
        "title": "AtlasRAG documentation",
        "url": "https://docs.example.com/atlasrag",
        "domain": "docs.example.com",
        "snippet": "A relevant search snippet.",
        "search_rank": 1,
    }


def _prepared_payload() -> dict[str, object]:
    return {
        "title": "AtlasRAG documentation",
        "url": "https://docs.example.com/atlasrag",
        "domain": "docs.example.com",
        "search_rank": 1,
        "content": "Sanitized page content.",
        "content_origin": WebContentOrigin.FETCHED_PAGE,
    }


def _evidence_payload() -> dict[str, object]:
    return {
        "title": "AtlasRAG documentation",
        "url": "https://docs.example.com/atlasrag",
        "domain": "docs.example.com",
        "search_rank": 1,
        "content": "A grounded summary.",
        "representation": WebEvidenceRepresentation.SUMMARY,
    }


def _web_evidence(*, search_rank: int = 1) -> WebEvidence:
    return WebEvidence.model_validate(
        {**_evidence_payload(), "search_rank": search_rank}
    )


def test_web_models_have_exact_field_sets() -> None:
    assert tuple(WebEvidenceRequest.model_fields) == ("original_query",)
    assert tuple(WebSearchResult.model_fields) == (
        "title",
        "url",
        "domain",
        "snippet",
        "search_rank",
    )
    assert tuple(PreparedWebSource.model_fields) == (
        "title",
        "url",
        "domain",
        "search_rank",
        "content",
        "content_origin",
    )
    assert tuple(WebEvidence.model_fields) == (
        "title",
        "url",
        "domain",
        "search_rank",
        "content",
        "representation",
    )
    assert tuple(WebEvidenceResult.model_fields) == (
        "execution_status",
        "evidence",
    )


def test_web_evidence_request_preserves_a_nonblank_original_query() -> None:
    request = WebEvidenceRequest(original_query="  What is AtlasRAG?  ")

    assert request.original_query == "  What is AtlasRAG?  "


@pytest.mark.parametrize("blank", ("", " ", "\t\n"))
def test_web_evidence_request_rejects_blank_query(blank: str) -> None:
    with pytest.raises(ValidationError, match="original_query"):
        WebEvidenceRequest(original_query=blank)


def test_web_evidence_request_rejects_byte_query() -> None:
    with pytest.raises(ValidationError, match="original_query"):
        WebEvidenceRequest.model_validate({"original_query": b"query"})


@pytest.mark.parametrize(
    ("model_type", "payload"),
    (
        (WebSearchResult, _search_payload()),
        (PreparedWebSource, _prepared_payload()),
        (WebEvidence, _evidence_payload()),
    ),
)
@pytest.mark.parametrize("scheme", ("http", "https"))
def test_web_source_models_accept_strict_http_urls_and_matching_domains(
    model_type: type[WebSearchResult] | type[PreparedWebSource] | type[WebEvidence],
    payload: dict[str, object],
    scheme: str,
) -> None:
    model = model_type.model_validate(
        {
            **payload,
            "url": f"{scheme}://Example.COM/article",
            "domain": "EXAMPLE.com",
        }
    )

    assert isinstance(model.url, HttpUrl)
    assert model.url.host == "example.com"
    assert model.domain == "example.com"


@pytest.mark.parametrize(
    ("model_type", "payload"),
    (
        (WebSearchResult, _search_payload()),
        (PreparedWebSource, _prepared_payload()),
        (WebEvidence, _evidence_payload()),
    ),
)
def test_web_source_models_canonicalize_matching_unicode_idn_domains(
    model_type: type[WebSearchResult] | type[PreparedWebSource] | type[WebEvidence],
    payload: dict[str, object],
) -> None:
    model = model_type.model_validate(
        {
            **payload,
            "url": "https://BÜCHER.Example/article",
            "domain": "BÜCHER.Example",
        }
    )

    assert model.url.host == "xn--bcher-kva.example"
    assert model.domain == "xn--bcher-kva.example"


def test_web_source_models_share_modern_idn_normalization_for_sharp_s() -> None:
    result = WebSearchResult.model_validate(
        {
            **_search_payload(),
            "url": "https://faß.de/path",
            "domain": "faß.de",
        }
    )

    assert result.url.host == "xn--fa-hia.de"
    assert result.domain == "xn--fa-hia.de"


def test_web_source_url_ports_remain_valid_without_entering_domain() -> None:
    result = WebSearchResult.model_validate(
        {
            **_search_payload(),
            "url": "https://EXAMPLE.com:8443/article",
            "domain": "EXAMPLE.com",
        }
    )

    assert result.url.port == 8443
    assert result.domain == "example.com"


@pytest.mark.parametrize(
    "userinfo_url",
    (
        "https://user@example.com/article",
        "https://user:never-serialize-this-secret@example.com/article",
    ),
)
@pytest.mark.parametrize(
    ("model_type", "payload"),
    (
        (WebSearchResult, _search_payload()),
        (PreparedWebSource, _prepared_payload()),
        (WebEvidence, _evidence_payload()),
    ),
)
def test_web_source_models_reject_userinfo_before_it_can_serialize(
    model_type: type[BaseModel],
    payload: dict[str, object],
    userinfo_url: str,
) -> None:
    with pytest.raises(ValidationError, match="url"):
        model_type.model_validate(
            {**payload, "url": userinfo_url, "domain": "example.com"}
        )


@pytest.mark.parametrize(
    ("model_type", "payload"),
    (
        (WebSearchResult, _search_payload()),
        (PreparedWebSource, _prepared_payload()),
        (WebEvidence, _evidence_payload()),
    ),
)
def test_web_source_models_reject_byte_url_inputs(
    model_type: type[BaseModel], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError, match="url"):
        model_type.model_validate(
            {**payload, "url": b"https://docs.example.com/article"}
        )


@pytest.mark.parametrize(
    ("url", "domain", "error_field"),
    (
        ("https://example.com./article", "example.com", "url"),
        ("https://example.com/article", "example.com.", "domain"),
    ),
)
def test_web_source_models_reject_trailing_dot_host_aliases(
    url: str, domain: str, error_field: str
) -> None:
    with pytest.raises(ValidationError, match=error_field):
        WebSearchResult.model_validate(
            {**_search_payload(), "url": url, "domain": domain}
        )


@pytest.mark.parametrize(
    "invalid_url",
    (
        "ftp://example.com/file",
        "file:///tmp/page.html",
        "example.com/page",
        "/relative/page",
        "not a url",
    ),
)
@pytest.mark.parametrize(
    ("model_type", "payload"),
    (
        (WebSearchResult, _search_payload()),
        (PreparedWebSource, _prepared_payload()),
        (WebEvidence, _evidence_payload()),
    ),
)
def test_web_source_models_reject_non_http_urls(
    model_type: type[BaseModel], payload: dict[str, object], invalid_url: str
) -> None:
    with pytest.raises(ValidationError, match="url"):
        model_type.model_validate({**payload, "url": invalid_url})


@pytest.mark.parametrize(
    "invalid_domain",
    (
        "other.example.com",
        "https://docs.example.com",
        "docs.example.com/path",
        "docs.example.com:443",
        "user@docs.example.com",
        " docs.example.com ",
    ),
)
@pytest.mark.parametrize(
    ("model_type", "payload"),
    (
        (WebSearchResult, _search_payload()),
        (PreparedWebSource, _prepared_payload()),
        (WebEvidence, _evidence_payload()),
    ),
)
def test_web_source_models_require_a_matching_host_only_domain(
    model_type: type[BaseModel],
    payload: dict[str, object],
    invalid_domain: str,
) -> None:
    with pytest.raises(ValidationError, match="domain"):
        model_type.model_validate({**payload, "domain": invalid_domain})


@pytest.mark.parametrize(
    ("model_type", "payload", "field_name"),
    (
        (WebSearchResult, _search_payload(), "title"),
        (WebSearchResult, _search_payload(), "domain"),
        (WebSearchResult, _search_payload(), "snippet"),
        (PreparedWebSource, _prepared_payload(), "title"),
        (PreparedWebSource, _prepared_payload(), "domain"),
        (PreparedWebSource, _prepared_payload(), "content"),
        (WebEvidence, _evidence_payload(), "title"),
        (WebEvidence, _evidence_payload(), "domain"),
        (WebEvidence, _evidence_payload(), "content"),
    ),
)
def test_web_source_models_reject_blank_text_fields_when_present(
    model_type: type[BaseModel],
    payload: dict[str, object],
    field_name: str,
) -> None:
    with pytest.raises(ValidationError, match=field_name):
        model_type.model_validate({**payload, field_name: " \t "})


@pytest.mark.parametrize(
    ("model_type", "payload", "field_name"),
    (
        (WebSearchResult, _search_payload(), "title"),
        (WebSearchResult, _search_payload(), "domain"),
        (WebSearchResult, _search_payload(), "snippet"),
        (PreparedWebSource, _prepared_payload(), "content"),
        (WebEvidence, _evidence_payload(), "content"),
    ),
)
def test_web_source_models_reject_byte_text_fields(
    model_type: type[BaseModel],
    payload: dict[str, object],
    field_name: str,
) -> None:
    with pytest.raises(ValidationError, match=field_name):
        model_type.model_validate({**payload, field_name: b"bytes are not text"})


def test_web_search_snippet_is_optional_but_nonblank_when_present() -> None:
    payload = _search_payload()
    payload.pop("snippet")

    result = WebSearchResult.model_validate(payload)

    assert result.snippet is None


@pytest.mark.parametrize(
    ("model_type", "payload"),
    (
        (WebSearchResult, _search_payload()),
        (PreparedWebSource, _prepared_payload()),
        (WebEvidence, _evidence_payload()),
    ),
)
@pytest.mark.parametrize("invalid_rank", (0, -1))
def test_web_source_models_require_positive_search_rank(
    model_type: type[BaseModel],
    payload: dict[str, object],
    invalid_rank: int,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        model_type.model_validate({**payload, "search_rank": invalid_rank})

    assert exc_info.value.errors()[0]["type"] == "greater_than_equal"


@pytest.mark.parametrize("malformed_rank", (True, "1"))
@pytest.mark.parametrize(
    ("model_type", "payload"),
    (
        (WebSearchResult, _search_payload()),
        (PreparedWebSource, _prepared_payload()),
        (WebEvidence, _evidence_payload()),
    ),
)
def test_web_source_models_reject_boolean_and_string_search_ranks(
    model_type: type[BaseModel],
    payload: dict[str, object],
    malformed_rank: object,
) -> None:
    with pytest.raises(ValidationError, match="search_rank"):
        model_type.model_validate({**payload, "search_rank": malformed_rank})


@pytest.mark.parametrize("content_origin", tuple(WebContentOrigin))
def test_prepared_web_source_uses_declared_content_origins(
    content_origin: WebContentOrigin,
) -> None:
    source = PreparedWebSource.model_validate(
        {**_prepared_payload(), "content_origin": content_origin}
    )

    assert source.content_origin is content_origin
    assert not hasattr(source, "raw_html")
    assert not hasattr(source, "http_response")


def test_prepared_web_source_rejects_unknown_content_origins() -> None:
    with pytest.raises(ValidationError) as exc_info:
        PreparedWebSource.model_validate(
            {**_prepared_payload(), "content_origin": "RAW_HTML"}
        )

    assert exc_info.value.errors()[0]["type"] == "enum"


@pytest.mark.parametrize("representation", tuple(WebEvidenceRepresentation))
def test_web_evidence_uses_declared_representations(
    representation: WebEvidenceRepresentation,
) -> None:
    evidence = WebEvidence.model_validate(
        {**_evidence_payload(), "representation": representation}
    )

    assert evidence.representation is representation
    assert not hasattr(evidence, "citation_id")
    assert not hasattr(evidence, "score")
    assert not hasattr(evidence, "trust")


def test_web_evidence_rejects_unknown_representations() -> None:
    with pytest.raises(ValidationError) as exc_info:
        WebEvidence.model_validate(
            {**_evidence_payload(), "representation": "RAW_HTML"}
        )

    assert exc_info.value.errors()[0]["type"] == "enum"


@pytest.mark.parametrize(
    "execution_status", (WebExecutionStatus.OK, WebExecutionStatus.DEGRADED)
)
def test_successful_web_outcomes_require_immutable_nonempty_evidence(
    execution_status: WebExecutionStatus,
) -> None:
    first = _web_evidence(search_rank=1)
    second = _web_evidence(search_rank=2)
    result = WebEvidenceResult.model_validate(
        {
            "execution_status": execution_status,
            "evidence": [first, second],
        }
    )

    assert result.execution_status is execution_status
    assert result.evidence == (first, second)
    assert isinstance(result.evidence, tuple)


@pytest.mark.parametrize("unordered_kind", ("set", "frozenset", "mapping", "generator"))
def test_ordered_web_evidence_rejects_unordered_iterables(
    unordered_kind: str,
) -> None:
    values = (_web_evidence(search_rank=1), _web_evidence(search_rank=2))

    with pytest.raises(ValidationError, match="evidence"):
        WebEvidenceResult.model_validate(
            {
                "execution_status": WebExecutionStatus.OK,
                "evidence": _unordered_collection(unordered_kind, values),
            }
        )


def test_ordered_web_evidence_accepts_json_arrays() -> None:
    result = WebEvidenceResult(
        execution_status=WebExecutionStatus.OK,
        evidence=(_web_evidence(),),
    )

    restored = WebEvidenceResult.model_validate_json(result.model_dump_json())

    assert isinstance(restored.evidence, tuple)


@pytest.mark.parametrize(
    "execution_status", (WebExecutionStatus.EMPTY, WebExecutionStatus.UNAVAILABLE)
)
def test_empty_and_unavailable_web_outcomes_remain_distinct(
    execution_status: WebExecutionStatus,
) -> None:
    result = WebEvidenceResult(execution_status=execution_status)

    assert result.execution_status is execution_status
    assert result.evidence == ()
    assert not hasattr(result, "error")
    assert not hasattr(result, "exception")
    assert not hasattr(result, "traceback")


@pytest.mark.parametrize(
    ("execution_status", "evidence"),
    (
        (WebExecutionStatus.OK, ()),
        (WebExecutionStatus.DEGRADED, ()),
        (WebExecutionStatus.EMPTY, (_web_evidence(),)),
        (WebExecutionStatus.UNAVAILABLE, (_web_evidence(),)),
    ),
)
def test_web_result_enforces_status_cardinality(
    execution_status: WebExecutionStatus,
    evidence: tuple[WebEvidence, ...],
) -> None:
    with pytest.raises(ValidationError, match="evidence"):
        WebEvidenceResult(
            execution_status=execution_status,
            evidence=evidence,
        )


@pytest.mark.parametrize(
    ("model_type", "payload"),
    (
        (WebEvidenceRequest, {"original_query": "question"}),
        (WebSearchResult, _search_payload()),
        (PreparedWebSource, _prepared_payload()),
        (WebEvidence, _evidence_payload()),
        (
            WebEvidenceResult,
            {"execution_status": "EMPTY", "evidence": ()},
        ),
    ),
)
def test_web_models_reject_unknown_fields(
    model_type: type[BaseModel], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        model_type.model_validate({**payload, "citation_id": "W1"})

    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize(
    ("model_factory", "field_name"),
    (
        (lambda: WebEvidenceRequest(original_query="question"), "original_query"),
        (lambda: WebSearchResult.model_validate(_search_payload()), "title"),
        (lambda: PreparedWebSource.model_validate(_prepared_payload()), "content"),
        (lambda: WebEvidence.model_validate(_evidence_payload()), "representation"),
        (
            lambda: WebEvidenceResult(
                execution_status=WebExecutionStatus.OK,
                evidence=(_web_evidence(),),
            ),
            "evidence",
        ),
    ),
)
def test_web_models_are_immutable(
    model_factory: Callable[[], BaseModel], field_name: str
) -> None:
    model = model_factory()

    with pytest.raises(ValidationError) as exc_info:
        setattr(model, field_name, "changed")

    assert exc_info.value.errors()[0]["type"] == "frozen_instance"
