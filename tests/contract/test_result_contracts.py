"""Contract tests for final citations, responses, and RAG core results."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import BaseModel, HttpUrl, TypeAdapter, ValidationError

from atlasrag.domain.enums import EvidenceType, RagCompletionStatus, SourceKind
from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.results import (
    FinalCitation,
    FinalLocalCitation,
    FinalResponse,
    FinalWebCitation,
    RagCoreResult,
)


def _local_payload(*, citation_id: object = "L1") -> dict[str, object]:
    return {
        "citation_id": citation_id,
        "source_kind": "LOCAL",
        "file_name": "guide.md",
        "relative_source_path": "docs/guide.md",
        "evidence_type": "TEXT_PARENT",
        "source_anchor": {"heading": "Answer contracts"},
    }


def _web_payload(*, citation_id: object = "W1") -> dict[str, object]:
    return {
        "citation_id": citation_id,
        "source_kind": "WEB",
        "title": "AtlasRAG documentation",
        "url": "https://docs.example.com/atlasrag",
        "domain": "docs.example.com",
    }


def _local_citation(*, citation_id: str = "L1") -> FinalLocalCitation:
    return FinalLocalCitation.model_validate(_local_payload(citation_id=citation_id))


def _web_citation(*, citation_id: str = "W1") -> FinalWebCitation:
    return FinalWebCitation.model_validate(_web_payload(citation_id=citation_id))


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


def test_final_result_models_have_exact_field_sets() -> None:
    assert tuple(FinalLocalCitation.model_fields) == (
        "citation_id",
        "source_kind",
        "file_name",
        "relative_source_path",
        "evidence_type",
        "source_anchor",
    )
    assert tuple(FinalWebCitation.model_fields) == (
        "citation_id",
        "source_kind",
        "title",
        "url",
        "domain",
    )
    assert tuple(FinalResponse.model_fields) == ("answer_text", "citations")
    assert tuple(RagCoreResult.model_fields) == (
        "final_response",
        "completion_status",
    )


def test_final_local_citation_defaults_and_serializes_fixed_source_kind() -> None:
    citation = FinalLocalCitation(
        citation_id="L1",
        file_name="guide.md",
        relative_source_path="docs/guide.md",
        evidence_type=EvidenceType.TEXT_PARENT,
    )

    assert citation.source_kind is SourceKind.LOCAL
    assert citation.source_anchor is None
    assert citation.model_dump(mode="json")["source_kind"] == "LOCAL"


@pytest.mark.parametrize("citation_id", ("L1", "L9", "L10", "L999"))
def test_final_local_citation_accepts_only_canonical_local_ids(
    citation_id: str,
) -> None:
    assert _local_citation(citation_id=citation_id).citation_id == citation_id


@pytest.mark.parametrize(
    "citation_id",
    ("", "L0", "L01", "L001", "l1", " L1", "L1 ", "W1", "L1\n", b"L1"),
)
def test_final_local_citation_rejects_noncanonical_local_ids(
    citation_id: object,
) -> None:
    with pytest.raises(ValidationError, match="citation_id"):
        FinalLocalCitation.model_validate(_local_payload(citation_id=citation_id))


def test_final_local_citation_preserves_strict_display_data() -> None:
    citation = FinalLocalCitation(
        citation_id="L1",
        file_name="  guide.md  ",
        relative_source_path="docs/guide.md",
        evidence_type=EvidenceType.TABLE,
        source_anchor=SourceAnchor(sheet_name="Summary", cell_range="A1:C8"),
    )

    assert citation.file_name == "  guide.md  "
    assert citation.relative_source_path == "docs/guide.md"
    assert citation.evidence_type is EvidenceType.TABLE
    assert citation.source_anchor == SourceAnchor(
        sheet_name="Summary", cell_range="A1:C8"
    )


@pytest.mark.parametrize("field_name", ("file_name", "relative_source_path"))
@pytest.mark.parametrize("invalid_text", ("", " \t ", b"guide.md"))
def test_final_local_citation_rejects_blank_and_byte_display_text(
    field_name: str,
    invalid_text: object,
) -> None:
    payload = _local_payload()
    payload[field_name] = invalid_text

    with pytest.raises(ValidationError, match=field_name):
        FinalLocalCitation.model_validate(payload)


@pytest.mark.parametrize(
    "invalid_path",
    (
        ".",
        "/docs/guide.md",
        "C:/docs/guide.md",
        "C:\\docs\\guide.md",
        "docs\\guide.md",
        "../guide.md",
        "docs/../guide.md",
        "./docs/guide.md",
        "docs/./guide.md",
        "docs//guide.md",
        "docs/guide.md/",
        " docs/guide.md",
        "docs/guide.md ",
        "docs/guide\x00.md",
    ),
)
def test_final_local_citation_enforces_canonical_posix_relative_paths(
    invalid_path: str,
) -> None:
    with pytest.raises(ValidationError, match="relative_source_path"):
        FinalLocalCitation.model_validate(
            {**_local_payload(), "relative_source_path": invalid_path}
        )


@pytest.mark.parametrize("evidence_type", ("TEXT_PARENT", "TABLE"))
def test_final_local_citation_accepts_only_final_evidence_types(
    evidence_type: str,
) -> None:
    citation = FinalLocalCitation.model_validate(
        {**_local_payload(), "evidence_type": evidence_type}
    )

    assert citation.evidence_type is EvidenceType(evidence_type)


@pytest.mark.parametrize("evidence_type", ("TEXT_CHILD", "PDF", b"TABLE"))
def test_final_local_citation_rejects_non_evidence_types(
    evidence_type: object,
) -> None:
    with pytest.raises(ValidationError, match="evidence_type"):
        FinalLocalCitation.model_validate(
            {**_local_payload(), "evidence_type": evidence_type}
        )


def test_final_local_citation_exposes_only_final_provenance_fields() -> None:
    citation = _local_citation()

    for forbidden_field in (
        "context_id",
        "document_id",
        "revision_id",
        "section_path",
        "child_id",
        "chunk_id",
    ):
        assert not hasattr(citation, forbidden_field)


def test_final_web_citation_defaults_and_serializes_fixed_source_kind() -> None:
    citation = FinalWebCitation.model_validate(
        {
            "citation_id": "W1",
            "title": "Documentation",
            "url": "https://docs.example.com/page",
            "domain": "docs.example.com",
        }
    )

    assert citation.source_kind is SourceKind.WEB
    assert citation.model_dump(mode="json")["source_kind"] == "WEB"


@pytest.mark.parametrize("citation_id", ("W1", "W9", "W10", "W999"))
def test_final_web_citation_accepts_only_canonical_web_ids(citation_id: str) -> None:
    assert _web_citation(citation_id=citation_id).citation_id == citation_id


@pytest.mark.parametrize(
    "citation_id",
    ("", "W0", "W01", "W001", "w1", " W1", "W1 ", "L1", "W1\n", b"W1"),
)
def test_final_web_citation_rejects_noncanonical_web_ids(
    citation_id: object,
) -> None:
    with pytest.raises(ValidationError, match="citation_id"):
        FinalWebCitation.model_validate(_web_payload(citation_id=citation_id))


@pytest.mark.parametrize("scheme", ("http", "https"))
def test_final_web_citation_accepts_safe_http_urls_and_matching_domains(
    scheme: str,
) -> None:
    citation = FinalWebCitation.model_validate(
        {
            **_web_payload(),
            "url": f"{scheme}://Example.COM:8443/article",
            "domain": "EXAMPLE.com",
        }
    )

    assert isinstance(citation.url, HttpUrl)
    assert citation.url.host == "example.com"
    assert citation.url.port == 8443
    assert citation.domain == "example.com"


def test_final_web_citation_normalizes_matching_unicode_idn_domains() -> None:
    citation = FinalWebCitation.model_validate(
        {
            **_web_payload(),
            "url": "https://BÜCHER.Example/article",
            "domain": "BÜCHER.Example",
        }
    )

    assert citation.url.host == "xn--bcher-kva.example"
    assert citation.domain == "xn--bcher-kva.example"


@pytest.mark.parametrize(
    "userinfo_url",
    (
        "https://user@example.com/article",
        "https://user:never-serialize-this-secret@example.com/article",
    ),
)
def test_final_web_citation_rejects_url_userinfo(userinfo_url: str) -> None:
    with pytest.raises(ValidationError, match="url"):
        FinalWebCitation.model_validate(
            {**_web_payload(), "url": userinfo_url, "domain": "example.com"}
        )


def test_final_web_citation_rejects_byte_url_input() -> None:
    with pytest.raises(ValidationError, match="url"):
        FinalWebCitation.model_validate(
            {**_web_payload(), "url": b"https://docs.example.com/article"}
        )


@pytest.mark.parametrize(
    ("url", "domain", "error_field"),
    (
        ("https://example.com./article", "example.com", "url"),
        ("https://example.com/article", "example.com.", "domain"),
    ),
)
def test_final_web_citation_rejects_trailing_dot_host_aliases(
    url: str,
    domain: str,
    error_field: str,
) -> None:
    with pytest.raises(ValidationError, match=error_field):
        FinalWebCitation.model_validate(
            {**_web_payload(), "url": url, "domain": domain}
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
def test_final_web_citation_rejects_non_http_urls(invalid_url: str) -> None:
    with pytest.raises(ValidationError, match="url"):
        FinalWebCitation.model_validate({**_web_payload(), "url": invalid_url})


@pytest.mark.parametrize(
    "invalid_domain",
    (
        "other.example.com",
        "https://docs.example.com",
        "docs.example.com/path",
        "docs.example.com:443",
        "user@docs.example.com",
        " docs.example.com ",
        b"docs.example.com",
    ),
)
def test_final_web_citation_requires_matching_host_only_domain(
    invalid_domain: object,
) -> None:
    with pytest.raises(ValidationError, match="domain"):
        FinalWebCitation.model_validate({**_web_payload(), "domain": invalid_domain})


@pytest.mark.parametrize("field_name", ("title", "domain"))
@pytest.mark.parametrize("invalid_text", ("", " \t ", b"text"))
def test_final_web_citation_rejects_blank_and_byte_text(
    field_name: str,
    invalid_text: object,
) -> None:
    payload = _web_payload()
    payload[field_name] = invalid_text

    with pytest.raises(ValidationError, match=field_name):
        FinalWebCitation.model_validate(payload)


def test_final_web_citation_exposes_no_search_or_content_fields() -> None:
    citation = _web_citation()

    assert not hasattr(citation, "search_rank")
    assert not hasattr(citation, "content")
    assert not hasattr(citation, "representation")


@pytest.mark.parametrize(
    ("model_type", "payload", "wrong_source_kind"),
    (
        (FinalLocalCitation, _local_payload(), "WEB"),
        (FinalWebCitation, _web_payload(), "LOCAL"),
    ),
)
def test_final_citation_source_kind_is_fixed_and_rejects_mismatch(
    model_type: type[BaseModel],
    payload: dict[str, object],
    wrong_source_kind: str,
) -> None:
    with pytest.raises(ValidationError, match="source_kind"):
        model_type.model_validate({**payload, "source_kind": wrong_source_kind})

    with pytest.raises(ValidationError, match="source_kind"):
        model_type.model_validate({**payload, "source_kind": b"LOCAL"})


def test_final_citation_variants_reject_other_variant_fields() -> None:
    with pytest.raises(ValidationError) as local_error:
        FinalLocalCitation.model_validate(
            {
                **_local_payload(),
                "title": "Web title",
                "url": "https://example.com",
                "domain": "example.com",
            }
        )
    assert {error["loc"][-1] for error in local_error.value.errors()}.issuperset(
        {"title", "url", "domain"}
    )

    with pytest.raises(ValidationError) as web_error:
        FinalWebCitation.model_validate(
            {
                **_web_payload(),
                "file_name": "guide.md",
                "relative_source_path": "docs/guide.md",
                "evidence_type": "TABLE",
            }
        )
    assert {error["loc"][-1] for error in web_error.value.errors()}.issuperset(
        {"file_name", "relative_source_path", "evidence_type"}
    )


def test_final_citation_discriminator_parses_dicts_and_json() -> None:
    adapter: TypeAdapter[FinalCitation] = TypeAdapter(FinalCitation)

    local = adapter.validate_python(_local_payload())
    web = adapter.validate_json(
        '{"citation_id":"W1","source_kind":"WEB",'
        '"title":"Documentation","url":"https://example.com/page",'
        '"domain":"example.com"}'
    )

    assert isinstance(local, FinalLocalCitation)
    assert isinstance(web, FinalWebCitation)
    assert local.source_kind is SourceKind.LOCAL
    assert web.source_kind is SourceKind.WEB


@pytest.mark.parametrize("source_kind", ("UNKNOWN", "local", b"LOCAL"))
def test_final_citation_discriminator_rejects_unknown_source_kinds(
    source_kind: object,
) -> None:
    with pytest.raises(ValidationError, match="source_kind"):
        TypeAdapter(FinalCitation).validate_python(
            {**_local_payload(), "source_kind": source_kind}
        )


def test_final_response_preserves_nonblank_text_and_citation_order() -> None:
    web = _web_citation()
    local = _local_citation()
    response = FinalResponse(
        answer_text="No frozen citation syntax is assumed.",
        citations=(web, local),
    )

    assert response.answer_text == "No frozen citation syntax is assumed."
    assert response.citations == (web, local)
    assert isinstance(response.citations, tuple)


@pytest.mark.parametrize("blank", ("", " ", "\t\n"))
def test_final_response_rejects_blank_answer_text(blank: str) -> None:
    with pytest.raises(ValidationError, match="answer_text"):
        FinalResponse(answer_text=blank)


def test_final_response_rejects_byte_answer_text() -> None:
    with pytest.raises(ValidationError, match="answer_text"):
        FinalResponse.model_validate({"answer_text": b"answer"})


@pytest.mark.parametrize("unordered_kind", ("set", "frozenset", "mapping", "generator"))
def test_final_response_rejects_unordered_citation_inputs(
    unordered_kind: str,
) -> None:
    citations = _unordered_collection(
        unordered_kind,
        (_local_citation(), _web_citation()),
    )

    with pytest.raises(ValidationError, match="citations"):
        FinalResponse.model_validate({"answer_text": "answer", "citations": citations})


def test_final_response_rejects_duplicate_citation_ids() -> None:
    with pytest.raises(ValidationError, match="citation"):
        FinalResponse(
            answer_text="answer",
            citations=(_local_citation(), _local_citation()),
        )


def test_final_response_enforces_global_cross_variant_citation_uniqueness() -> None:
    local = _local_citation(citation_id="L1")
    forged_cross_kind_duplicate = FinalWebCitation.model_construct(
        citation_id="L1",
        source_kind=SourceKind.WEB,
        title="Documentation",
        url=HttpUrl("https://example.com/page"),
        domain="example.com",
    )

    with pytest.raises(ValidationError, match="citation"):
        FinalResponse(
            answer_text="answer",
            citations=(local, forged_cross_kind_duplicate),
        )


def test_final_response_discriminated_json_roundtrip_uses_tuples() -> None:
    response = FinalResponse(
        answer_text="Grounded answer.",
        citations=(_local_citation(), _web_citation()),
    )

    restored = FinalResponse.model_validate_json(response.model_dump_json())

    assert restored == response
    assert isinstance(restored.citations, tuple)
    assert isinstance(restored.citations[0], FinalLocalCitation)
    assert isinstance(restored.citations[1], FinalWebCitation)


def test_final_response_contains_no_transport_or_runtime_metadata() -> None:
    response = FinalResponse(answer_text="answer")

    for forbidden_field in (
        "trace_id",
        "latency_ms",
        "runtime_mode",
        "session_id",
        "completion_status",
    ):
        assert not hasattr(response, forbidden_field)


@pytest.mark.parametrize("completion_status", tuple(RagCompletionStatus))
def test_rag_core_result_accepts_each_frozen_completion_status(
    completion_status: RagCompletionStatus,
) -> None:
    response = FinalResponse(answer_text="answer")
    result = RagCoreResult(
        final_response=response,
        completion_status=completion_status,
    )

    assert result.final_response is response
    assert result.completion_status is completion_status
    assert not hasattr(result, "cache_eligible")
    assert not hasattr(result, "degraded")


def test_rag_core_result_accepts_legitimate_json_enum_string() -> None:
    result = RagCoreResult.model_validate_json(
        '{"final_response":{"answer_text":"safe answer","citations":[]},'
        '"completion_status":"SAFE_FAILURE"}'
    )

    assert result.completion_status is RagCompletionStatus.SAFE_FAILURE
    assert result.final_response.citations == ()


def test_rag_core_result_rejects_byte_completion_status() -> None:
    with pytest.raises(ValidationError, match="completion_status"):
        RagCoreResult.model_validate(
            {
                "final_response": {"answer_text": "answer"},
                "completion_status": b"SUCCESS",
            }
        )


@pytest.mark.parametrize(
    ("model_type", "payload"),
    (
        (FinalLocalCitation, _local_payload()),
        (FinalWebCitation, _web_payload()),
        (FinalResponse, {"answer_text": "answer"}),
        (
            RagCoreResult,
            {
                "final_response": {"answer_text": "answer"},
                "completion_status": "SUCCESS",
            },
        ),
    ),
)
def test_final_result_models_reject_unknown_fields(
    model_type: type[BaseModel], payload: dict[str, object]
) -> None:
    _assert_extra_field_rejected(model_type, payload)


@pytest.mark.parametrize(
    ("model_factory", "field_name"),
    (
        (_local_citation, "file_name"),
        (_web_citation, "title"),
        (lambda: FinalResponse(answer_text="answer"), "answer_text"),
        (
            lambda: RagCoreResult(
                final_response=FinalResponse(answer_text="answer"),
                completion_status=RagCompletionStatus.SUCCESS,
            ),
            "completion_status",
        ),
    ),
)
def test_final_result_models_are_immutable(
    model_factory: Callable[[], BaseModel], field_name: str
) -> None:
    _assert_frozen(model_factory, field_name)


def test_task3_models_are_importable_from_domain_public_api() -> None:
    from atlasrag.domain import (
        AnswerInput,
        AnswerLocalEvidence,
        AnswerWebEvidence,
        FinalLocalCitation,
        FinalResponse,
        FinalWebCitation,
        GenerationResult,
        OutputReviewResult,
        RagCoreResult,
        WorkingEvidenceBundle,
    )
    from atlasrag.domain import FinalCitation as PublicFinalCitation

    public_models = (
        AnswerLocalEvidence,
        AnswerWebEvidence,
        AnswerInput,
        WorkingEvidenceBundle,
        GenerationResult,
        OutputReviewResult,
        FinalLocalCitation,
        FinalWebCitation,
        FinalResponse,
        RagCoreResult,
    )

    assert tuple(model.__name__ for model in public_models) == (
        "AnswerLocalEvidence",
        "AnswerWebEvidence",
        "AnswerInput",
        "WorkingEvidenceBundle",
        "GenerationResult",
        "OutputReviewResult",
        "FinalLocalCitation",
        "FinalWebCitation",
        "FinalResponse",
        "RagCoreResult",
    )
    assert PublicFinalCitation is FinalCitation
