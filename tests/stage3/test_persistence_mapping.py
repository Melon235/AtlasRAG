"""Typed Stage 3 canonical-to-persistence mapping contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any, TypeVar, cast

import pytest
from pydantic import BaseModel, ValidationError

from atlasrag.domain.enums import SourceType
from atlasrag.domain.errors import CanonicalDataError
from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.ingestion.chunks import (
    ChunkRecord,
    ChunkType,
    TableProvenance,
    TextChildProvenance,
    TextParentProvenance,
)
from atlasrag.domain.ingestion.document import (
    DocumentContainer,
    DocumentMetadata,
    ParseInfo,
)
from atlasrag.domain.ingestion.elements import (
    CanonicalElement,
    CanonicalElementMetadata,
    TableData,
    render_table_data,
)
from atlasrag.domain.ingestion.enums import (
    ElementType,
    ListKind,
    ParseOutcome,
    ParseReliability,
)
from atlasrag.domain.ingestion.source import (
    CanonicalSource,
    derive_document_id,
    derive_revision_id,
)
from atlasrag.repositories.postgres.records import (
    FailedStage,
    IngestionStatus,
    JsonObject,
    StoredChunk,
    StoredDocument,
)
from atlasrag.services.ingestion.chunking import (
    DEFAULT_CHUNKER_BEHAVIOR_VERSION,
    PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
    ChunkingProfile,
    build_chunks,
)
from atlasrag.services.ingestion.identity import build_element
from atlasrag.services.ingestion.mapping import (
    ChunkPersistenceMetadataV1,
    ChunkStrategyMetadataV1,
    DocumentParseMetadataV1,
    ElementPersistenceMetadataV1,
    ElementStructuredContentV1,
    map_chunk_from_stored,
    map_chunk_to_stored,
    map_chunks_from_stored,
    map_chunks_to_stored,
    map_document_from_stored,
    map_document_parse_metadata,
    map_element_from_stored,
    map_element_to_stored,
    map_elements_from_stored,
    map_elements_to_stored,
)

NOW = datetime(2026, 10, 9, 8, 30, tzinfo=UTC)
DOCUMENT_ID = derive_document_id("guides/reference.xlsx")
CONTENT_HASH = "b" * 64
PIPELINE_FINGERPRINT = "c" * 64
REVISION_ID = derive_revision_id(DOCUMENT_ID, CONTENT_HASH, PIPELINE_FINGERPRINT)
_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _table_data() -> TableData:
    anchor = SourceAnchor(sheet_name="Metrics", cell_range="A1:B3")
    return TableData(
        headers=("Name", "Value"),
        rows=(("Alpha", "1"), ("Beta", "2")),
        caption="Summary",
        sheet_name="Metrics",
        source_anchor=anchor,
    )


def _elements() -> tuple[CanonicalElement, ...]:
    table_data = _table_data()
    return (
        build_element(
            document_id=DOCUMENT_ID,
            revision_id=REVISION_ID,
            element_type=ElementType.HEADING,
            order=0,
            content="Reference",
            section_path=("Reference",),
            source_anchor=SourceAnchor(heading="Reference"),
            metadata=CanonicalElementMetadata(heading_level=1),
        ),
        build_element(
            document_id=DOCUMENT_ID,
            revision_id=REVISION_ID,
            element_type=ElementType.PARAGRAPH,
            order=1,
            content="A canonical paragraph with enough text for child chunks.",
            section_path=("Reference",),
        ),
        build_element(
            document_id=DOCUMENT_ID,
            revision_id=REVISION_ID,
            element_type=ElementType.LIST,
            order=2,
            content="First item",
            section_path=("Reference",),
            metadata=CanonicalElementMetadata(
                list_kind=ListKind.ORDERED,
                list_depth=0,
            ),
        ),
        build_element(
            document_id=DOCUMENT_ID,
            revision_id=REVISION_ID,
            element_type=ElementType.CODE,
            order=3,
            content="print('atlas')",
            section_path=("Reference",),
            metadata=CanonicalElementMetadata(code_language="python"),
        ),
        build_element(
            document_id=DOCUMENT_ID,
            revision_id=REVISION_ID,
            element_type=ElementType.TABLE,
            order=4,
            content=render_table_data(table_data),
            section_path=("Reference", "Metrics"),
            source_anchor=table_data.source_anchor,
            table_data=table_data,
        ),
    )


def _document() -> DocumentContainer:
    return DocumentContainer(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        source=CanonicalSource(
            relative_source_path="guides/reference.xlsx",
            file_name="reference.xlsx",
            source_type=SourceType.XLSX,
        ),
        metadata=DocumentMetadata(
            title="Reference",
            authors=("Atlas Team",),
            language="en",
            sheet_names=("Metrics",),
        ),
        parse_info=ParseInfo(
            outcome=ParseOutcome.DEGRADED,
            reliability=ParseReliability.PARTIAL,
            parser_name="docling",
            parser_version="2.60.0",
            warning_codes=("PARTIAL_STYLE",),
        ),
        elements=_elements(),
    )


def _stored_document(document: DocumentContainer | None = None) -> StoredDocument:
    canonical = document or _document()
    return StoredDocument(
        document_id=canonical.document_id,
        file_name=canonical.source.file_name,
        relative_source_path=canonical.source.relative_source_path,
        source_type=canonical.source.source_type,
        observed_content_hash=CONTENT_HASH,
        building_revision_id=canonical.revision_id,
        building_content_hash=CONTENT_HASH,
        building_pipeline_fingerprint=PIPELINE_FINGERPRINT,
        ingestion_status=IngestionStatus.PARSED,
        parse_metadata=cast(JsonObject, map_document_parse_metadata(canonical)),
        created_at=NOW,
        updated_at=NOW,
    )


def _chunks() -> tuple[ChunkRecord, ...]:
    chunks = build_chunks(
        _document(),
        ChunkingProfile(
            parent_max_chars=200,
            child_max_chars=24,
            table_slice_max_chars=200,
        ),
    )
    assert {chunk.chunk_type for chunk in chunks} == {
        ChunkType.TEXT_PARENT,
        ChunkType.TEXT_CHILD,
        ChunkType.TABLE,
    }
    return chunks


def _payload(record: BaseModel) -> dict[str, Any]:
    return record.model_dump(mode="json")


def _deep_json(depth: int = 1_200) -> object:
    value: object = None
    for _ in range(depth):
        value = {"nested": value}
    return value


def _replace(record: _ModelT, **changes: object) -> _ModelT:
    data = record.model_dump(mode="json")
    data.update(changes)
    return type(record).model_validate(data)


def _replace_json_field(
    record: _ModelT,
    field: str,
    mutate: object,
) -> _ModelT:
    data = record.model_dump(mode="json")
    payload = dict(data[field])
    if callable(mutate):
        mutate(payload)
    else:
        payload = mutate  # type: ignore[assignment]
    data[field] = payload
    return type(record).model_validate(data)


@pytest.mark.parametrize(
    "model,payload",
    [
        (
            DocumentParseMetadataV1,
            {
                "schema_version": 1,
                "parse_info": _document().parse_info,
                "document_metadata": _document().metadata,
            },
        ),
        (
            ElementStructuredContentV1,
            {"schema_version": 1, "table_data": _table_data()},
        ),
        (
            ElementPersistenceMetadataV1,
            {
                "schema_version": 1,
                "canonical_metadata": CanonicalElementMetadata(),
            },
        ),
        (
            ChunkPersistenceMetadataV1,
            {
                "schema_version": 1,
                "order": _chunks()[0].order,
                "retrieval_role": _chunks()[0].retrieval_role,
                "content_hash": _chunks()[0].content_hash,
                "provenance": _chunks()[0].provenance,
                "table_slice": _chunks()[0].table_slice,
            },
        ),
        (
            ChunkStrategyMetadataV1,
            {
                "schema_version": 1,
                "chunking_strategy": _chunks()[0].chunking_strategy,
                "chunker_behavior_version": DEFAULT_CHUNKER_BEHAVIOR_VERSION,
                "profile_fingerprint": PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
            },
        ),
    ],
)
def test_persistence_payloads_are_required_versioned_extra_forbid_and_immutable(
    model: type[_ModelT], payload: dict[str, object]
) -> None:
    value = model.model_validate(payload)
    assert value.model_dump(mode="json")["schema_version"] == 1

    with pytest.raises(ValidationError, match="frozen"):
        setattr(value, "schema_version", 1)
    for required_field in payload:
        with pytest.raises(ValidationError, match="Field required"):
            model.model_validate(
                {key: item for key, item in payload.items() if key != required_field}
            )
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "schema_version": 2})
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "schema_version": True})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate({**payload, "provider_payload": {}})


def test_document_metadata_json_is_typed_serialized_and_round_trips() -> None:
    document = _document()

    payload = map_document_parse_metadata(document)

    assert payload == DocumentParseMetadataV1(
        schema_version=1,
        parse_info=document.parse_info,
        document_metadata=document.metadata,
    ).model_dump(mode="json")
    assert isinstance(payload, dict)
    assert payload["parse_info"]["outcome"] == "DEGRADED"  # type: ignore[index]

    stored_elements = map_elements_to_stored(document.elements, created_at=NOW)
    restored = map_document_from_stored(
        _stored_document(document),
        tuple(reversed(stored_elements)),
        revision_id=document.revision_id,
    )
    assert restored == document
    assert tuple(element.order for element in restored.elements) == tuple(
        range(len(document.elements))
    )


@pytest.mark.parametrize(
    "element", _elements(), ids=lambda item: item.element_type.value
)
def test_every_element_kind_semantically_round_trips(element: CanonicalElement) -> None:
    stored = map_element_to_stored(element, created_at=NOW)

    assert stored.created_at == NOW
    assert _payload(stored)["structured_content"] == ElementStructuredContentV1(
        schema_version=1,
        table_data=element.table_data,
    ).model_dump(mode="json")
    assert _payload(stored)["metadata"] == ElementPersistenceMetadataV1(
        schema_version=1,
        canonical_metadata=element.metadata,
    ).model_dump(mode="json")
    assert map_element_from_stored(stored) == element


def test_mapping_writes_only_plain_json_values_into_storage_columns() -> None:
    stored_elements = map_elements_to_stored(_elements(), created_at=NOW)
    stored_chunks = map_chunks_to_stored(
        _chunks(),
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )

    for stored in (*stored_elements, *stored_chunks):
        dumped = stored.model_dump(mode="json")
        for field in ("structured_content", "metadata", "strategy_metadata"):
            if field in dumped:
                assert isinstance(dumped[field], dict)


def test_reconstructed_elements_are_sorted_by_validated_canonical_order() -> None:
    stored = map_elements_to_stored(_elements(), created_at=NOW)

    assert map_elements_from_stored(tuple(reversed(stored))) == _elements()


def test_sparse_element_orders_semantically_round_trip() -> None:
    sparse_elements = (
        build_element(
            document_id=DOCUMENT_ID,
            revision_id=REVISION_ID,
            element_type=ElementType.PARAGRAPH,
            order=3,
            content="Sparse first element.",
            section_path=("Sparse",),
        ),
        build_element(
            document_id=DOCUMENT_ID,
            revision_id=REVISION_ID,
            element_type=ElementType.CODE,
            order=9,
            content="print('sparse')",
            section_path=("Sparse",),
            metadata=CanonicalElementMetadata(code_language="python"),
        ),
    )
    base = _document()
    document = DocumentContainer(
        document_id=base.document_id,
        revision_id=base.revision_id,
        source=base.source,
        metadata=base.metadata,
        parse_info=base.parse_info,
        elements=sparse_elements,
    )
    stored_elements = map_elements_to_stored(
        tuple(reversed(sparse_elements)),
        created_at=NOW,
    )

    assert map_elements_from_stored(stored_elements) == sparse_elements
    assert (
        map_document_from_stored(
            _stored_document(document),
            stored_elements,
            revision_id=document.revision_id,
        )
        == document
    )


@pytest.mark.parametrize(
    "chunk_type,provenance_type",
    [
        (ChunkType.TEXT_PARENT, TextParentProvenance),
        (ChunkType.TEXT_CHILD, TextChildProvenance),
        (ChunkType.TABLE, TableProvenance),
    ],
)
def test_every_chunk_kind_semantically_round_trips(
    chunk_type: ChunkType,
    provenance_type: type[object],
) -> None:
    chunk = next(item for item in _chunks() if item.chunk_type is chunk_type)

    stored = map_chunk_to_stored(
        chunk,
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )

    assert stored.created_at == NOW
    assert stored.content == chunk.raw_content
    assert isinstance(chunk.provenance, provenance_type)
    assert _payload(stored)["metadata"] == ChunkPersistenceMetadataV1(
        schema_version=1,
        order=chunk.order,
        retrieval_role=chunk.retrieval_role,
        content_hash=chunk.content_hash,
        provenance=chunk.provenance,
        table_slice=chunk.table_slice,
    ).model_dump(mode="json")
    assert _payload(stored)["strategy_metadata"] == ChunkStrategyMetadataV1(
        schema_version=1,
        chunking_strategy=chunk.chunking_strategy,
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
    ).model_dump(mode="json")
    assert map_chunk_from_stored(stored) == chunk


def test_reconstructed_chunks_are_sorted_by_validated_embedded_order() -> None:
    chunks = _chunks()
    stored = map_chunks_to_stored(
        chunks,
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )

    restored = map_chunks_from_stored(tuple(reversed(stored)))

    assert restored == chunks
    assert tuple(chunk.order for chunk in restored) == tuple(range(len(chunks)))


@pytest.mark.parametrize(
    "timestamp",
    [
        NOW.replace(tzinfo=None),
        NOW.astimezone(timezone(timedelta(hours=8))),
    ],
)
def test_write_mapping_requires_an_aware_utc_timestamp(timestamp: datetime) -> None:
    with pytest.raises(ValueError, match="UTC"):
        map_element_to_stored(_elements()[0], created_at=timestamp)
    with pytest.raises(ValueError, match="UTC"):
        map_chunk_to_stored(
            _chunks()[0],
            chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
            profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
            created_at=timestamp,
        )


@pytest.mark.parametrize(
    "field,mutation",
    [
        ("parse_metadata", lambda value: value.__setitem__("schema_version", 2)),
        ("parse_metadata", lambda value: value.pop("schema_version")),
        ("parse_metadata", lambda value: value.__setitem__("extra", True)),
        ("parse_metadata", lambda value: value.pop("parse_info")),
        (
            "parse_metadata",
            lambda value: value["parse_info"].__setitem__("outcome", "UNKNOWN"),
        ),
    ],
)
def test_document_read_fails_closed_for_malformed_typed_payloads(
    field: str, mutation: object
) -> None:
    document = _document()
    stored_document = _replace_json_field(_stored_document(document), field, mutation)
    elements = map_elements_to_stored(document.elements, created_at=NOW)

    with pytest.raises(CanonicalDataError) as captured:
        map_document_from_stored(
            stored_document,
            elements,
            revision_id=document.revision_id,
        )
    assert captured.value.args == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"file_name": "other.xlsx"},
        {"relative_source_path": "other/reference.xlsx"},
        {"source_type": SourceType.DOCX},
        {"building_content_hash": "d" * 64},
        {"building_pipeline_fingerprint": "d" * 64},
    ],
)
def test_document_read_rejects_first_class_or_revision_contradictions(
    changes: dict[str, object],
) -> None:
    document = _document()
    stored_document = _replace(_stored_document(document), **changes)

    with pytest.raises(CanonicalDataError):
        map_document_from_stored(
            stored_document,
            map_elements_to_stored(document.elements, created_at=NOW),
            revision_id=document.revision_id,
        )


def test_document_read_requires_building_owner_hash_to_match_observed_hash() -> None:
    document = _document()
    stored_document = _replace(
        _stored_document(document),
        observed_content_hash="d" * 64,
    )

    with pytest.raises(CanonicalDataError):
        map_document_from_stored(
            stored_document,
            map_elements_to_stored(document.elements, created_at=NOW),
            revision_id=document.revision_id,
        )


def test_document_read_requires_requested_revision_to_match_a_stored_triple() -> None:
    document = _document()
    with pytest.raises(CanonicalDataError):
        map_document_from_stored(
            _stored_document(document),
            map_elements_to_stored(document.elements, created_at=NOW),
            revision_id="rev_" + "f" * 64,
        )


def test_document_metadata_belongs_only_to_building_when_current_and_building_coexist() -> (
    None
):
    document = _document()
    current_hash = "d" * 64
    current_pipeline = "e" * 64
    current_revision = derive_revision_id(
        document.document_id,
        current_hash,
        current_pipeline,
    )
    stored_document = _replace(
        _stored_document(document),
        current_revision_id=current_revision,
        current_content_hash=current_hash,
        current_pipeline_fingerprint=current_pipeline,
    )
    elements = map_elements_to_stored(document.elements, created_at=NOW)

    assert (
        map_document_from_stored(
            stored_document,
            elements,
            revision_id=document.revision_id,
        )
        == document
    )
    with pytest.raises(CanonicalDataError):
        map_document_from_stored(
            stored_document,
            (),
            revision_id=current_revision,
        )


def test_document_metadata_belongs_to_current_after_building_is_cleared() -> None:
    document = _document()
    stored_document = _replace(
        _stored_document(document),
        current_revision_id=document.revision_id,
        current_content_hash=CONTENT_HASH,
        current_pipeline_fingerprint=PIPELINE_FINGERPRINT,
        building_revision_id=None,
        building_content_hash=None,
        building_pipeline_fingerprint=None,
        ingestion_status=IngestionStatus.READY,
    )

    assert (
        map_document_from_stored(
            stored_document,
            map_elements_to_stored(document.elements, created_at=NOW),
            revision_id=document.revision_id,
        )
        == document
    )


def test_current_metadata_remains_readable_when_observed_source_has_drifted() -> None:
    document = _document()
    stored_document = _replace(
        _stored_document(document),
        observed_content_hash="d" * 64,
        current_revision_id=document.revision_id,
        current_content_hash=CONTENT_HASH,
        current_pipeline_fingerprint=PIPELINE_FINGERPRINT,
        building_revision_id=None,
        building_content_hash=None,
        building_pipeline_fingerprint=None,
        ingestion_status=IngestionStatus.READY,
    )

    assert (
        map_document_from_stored(
            stored_document,
            map_elements_to_stored(document.elements, created_at=NOW),
            revision_id=document.revision_id,
        )
        == document
    )


@pytest.mark.parametrize(
    ("status", "failed_stage"),
    [
        (IngestionStatus.RECEIVED, None),
        (IngestionStatus.PARSING, None),
        (IngestionStatus.FAILED, FailedStage.PARSE),
        (IngestionStatus.READY, None),
    ],
)
def test_document_read_fails_closed_when_building_metadata_ownership_is_unproven(
    status: IngestionStatus,
    failed_stage: FailedStage | None,
) -> None:
    document = _document()
    current_hash = "d" * 64
    current_pipeline = "e" * 64
    current_revision = derive_revision_id(
        document.document_id,
        current_hash,
        current_pipeline,
    )
    stored_document = _replace(
        _stored_document(document),
        current_revision_id=current_revision,
        current_content_hash=current_hash,
        current_pipeline_fingerprint=current_pipeline,
        ingestion_status=status,
        failed_stage=failed_stage,
    )

    for selected_revision in (current_revision, document.revision_id):
        with pytest.raises(CanonicalDataError):
            map_document_from_stored(
                stored_document,
                map_elements_to_stored(document.elements, created_at=NOW),
                revision_id=selected_revision,
            )


def test_post_parse_failure_metadata_is_provably_owned_by_building_revision() -> None:
    document = _document()
    stored_document = _replace(
        _stored_document(document),
        ingestion_status=IngestionStatus.FAILED,
        failed_stage=FailedStage.CHUNK,
    )

    assert (
        map_document_from_stored(
            stored_document,
            map_elements_to_stored(document.elements, created_at=NOW),
            revision_id=document.revision_id,
        )
        == document
    )


@pytest.mark.parametrize("field", ["structured_content", "metadata"])
@pytest.mark.parametrize("mutation_kind", ["version", "missing", "extra"])
def test_element_read_rejects_unknown_missing_and_extra_payload_fields(
    field: str, mutation_kind: str
) -> None:
    stored = map_element_to_stored(_elements()[-1], created_at=NOW)

    def mutate(payload: dict[str, object]) -> None:
        if mutation_kind == "version":
            payload["schema_version"] = 7
        elif mutation_kind == "missing":
            payload.pop("schema_version")
        else:
            payload["provider_native"] = {}

    malformed = _replace_json_field(stored, field, mutate)
    with pytest.raises(CanonicalDataError):
        map_element_from_stored(malformed)


@pytest.mark.parametrize(
    "change",
    [
        {"element_type": "UNKNOWN"},
        {"content": "different"},
        {"order_index": 17},
        {"source_anchor": SourceAnchor(sheet_name="Other")},
        {"section_path": ("Other",)},
    ],
)
def test_element_read_rejects_unknown_enum_and_first_class_contradictions(
    change: dict[str, object],
) -> None:
    stored = map_element_to_stored(_elements()[-1], created_at=NOW)
    with pytest.raises(CanonicalDataError):
        map_element_from_stored(_replace(stored, **change))


def test_element_read_rejects_bad_nested_table_data() -> None:
    stored = map_element_to_stored(_elements()[-1], created_at=NOW)

    def mutate(payload: dict[str, object]) -> None:
        table = payload["table_data"]
        assert isinstance(table, dict)
        table["rows"] = [["too", "wide", "row"]]

    malformed = _replace_json_field(stored, "structured_content", mutate)
    with pytest.raises(CanonicalDataError):
        map_element_from_stored(malformed)


def test_element_read_rejects_table_and_non_table_payload_contradictions() -> None:
    table = map_element_to_stored(_elements()[-1], created_at=NOW)
    paragraph = map_element_to_stored(_elements()[1], created_at=NOW)

    table_without_data = _replace_json_field(
        table,
        "structured_content",
        lambda value: value.__setitem__("table_data", None),
    )
    paragraph_with_table = _replace_json_field(
        paragraph,
        "structured_content",
        lambda value: value.__setitem__(
            "table_data", _table_data().model_dump(mode="json")
        ),
    )
    with pytest.raises(CanonicalDataError):
        map_element_from_stored(table_without_data)
    with pytest.raises(CanonicalDataError):
        map_element_from_stored(paragraph_with_table)


@pytest.mark.parametrize("field", ["metadata", "strategy_metadata"])
@pytest.mark.parametrize("mutation_kind", ["version", "missing", "extra"])
def test_chunk_read_rejects_unknown_missing_and_extra_payload_fields(
    field: str, mutation_kind: str
) -> None:
    stored = map_chunk_to_stored(
        _chunks()[0],
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )

    def mutate(payload: dict[str, object]) -> None:
        if mutation_kind == "version":
            payload["schema_version"] = 7
        elif mutation_kind == "missing":
            payload.pop("schema_version")
        else:
            payload["unknown"] = True

    malformed = _replace_json_field(stored, field, mutate)
    with pytest.raises(CanonicalDataError):
        map_chunk_from_stored(malformed)


def test_default_chunker_version_requires_the_pinned_profile() -> None:
    wrong_fingerprint = "d" * 64
    with pytest.raises(ValidationError, match="pinned profile"):
        ChunkStrategyMetadataV1(
            schema_version=1,
            chunking_strategy=_chunks()[0].chunking_strategy,
            chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
            profile_fingerprint=wrong_fingerprint,
        )

    stored = map_chunk_to_stored(
        _chunks()[0],
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )
    malformed = _replace_json_field(
        stored,
        "strategy_metadata",
        lambda payload: payload.__setitem__("profile_fingerprint", wrong_fingerprint),
    )
    with pytest.raises(CanonicalDataError):
        map_chunk_from_stored(malformed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("retrieval_role", "UNKNOWN"),
        ("content_hash", "f" * 64),
        ("order", -1),
    ],
)
def test_chunk_read_rejects_unknown_enum_bad_hash_and_bad_order(
    field: str, value: object
) -> None:
    stored = map_chunk_to_stored(
        _chunks()[0],
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )
    malformed = _replace_json_field(
        stored,
        "metadata",
        lambda payload: payload.__setitem__(field, value),
    )
    with pytest.raises(CanonicalDataError):
        map_chunk_from_stored(malformed)


def test_chunk_read_rejects_bad_nested_ranges() -> None:
    child = next(
        chunk for chunk in _chunks() if chunk.chunk_type is ChunkType.TEXT_CHILD
    )
    stored = map_chunk_to_stored(
        child,
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )

    def mutate(payload: dict[str, object]) -> None:
        provenance = payload["provenance"]
        assert isinstance(provenance, dict)
        provenance["parent_char_end"] = provenance["parent_char_start"]

    with pytest.raises(CanonicalDataError):
        map_chunk_from_stored(_replace_json_field(stored, "metadata", mutate))


@pytest.mark.parametrize(
    "change",
    [
        {"chunk_type": "TABLE"},
        {"content": "different"},
        {"source_anchor": SourceAnchor(heading="Unexpected")},
        {"sheet_name": "Unexpected"},
    ],
)
def test_chunk_read_rejects_first_class_type_parent_content_anchor_and_sheet_changes(
    change: dict[str, object],
) -> None:
    child = next(
        chunk for chunk in _chunks() if chunk.chunk_type is ChunkType.TEXT_CHILD
    )
    stored = map_chunk_to_stored(
        child,
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )
    with pytest.raises(CanonicalDataError):
        map_chunk_from_stored(_replace(stored, **change))


@pytest.mark.parametrize(
    "change",
    [
        {"parent_id": "chunk_" + "f" * 64},
        {"section_path": ("Other",)},
    ],
)
def test_chunk_set_read_rejects_wrong_parent_reference_or_parent_shape(
    change: dict[str, object],
) -> None:
    stored = list(
        map_chunks_to_stored(
            _chunks(),
            chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
            profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
            created_at=NOW,
        )
    )
    child_index = next(
        index
        for index, value in enumerate(stored)
        if value.chunk_type.value == ChunkType.TEXT_CHILD.value
    )
    stored[child_index] = _replace(stored[child_index], **change)

    with pytest.raises(CanonicalDataError):
        map_chunks_from_stored(tuple(stored))


def test_table_chunk_read_rejects_slice_provenance_anchor_and_sheet_contradictions() -> (
    None
):
    table = next(chunk for chunk in _chunks() if chunk.chunk_type is ChunkType.TABLE)
    stored = map_chunk_to_stored(
        table,
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )

    bad_sheet = _replace(stored, sheet_name="Other")
    bad_anchor = _replace(stored, source_anchor=SourceAnchor(sheet_name="Other"))

    def mutate_slice(payload: dict[str, object]) -> None:
        table_slice = payload["table_slice"]
        assert isinstance(table_slice, dict)
        table_slice["row_end"] = 1

    bad_slice = _replace_json_field(stored, "metadata", mutate_slice)

    for malformed in (bad_sheet, bad_anchor, bad_slice):
        with pytest.raises(CanonicalDataError):
            map_chunk_from_stored(malformed)


def test_chunk_read_rejects_table_and_non_table_payload_combinations() -> None:
    parent = next(
        chunk for chunk in _chunks() if chunk.chunk_type is ChunkType.TEXT_PARENT
    )
    table = next(chunk for chunk in _chunks() if chunk.chunk_type is ChunkType.TABLE)
    stored_parent = map_chunk_to_stored(
        parent,
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )
    stored_table = map_chunk_to_stored(
        table,
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )

    table_metadata = _payload(stored_table)["metadata"]
    parent_metadata = _payload(stored_parent)["metadata"]
    with pytest.raises(CanonicalDataError):
        map_chunk_from_stored(_replace(stored_parent, metadata=table_metadata))
    with pytest.raises(CanonicalDataError):
        map_chunk_from_stored(_replace(stored_table, metadata=parent_metadata))


def test_chunk_set_read_rejects_duplicate_embedded_orders() -> None:
    stored = list(
        map_chunks_to_stored(
            _chunks(),
            chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
            profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
            created_at=NOW,
        )
    )
    first_order = _payload(stored[0])["metadata"]["order"]
    stored[1] = _replace_json_field(
        stored[1],
        "metadata",
        lambda payload: payload.__setitem__("order", first_order),
    )

    with pytest.raises(CanonicalDataError):
        map_chunks_from_stored(tuple(stored))


def test_chunk_set_read_rejects_empty_or_gapped_canonical_order() -> None:
    with pytest.raises(CanonicalDataError):
        map_chunks_from_stored(())

    stored = list(
        map_chunks_to_stored(
            _chunks(),
            chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
            profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
            created_at=NOW,
        )
    )
    stored[-1] = _replace_json_field(
        stored[-1],
        "metadata",
        lambda payload: payload.__setitem__("order", len(stored)),
    )
    with pytest.raises(CanonicalDataError):
        map_chunks_from_stored(tuple(stored))


def test_chunk_set_read_rejects_missing_child_coverage_even_if_orders_are_repacked() -> (
    None
):
    stored = list(
        map_chunks_to_stored(
            _chunks(),
            chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
            profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
            created_at=NOW,
        )
    )
    child_indexes = [
        index
        for index, record in enumerate(stored)
        if record.chunk_type.value == ChunkType.TEXT_CHILD.value
    ]
    assert len(child_indexes) >= 2
    stored.pop(child_indexes[0])
    stored = [
        _replace_json_field(
            record,
            "metadata",
            lambda payload, order=order: payload.__setitem__("order", order),
        )
        for order, record in enumerate(stored)
    ]

    with pytest.raises(CanonicalDataError):
        map_chunks_from_stored(tuple(stored))


def test_chunk_set_read_rejects_strategy_identity_disagreement() -> None:
    stored = list(
        map_chunks_to_stored(
            _chunks(),
            chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
            profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
            created_at=NOW,
        )
    )
    stored[1] = _replace_json_field(
        stored[1],
        "strategy_metadata",
        lambda payload: payload.__setitem__("chunker_behavior_version", "chunker-v8"),
    )

    with pytest.raises(CanonicalDataError):
        map_chunks_from_stored(tuple(stored))


def test_reads_revalidate_forged_stored_instances() -> None:
    element = map_element_to_stored(_elements()[0], created_at=NOW)
    element_values = {
        field: getattr(element, field) for field in type(element).model_fields
    }
    forged_element = type(element).model_construct(
        **{**element_values, "order_index": -1}
    )

    chunk = map_chunk_to_stored(
        _chunks()[0],
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )
    chunk_values = {field: getattr(chunk, field) for field in type(chunk).model_fields}
    forged_chunk = type(chunk).model_construct(
        **{**chunk_values, "created_at": NOW.replace(tzinfo=None)}
    )

    with pytest.raises(CanonicalDataError):
        map_element_from_stored(forged_element)
    with pytest.raises(CanonicalDataError):
        map_chunk_from_stored(forged_chunk)


def test_document_read_translates_deep_persisted_json_recursion() -> None:
    document = _document()
    stored = _stored_document(document)
    values = {field: getattr(stored, field) for field in type(stored).model_fields}
    forged = type(stored).model_construct(**{**values, "parse_metadata": _deep_json()})

    with pytest.raises(CanonicalDataError) as captured:
        map_document_from_stored(
            forged,
            map_elements_to_stored(document.elements, created_at=NOW),
            revision_id=document.revision_id,
        )
    assert captured.value.args == ()
    assert captured.value.__cause__ is None


def test_element_read_translates_deep_persisted_json_recursion() -> None:
    stored = map_element_to_stored(_elements()[0], created_at=NOW)
    values = {field: getattr(stored, field) for field in type(stored).model_fields}
    forged = type(stored).model_construct(**{**values, "metadata": _deep_json()})

    with pytest.raises(CanonicalDataError) as captured:
        map_element_from_stored(forged)
    assert captured.value.args == ()
    assert captured.value.__cause__ is None


def test_chunk_read_translates_deep_persisted_json_recursion() -> None:
    stored = map_chunk_to_stored(
        _chunks()[0],
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )
    values = {field: getattr(stored, field) for field in type(stored).model_fields}
    forged = type(stored).model_construct(**{**values, "metadata": _deep_json()})

    with pytest.raises(CanonicalDataError) as captured:
        map_chunk_from_stored(forged)
    assert captured.value.args == ()
    assert captured.value.__cause__ is None


def test_chunk_set_uses_revalidated_records_after_accepting_runtime_json_shape() -> (
    None
):
    chunks = _chunks()
    stored = map_chunks_to_stored(
        chunks,
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )
    runtime_rows = cast(
        tuple[StoredChunk, ...],
        tuple(record.model_dump(mode="json") for record in reversed(stored)),
    )

    assert map_chunks_from_stored(runtime_rows) == chunks


def test_read_mapping_does_not_wrap_unexpected_runtime_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = map_chunk_to_stored(
        _chunks()[0],
        chunker_behavior_version=DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        profile_fingerprint=PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
        created_at=NOW,
    )

    def explode(cls: type[ChunkPersistenceMetadataV1], value: object) -> None:
        raise RuntimeError("unexpected mapper defect")

    monkeypatch.setattr(
        ChunkPersistenceMetadataV1, "model_validate", classmethod(explode)
    )
    with pytest.raises(RuntimeError, match="unexpected mapper defect"):
        map_chunk_from_stored(stored)
