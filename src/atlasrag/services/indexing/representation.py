"""Pure lossless baseline representation construction with no model or I/O."""

from dataclasses import dataclass

from atlasrag.domain.enums import RetrievalChunkType
from atlasrag.domain.indexing.failures import RepresentationInputLimitError
from atlasrag.domain.indexing.models import (
    INDEX_TEXT_MAX_BYTES,
    IndexStructuredMetadataV1,
    RepresentationProfileV1,
    RetrievalRepresentation,
)
from atlasrag.domain.ingestion.chunks import (
    ChunkRecord,
    ChunkType,
    TableProvenance,
    TextChildProvenance,
    render_table_slice,
)


@dataclass(frozen=True, init=False)
class RepresentationBuilder:
    """Bind the closed profile to PartIConfig.representation_version.

    The caller supplies the configured version and may lower the UTF-8 capacity.
    The default capacity matches the existing derived index text column.
    """

    profile: RepresentationProfileV1
    max_text_bytes: int

    def __init__(
        self,
        profile: RepresentationProfileV1,
        *,
        representation_version: str,
        max_text_bytes: int = INDEX_TEXT_MAX_BYTES,
    ) -> None:
        if not isinstance(profile, RepresentationProfileV1):
            raise TypeError("profile must be a RepresentationProfileV1")
        validated = RepresentationProfileV1.model_validate(profile)
        if (
            not isinstance(representation_version, str)
            or representation_version != validated.representation_version
        ):
            raise ValueError("configured representation_version does not match profile")
        if (
            isinstance(max_text_bytes, bool)
            or not isinstance(max_text_bytes, int)
            or not 1 <= max_text_bytes <= INDEX_TEXT_MAX_BYTES
        ):
            raise ValueError(
                f"max_text_bytes must be an integer in [1, {INDEX_TEXT_MAX_BYTES}]"
            )
        object.__setattr__(self, "profile", validated)
        object.__setattr__(self, "max_text_bytes", max_text_bytes)

    def build(self, chunk: ChunkRecord) -> RetrievalRepresentation:
        """Revalidate a canonical retrievable chunk before emitting its text."""
        if not isinstance(chunk, ChunkRecord):
            raise TypeError("chunk must be a canonical ChunkRecord")
        if set(vars(chunk)) - set(ChunkRecord.model_fields):
            raise ValueError("unknown chunk fields")
        validated = ChunkRecord.model_validate(chunk)
        if validated != chunk:
            raise ValueError("chunk must already be canonical")
        if validated.chunk_type is ChunkType.TEXT_PARENT:
            raise ValueError("TEXT_PARENT is not retrievable")
        assert isinstance(validated.provenance, (TextChildProvenance, TableProvenance))
        text = validated.raw_content
        anchor = None
        if validated.chunk_type is ChunkType.TABLE:
            assert validated.table_slice is not None
            text = render_table_slice(validated.table_slice)
            anchor = validated.table_slice.source_anchor
        actual_bytes = len(text.encode("utf-8"))
        if actual_bytes > self.max_text_bytes:
            raise RepresentationInputLimitError(
                actual_bytes=actual_bytes, max_bytes=self.max_text_bytes
            )
        return RetrievalRepresentation(
            chunk_id=validated.chunk_id,
            document_id=validated.document_id,
            revision_id=validated.revision_id,
            chunk_type=RetrievalChunkType(validated.chunk_type.value),
            dense_text=text,
            sparse_text=text,
            structured_metadata=IndexStructuredMetadataV1(
                section_path=validated.section_path,
                provenance=validated.provenance,
                source_anchor=anchor,
            ),
        )
