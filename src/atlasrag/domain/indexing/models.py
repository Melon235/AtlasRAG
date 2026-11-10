"""Immutable internal indexing DTOs; never persisted in PostgreSQL or State."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from atlasrag._canonical import validate_unicode_scalar_text
from atlasrag.domain.base import (
    FrozenModel,
    StrictReal,
    validate_ordered_collection_input,
    validate_string_enum_input,
)
from atlasrag.domain.enums import RetrievalChunkType, SourceType
from atlasrag.domain.evidence import SourceAnchor
from atlasrag.domain.ingestion.chunks import TableProvenance, TextChildProvenance
from atlasrag.domain.ingestion.elements import normalize_section_path
from atlasrag.domain.ingestion.source import validate_document_id, validate_revision_id

INDEX_TEXT_MAX_BYTES = 65_535
INDEX_FILE_NAME_MAX_BYTES = 1_024


def _chunk_id(value: str) -> str:
    if re.fullmatch(r"chunk_[0-9a-f]{64}", value) is None:
        raise ValueError("chunk_id must be a canonical chunk identity")
    return value


def _nonblank(value: str) -> str:
    value = validate_unicode_scalar_text(value)
    if not value.strip():
        raise ValueError("value must not be blank")
    return value


def _file_name(value: str) -> str:
    value = _nonblank(value)
    if "\x00" in value:
        raise ValueError("file_name must not contain NUL")
    if len(value.encode("utf-8")) > INDEX_FILE_NAME_MAX_BYTES:
        raise ValueError(
            f"file_name must not exceed {INDEX_FILE_NAME_MAX_BYTES} UTF-8 bytes"
        )
    return value


def _strict_schema_version(value: object) -> object:
    if type(value) is not int:
        raise ValueError("schema_version must be an integer")
    return value


def _index_text(value: str) -> str:
    value = validate_unicode_scalar_text(value)
    if "\x00" in value:
        raise ValueError("index text must not contain NUL")
    if not value:
        raise ValueError("index text must not be empty")
    if len(value.encode("utf-8")) > INDEX_TEXT_MAX_BYTES:
        raise ValueError(f"text must not exceed {INDEX_TEXT_MAX_BYTES} UTF-8 bytes")
    return value


_SchemaVersion = Annotated[Literal[1], BeforeValidator(_strict_schema_version)]
_ChunkId = Annotated[StrictStr, AfterValidator(_chunk_id)]
_DocumentId = Annotated[StrictStr, AfterValidator(validate_document_id)]
_RevisionId = Annotated[StrictStr, AfterValidator(validate_revision_id)]
_Nonblank = Annotated[StrictStr, AfterValidator(_nonblank)]
_FileName = Annotated[StrictStr, AfterValidator(_file_name)]
_IndexText = Annotated[StrictStr, AfterValidator(_index_text)]
_RetrievalChunkType = Annotated[
    RetrievalChunkType, BeforeValidator(validate_string_enum_input)
]
_SourceType = Annotated[SourceType, BeforeValidator(validate_string_enum_input)]
_SectionPath = Annotated[
    tuple[StrictStr, ...], BeforeValidator(validate_ordered_collection_input)
]
_DenseVector = Annotated[
    tuple[StrictReal, ...],
    BeforeValidator(validate_ordered_collection_input),
    Field(min_length=1),
]


class RepresentationProfileV1(FrozenModel):
    """Closed baseline; behavior changes require a new representation version."""

    schema_version: _SchemaVersion = 1
    strategy_name: Literal["raw-child-table-slice"] = "raw-child-table-slice"
    representation_version: Literal["representation-v1"] = "representation-v1"


class IndexStructuredMetadataV1(FrozenModel):
    """Only canonical structural facts needed by indexing and future filters."""

    schema_version: _SchemaVersion = 1
    section_path: _SectionPath = ()
    provenance: TextChildProvenance | TableProvenance
    source_anchor: SourceAnchor | None = None

    _canonical_section_path = field_validator("section_path")(normalize_section_path)

    @model_validator(mode="after")
    def _anchor_matches_lineage(self) -> Self:
        if isinstance(self.provenance, TextChildProvenance):
            if self.source_anchor is not None:
                raise ValueError("text Child lineage does not carry a source_anchor")
        elif (
            self.provenance.sheet_name is not None
            and self.source_anchor is not None
            and self.source_anchor.sheet_name is not None
        ):
            if self.source_anchor.sheet_name != self.provenance.sheet_name:
                raise ValueError("source_anchor sheet_name must match table provenance")
        return self


def _validate_metadata_type(
    chunk_type: RetrievalChunkType, metadata: IndexStructuredMetadataV1
) -> None:
    expected = (
        TextChildProvenance
        if chunk_type is RetrievalChunkType.TEXT_CHILD
        else TableProvenance
    )
    if not isinstance(metadata.provenance, expected):
        raise ValueError("chunk_type must match structured_metadata provenance")


class RetrievalRepresentation(FrozenModel):
    """Lossless dense and sparse inputs carrying canonical chunk identity."""

    chunk_id: _ChunkId
    document_id: _DocumentId
    revision_id: _RevisionId
    chunk_type: _RetrievalChunkType
    dense_text: _IndexText
    sparse_text: _IndexText
    structured_metadata: IndexStructuredMetadataV1

    @model_validator(mode="after")
    def _metadata_matches_type(self) -> Self:
        _validate_metadata_type(self.chunk_type, self.structured_metadata)
        return self


class RetrievalRecord(FrozenModel):
    """Transient derived-index row with an ordered finite immutable vector."""

    chunk_id: _ChunkId
    document_id: _DocumentId
    revision_id: _RevisionId
    file_name: _FileName
    parent_id: _ChunkId | None = None
    chunk_type: _RetrievalChunkType
    source_type: _SourceType
    content: _IndexText
    sparse_text: _IndexText
    dense_vector: _DenseVector
    structured_metadata: IndexStructuredMetadataV1

    @model_validator(mode="after")
    def _parent_and_metadata_match_type(self) -> Self:
        _validate_metadata_type(self.chunk_type, self.structured_metadata)
        if (self.chunk_type is RetrievalChunkType.TEXT_CHILD) != (
            self.parent_id is not None
        ):
            raise ValueError("TEXT_CHILD requires parent_id; TABLE forbids parent_id")
        if self.parent_id == self.chunk_id:
            raise ValueError("a chunk cannot be its own parent")
        return self


class EmbeddingModelIdentity(FrozenModel):
    """Resolved model, tokenizer, dimension, and explicit execution device."""

    model_id: _Nonblank
    model_revision: _Nonblank
    tokenizer_id: _Nonblank
    tokenizer_revision: _Nonblank
    dimension: Annotated[StrictInt, Field(ge=1)]
    device: _Nonblank


class PublishResult(FrozenModel):
    """Canonical identities returned after the publication transaction."""

    document_id: _DocumentId
    published_revision_id: _RevisionId
    previous_current_revision_id: _RevisionId | None = None
