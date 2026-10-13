"""Explicit typed mappings between Stage 3 canonical and Stage 2 stored records."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import (
    BeforeValidator,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from atlasrag.domain.base import FrozenModel, validate_string_enum_input
from atlasrag.domain.errors import CanonicalDataError
from atlasrag.domain.ingestion.chunks import (
    ChunkingStrategy,
    ChunkProvenance,
    ChunkRecord,
    ChunkType,
    RetrievalRole,
    TableProvenance,
    TableSlice,
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
    normalize_single_line,
)
from atlasrag.domain.ingestion.enums import ParseOutcome
from atlasrag.domain.ingestion.source import (
    derive_revision_id,
    validate_revision_id,
    validate_sha256_hex,
)
from atlasrag.repositories.postgres.records import (
    FailedStage,
    IngestionStatus,
    StoredChunk,
    StoredChunkType,
    StoredDocument,
    StoredElement,
)
from atlasrag.services.ingestion.chunking import (
    DEFAULT_CHUNKER_BEHAVIOR_VERSION,
    PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
)


def _strict_schema_version(value: object) -> object:
    if type(value) is not int or value != 1:
        raise ValueError("schema_version must be the integer 1")
    return value


_SchemaVersion = Annotated[Literal[1], BeforeValidator(_strict_schema_version)]
_RetrievalRole = Annotated[RetrievalRole, BeforeValidator(validate_string_enum_input)]
_ChunkingStrategy = Annotated[
    ChunkingStrategy, BeforeValidator(validate_string_enum_input)
]


class DocumentParseMetadataSlotV1(FrozenModel):
    """Stable parse facts bound to one canonical document revision."""

    revision_id: StrictStr
    parse_info: ParseInfo
    document_metadata: DocumentMetadata

    @field_validator("revision_id")
    @classmethod
    def _canonical_revision_id(cls, value: str) -> str:
        return validate_revision_id(value)

    @model_validator(mode="after")
    def _requires_usable_parse_facts(self) -> Self:
        if self.parse_info.outcome is ParseOutcome.FAILED:
            raise ValueError("parse metadata slot requires usable parse facts")
        return self


class DocumentParseMetadataEnvelopeV1(FrozenModel):
    """Versioned current/building parse facts stored on one document row."""

    schema_version: _SchemaVersion
    current: DocumentParseMetadataSlotV1 | None
    building: DocumentParseMetadataSlotV1 | None


class ElementStructuredContentV1(FrozenModel):
    """Versioned structured canonical element content."""

    schema_version: _SchemaVersion
    table_data: TableData | None


class ElementPersistenceMetadataV1(FrozenModel):
    """Versioned stable metadata for one canonical element."""

    schema_version: _SchemaVersion
    canonical_metadata: CanonicalElementMetadata


class ChunkPersistenceMetadataV1(FrozenModel):
    """Versioned canonical chunk facts stored outside first-class columns."""

    schema_version: _SchemaVersion
    order: Annotated[StrictInt, Field(ge=0)]
    retrieval_role: _RetrievalRole
    content_hash: StrictStr
    provenance: ChunkProvenance
    table_slice: TableSlice | None

    @field_validator("content_hash")
    @classmethod
    def _canonical_content_hash(cls, value: str) -> str:
        return validate_sha256_hex(value, "chunk content hash")

    @model_validator(mode="after")
    def _table_payload_matches_provenance(self) -> Self:
        if isinstance(self.provenance, TableProvenance):
            if self.table_slice is None:
                raise ValueError("table provenance requires table_slice")
            lineage = (
                self.provenance.source_element_id,
                self.provenance.row_start,
                self.provenance.row_end,
                self.provenance.sheet_name,
            )
            slice_lineage = (
                self.table_slice.source_element_id,
                self.table_slice.row_start,
                self.table_slice.row_end,
                self.table_slice.sheet_name,
            )
            if lineage != slice_lineage:
                raise ValueError("table provenance and table_slice must match")
        elif self.table_slice is not None:
            raise ValueError("non-table provenance must not contain table_slice")
        return self


class ChunkStrategyMetadataV1(FrozenModel):
    """Versioned chunk-strategy identity needed for reproducibility."""

    schema_version: _SchemaVersion
    chunking_strategy: _ChunkingStrategy
    chunker_behavior_version: StrictStr
    profile_fingerprint: StrictStr

    @field_validator("chunker_behavior_version")
    @classmethod
    def _canonical_behavior_version(cls, value: str) -> str:
        return normalize_single_line(value)

    @field_validator("profile_fingerprint")
    @classmethod
    def _canonical_profile_fingerprint(cls, value: str) -> str:
        return validate_sha256_hex(value, "chunking profile fingerprint")

    @model_validator(mode="after")
    def _default_behavior_uses_pinned_profile(self) -> Self:
        if (
            self.chunker_behavior_version == DEFAULT_CHUNKER_BEHAVIOR_VERSION
            and self.profile_fingerprint != PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT
        ):
            raise ValueError(
                "default chunker_behavior_version requires its pinned profile"
            )
        return self


def _require_utc_timestamp(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError("created_at must be an aware UTC timestamp")
    return value


def _canonical_data_error() -> CanonicalDataError:
    return CanonicalDataError()


def empty_document_parse_metadata_envelope() -> DocumentParseMetadataEnvelopeV1:
    """Build the explicit empty parse-metadata envelope."""
    return DocumentParseMetadataEnvelopeV1(
        schema_version=1,
        current=None,
        building=None,
    )


def build_document_parse_metadata_slot(
    document: DocumentContainer,
) -> DocumentParseMetadataSlotV1:
    """Bind revalidated canonical parse facts to their revision."""
    canonical = DocumentContainer.model_validate(document)
    return DocumentParseMetadataSlotV1(
        revision_id=canonical.revision_id,
        parse_info=canonical.parse_info,
        document_metadata=canonical.metadata,
    )


def replace_document_parse_metadata_slot(
    envelope: DocumentParseMetadataEnvelopeV1,
    *,
    slot_name: Literal["current", "building"],
    slot: DocumentParseMetadataSlotV1 | None,
) -> DocumentParseMetadataEnvelopeV1:
    """Return a new envelope after replacing exactly one named typed slot."""
    canonical_envelope = DocumentParseMetadataEnvelopeV1.model_validate(envelope)
    canonical_slot = (
        None if slot is None else DocumentParseMetadataSlotV1.model_validate(slot)
    )
    if slot_name == "current":
        return DocumentParseMetadataEnvelopeV1(
            schema_version=1,
            current=canonical_slot,
            building=canonical_envelope.building,
        )
    if slot_name == "building":
        return DocumentParseMetadataEnvelopeV1(
            schema_version=1,
            current=canonical_envelope.current,
            building=canonical_slot,
        )
    raise ValueError("slot_name must be 'current' or 'building'")


def serialize_document_parse_metadata_envelope(
    envelope: DocumentParseMetadataEnvelopeV1,
) -> dict[str, object]:
    """Serialize only the revalidated typed envelope to JSON values."""
    canonical = DocumentParseMetadataEnvelopeV1.model_validate(envelope)
    return canonical.model_dump(mode="json")


def _validate_document_parse_metadata_ownership(
    document: StoredDocument,
    envelope: DocumentParseMetadataEnvelopeV1,
) -> None:
    current_revision = document.current_revision_id
    building_revision = document.building_revision_id
    if (
        current_revision is not None
        and building_revision is not None
        and current_revision == building_revision
    ):
        raise _canonical_data_error()
    for slot, revision_id in (
        (envelope.current, current_revision),
        (envelope.building, building_revision),
    ):
        if slot is not None and (
            revision_id is None or slot.revision_id != revision_id
        ):
            raise _canonical_data_error()
    if current_revision is not None and envelope.current is None:
        raise _canonical_data_error()
    if document.ingestion_status is IngestionStatus.READY and (
        current_revision is None
        or building_revision is not None
        or envelope.building is not None
    ):
        raise _canonical_data_error()

    building_is_pre_parse = document.ingestion_status in {
        IngestionStatus.RECEIVED,
        IngestionStatus.PARSING,
    } or (
        document.ingestion_status is IngestionStatus.FAILED
        and document.failed_stage is FailedStage.PARSE
    )
    building_is_post_parse = (
        document.ingestion_status in _BUILDING_PARSE_METADATA_STATUSES
        or (
            document.ingestion_status is IngestionStatus.FAILED
            and document.failed_stage in _POST_PARSE_FAILED_STAGES
        )
    )
    if building_is_pre_parse and envelope.building is not None:
        raise _canonical_data_error()
    if building_is_post_parse and envelope.building is None:
        raise _canonical_data_error()


def load_document_parse_metadata_envelope(
    document: StoredDocument,
) -> DocumentParseMetadataEnvelopeV1:
    """Revalidate one stored envelope and its revision-slot ownership."""
    try:
        stored = StoredDocument.model_validate(document)
        envelope = DocumentParseMetadataEnvelopeV1.model_validate(stored.parse_metadata)
    except (RecursionError, ValidationError):
        raise _canonical_data_error() from None
    _validate_document_parse_metadata_ownership(stored, envelope)
    return envelope


def map_element_to_stored(
    element: CanonicalElement,
    *,
    created_at: datetime,
) -> StoredElement:
    """Map one revalidated canonical element to the frozen Stage 2 record."""
    canonical = CanonicalElement.model_validate(element)
    timestamp = _require_utc_timestamp(created_at)
    structured_content = ElementStructuredContentV1(
        schema_version=1,
        table_data=canonical.table_data,
    )
    metadata = ElementPersistenceMetadataV1(
        schema_version=1,
        canonical_metadata=canonical.metadata,
    )
    return StoredElement(
        element_id=canonical.element_id,
        document_id=canonical.document_id,
        revision_id=canonical.revision_id,
        element_type=canonical.element_type.value,
        order_index=canonical.order,
        content=canonical.content,
        section_path=canonical.section_path,
        source_anchor=canonical.source_anchor,
        structured_content=structured_content.model_dump(mode="json"),
        metadata=metadata.model_dump(mode="json"),
        created_at=timestamp,
    )


def map_elements_to_stored(
    elements: Iterable[CanonicalElement],
    *,
    created_at: datetime,
) -> tuple[StoredElement, ...]:
    """Map one canonical element collection without changing its order."""
    timestamp = _require_utc_timestamp(created_at)
    return tuple(
        map_element_to_stored(element, created_at=timestamp) for element in elements
    )


def map_element_from_stored(element: StoredElement) -> CanonicalElement:
    """Revalidate one stored element and reconstruct its canonical meaning."""
    try:
        stored = StoredElement.model_validate(element)
        structured = ElementStructuredContentV1.model_validate(
            stored.structured_content
        )
        metadata = ElementPersistenceMetadataV1.model_validate(stored.metadata)
        return CanonicalElement.model_validate(
            {
                "element_id": stored.element_id,
                "document_id": stored.document_id,
                "revision_id": stored.revision_id,
                "element_type": stored.element_type,
                "order": stored.order_index,
                "content": stored.content,
                "section_path": stored.section_path,
                "source_anchor": stored.source_anchor,
                "metadata": metadata.canonical_metadata,
                "table_data": structured.table_data,
            }
        )
    except (RecursionError, ValidationError):
        raise _canonical_data_error() from None


def map_elements_from_stored(
    elements: Iterable[StoredElement],
) -> tuple[CanonicalElement, ...]:
    """Reconstruct one validated canonical element set in source order."""
    canonical = tuple(map(map_element_from_stored, elements))
    ordered = tuple(sorted(canonical, key=lambda item: item.order))
    orders = tuple(element.order for element in ordered)
    if len(set(orders)) != len(orders):
        raise _canonical_data_error()
    if len({element.element_id for element in ordered}) != len(ordered):
        raise _canonical_data_error()
    scopes = {(element.document_id, element.revision_id) for element in ordered}
    if len(scopes) > 1:
        raise _canonical_data_error()
    return ordered


_BUILDING_PARSE_METADATA_STATUSES = frozenset(
    {
        IngestionStatus.PARSED,
        IngestionStatus.CHUNKING,
        IngestionStatus.CHUNKED,
        IngestionStatus.INDEXING,
    }
)
_POST_PARSE_FAILED_STAGES = frozenset(
    {
        FailedStage.CHUNK,
        FailedStage.REPRESENTATION,
        FailedStage.EMBEDDING,
        FailedStage.INDEX_WRITE,
        FailedStage.INDEX_VERIFY,
    }
)


def _parse_metadata_owner(
    document: StoredDocument,
) -> tuple[Literal["current", "building"], str, str, str] | None:
    """Resolve the only lifecycle state that proves parse_metadata ownership."""
    building_is_owner = document.ingestion_status in _BUILDING_PARSE_METADATA_STATUSES
    if (
        document.ingestion_status is IngestionStatus.FAILED
        and document.failed_stage in _POST_PARSE_FAILED_STAGES
    ):
        building_is_owner = True

    if building_is_owner:
        if (
            document.building_revision_id is None
            or document.building_content_hash is None
            or document.building_pipeline_fingerprint is None
        ):
            return None
        return (
            "building",
            document.building_revision_id,
            document.building_content_hash,
            document.building_pipeline_fingerprint,
        )

    if (
        document.building_revision_id is not None
        or document.ingestion_status is not IngestionStatus.READY
        or document.current_revision_id is None
        or document.current_content_hash is None
        or document.current_pipeline_fingerprint is None
    ):
        return None
    return (
        "current",
        document.current_revision_id,
        document.current_content_hash,
        document.current_pipeline_fingerprint,
    )


def map_document_from_stored(
    document: StoredDocument,
    elements: Iterable[StoredElement],
    *,
    revision_id: str,
) -> DocumentContainer:
    """Reconstruct the explicitly selected, provably-owned canonical revision."""
    try:
        stored = StoredDocument.model_validate(document)
    except (RecursionError, ValidationError):
        raise _canonical_data_error() from None
    parse_metadata = load_document_parse_metadata_envelope(stored)

    owner = _parse_metadata_owner(stored)
    if owner is None or owner[1] != revision_id:
        raise _canonical_data_error()
    owner_name, owner_revision, owner_content_hash, owner_pipeline_fingerprint = owner
    selected_metadata = (
        parse_metadata.current if owner_name == "current" else parse_metadata.building
    )
    if selected_metadata is None or selected_metadata.revision_id != owner_revision:
        raise _canonical_data_error()
    if owner_name == "building" and owner_content_hash != stored.observed_content_hash:
        raise _canonical_data_error()
    try:
        expected_revision = derive_revision_id(
            stored.document_id,
            owner_content_hash,
            owner_pipeline_fingerprint,
        )
    except ValueError:
        raise _canonical_data_error() from None
    if owner_revision != expected_revision:
        raise _canonical_data_error()

    canonical_elements = map_elements_from_stored(elements)
    try:
        return DocumentContainer.model_validate(
            {
                "schema_version": 1,
                "document_id": stored.document_id,
                "revision_id": revision_id,
                "source": {
                    "relative_source_path": stored.relative_source_path,
                    "file_name": stored.file_name,
                    "source_type": stored.source_type.value,
                },
                "metadata": selected_metadata.document_metadata,
                "parse_info": selected_metadata.parse_info,
                "elements": canonical_elements,
            }
        )
    except (RecursionError, ValidationError):
        raise _canonical_data_error() from None


def map_chunk_to_stored(
    chunk: ChunkRecord,
    *,
    chunker_behavior_version: str,
    profile_fingerprint: str,
    created_at: datetime,
) -> StoredChunk:
    """Map one revalidated canonical chunk to the frozen Stage 2 record."""
    canonical = ChunkRecord.model_validate(chunk)
    timestamp = _require_utc_timestamp(created_at)
    metadata = ChunkPersistenceMetadataV1(
        schema_version=1,
        order=canonical.order,
        retrieval_role=canonical.retrieval_role,
        content_hash=canonical.content_hash,
        provenance=canonical.provenance,
        table_slice=canonical.table_slice,
    )
    strategy_metadata = ChunkStrategyMetadataV1(
        schema_version=1,
        chunking_strategy=canonical.chunking_strategy,
        chunker_behavior_version=chunker_behavior_version,
        profile_fingerprint=profile_fingerprint,
    )
    source_anchor = (
        None if canonical.table_slice is None else canonical.table_slice.source_anchor
    )
    sheet_name = (
        None if canonical.table_slice is None else canonical.table_slice.sheet_name
    )
    return StoredChunk(
        chunk_id=canonical.chunk_id,
        document_id=canonical.document_id,
        revision_id=canonical.revision_id,
        chunk_type=StoredChunkType(canonical.chunk_type.value),
        parent_id=canonical.parent_id,
        content=canonical.raw_content,
        section_path=canonical.section_path,
        source_anchor=source_anchor,
        sheet_name=sheet_name,
        metadata=metadata.model_dump(mode="json"),
        strategy_metadata=strategy_metadata.model_dump(mode="json"),
        created_at=timestamp,
    )


def map_chunks_to_stored(
    chunks: Iterable[ChunkRecord],
    *,
    chunker_behavior_version: str,
    profile_fingerprint: str,
    created_at: datetime,
) -> tuple[StoredChunk, ...]:
    """Map one canonical chunk collection without changing its order."""
    timestamp = _require_utc_timestamp(created_at)
    return tuple(
        map_chunk_to_stored(
            chunk,
            chunker_behavior_version=chunker_behavior_version,
            profile_fingerprint=profile_fingerprint,
            created_at=timestamp,
        )
        for chunk in chunks
    )


def map_chunk_from_stored(chunk: StoredChunk) -> ChunkRecord:
    """Revalidate one stored chunk and reconstruct its canonical meaning."""
    try:
        stored = StoredChunk.model_validate(chunk)
        metadata = ChunkPersistenceMetadataV1.model_validate(stored.metadata)
        strategy_metadata = ChunkStrategyMetadataV1.model_validate(
            stored.strategy_metadata
        )
        canonical = ChunkRecord.model_validate(
            {
                "chunk_id": stored.chunk_id,
                "document_id": stored.document_id,
                "revision_id": stored.revision_id,
                "chunk_type": stored.chunk_type.value,
                "retrieval_role": metadata.retrieval_role,
                "chunking_strategy": strategy_metadata.chunking_strategy,
                "raw_content": stored.content,
                "content_hash": metadata.content_hash,
                "parent_id": stored.parent_id,
                "order": metadata.order,
                "section_path": stored.section_path,
                "provenance": metadata.provenance,
                "table_slice": metadata.table_slice,
            }
        )
    except (RecursionError, ValidationError):
        raise _canonical_data_error() from None

    expected_source_anchor = (
        None if canonical.table_slice is None else canonical.table_slice.source_anchor
    )
    expected_sheet_name = (
        None if canonical.table_slice is None else canonical.table_slice.sheet_name
    )
    if (
        stored.source_anchor != expected_source_anchor
        or stored.sheet_name != expected_sheet_name
    ):
        raise _canonical_data_error()
    return canonical


_ParentSpanIndex = dict[
    tuple[str, int],
    tuple[tuple[int, ...], tuple[int, ...]],
]


def _parent_span_index(provenance: TextParentProvenance) -> _ParentSpanIndex:
    mutable: dict[tuple[str, int], tuple[list[int], list[int]]] = {}
    for span in provenance.source_spans:
        starts, ends = mutable.setdefault(
            (span.element_id, span.element_order),
            ([], []),
        )
        starts.append(span.char_start)
        ends.append(span.char_end)
    return {
        identity: (tuple(starts), tuple(ends))
        for identity, (starts, ends) in mutable.items()
    }


def map_chunks_from_stored(
    chunks: Iterable[StoredChunk],
) -> tuple[ChunkRecord, ...]:
    """Reconstruct chunks in validated embedded canonical order."""
    try:
        stored_chunks = tuple(StoredChunk.model_validate(chunk) for chunk in chunks)
    except (RecursionError, ValidationError):
        raise _canonical_data_error() from None
    canonical = tuple(map(map_chunk_from_stored, stored_chunks))
    if not canonical:
        raise _canonical_data_error()
    orders = tuple(chunk.order for chunk in canonical)
    if len(set(orders)) != len(orders):
        raise _canonical_data_error()
    ordered = tuple(sorted(canonical, key=lambda item: item.order))
    if tuple(chunk.order for chunk in ordered) != tuple(range(len(ordered))):
        raise _canonical_data_error()
    if len({chunk.chunk_id for chunk in ordered}) != len(ordered):
        raise _canonical_data_error()
    scopes = {(chunk.document_id, chunk.revision_id) for chunk in ordered}
    if len(scopes) != 1:
        raise _canonical_data_error()
    try:
        strategy_identities = {
            (
                strategy.chunker_behavior_version,
                strategy.profile_fingerprint,
            )
            for stored in stored_chunks
            for strategy in (
                ChunkStrategyMetadataV1.model_validate(stored.strategy_metadata),
            )
        }
    except (RecursionError, ValidationError):
        raise _canonical_data_error() from None
    if len(strategy_identities) != 1:
        raise _canonical_data_error()

    active_parent: ChunkRecord | None = None
    active_span_index: _ParentSpanIndex = {}
    covered_parent_end = 0
    for chunk in ordered:
        if chunk.chunk_type is ChunkType.TEXT_PARENT:
            if active_parent is not None and covered_parent_end != len(
                active_parent.raw_content
            ):
                raise _canonical_data_error()
            if not isinstance(chunk.provenance, TextParentProvenance):
                raise _canonical_data_error()
            active_parent = chunk
            active_span_index = _parent_span_index(chunk.provenance)
            covered_parent_end = 0
            continue
        if chunk.chunk_type is ChunkType.TABLE:
            if active_parent is not None and covered_parent_end != len(
                active_parent.raw_content
            ):
                raise _canonical_data_error()
            active_parent = None
            active_span_index = {}
            covered_parent_end = 0
            continue
        if (
            active_parent is None
            or chunk.parent_id != active_parent.chunk_id
            or active_parent.document_id != chunk.document_id
            or active_parent.revision_id != chunk.revision_id
            or active_parent.section_path != chunk.section_path
            or not isinstance(chunk.provenance, TextChildProvenance)
        ):
            raise _canonical_data_error()
        child_provenance = chunk.provenance
        if (
            child_provenance.parent_char_start != covered_parent_end
            or child_provenance.parent_char_end > len(active_parent.raw_content)
            or active_parent.raw_content[
                child_provenance.parent_char_start : child_provenance.parent_char_end
            ]
            != chunk.raw_content
        ):
            raise _canonical_data_error()
        covered_parent_end = child_provenance.parent_char_end
        for child_span in child_provenance.source_spans:
            intervals = active_span_index.get(
                (child_span.element_id, child_span.element_order)
            )
            if intervals is None:
                raise _canonical_data_error()
            starts, ends = intervals
            interval_index = bisect_right(starts, child_span.char_start) - 1
            if interval_index < 0 or child_span.char_end > ends[interval_index]:
                raise _canonical_data_error()
    if active_parent is not None and covered_parent_end != len(
        active_parent.raw_content
    ):
        raise _canonical_data_error()
    return ordered
