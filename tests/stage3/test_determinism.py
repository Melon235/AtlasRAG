"""Determinism and canonical-output regression tests for chunk construction."""

from __future__ import annotations

from atlasrag.domain.enums import SourceType
from atlasrag.domain.ingestion.chunks import ChunkRecord, ChunkType
from atlasrag.domain.ingestion.document import (
    DocumentContainer,
    DocumentMetadata,
    ParseInfo,
)
from atlasrag.domain.ingestion.elements import CanonicalElement
from atlasrag.domain.ingestion.enums import ElementType, ParseOutcome, ParseReliability
from atlasrag.domain.ingestion.source import CanonicalSource, derive_document_id
from atlasrag.services.ingestion.chunking import ChunkingProfile, build_chunks
from atlasrag.services.ingestion.identity import build_element, chunk_content_hash

DOCUMENT_ID = derive_document_id("docs/deterministic.md")
REVISION_ID = "rev_" + "d" * 64


def element(order: int, content: str, section: str) -> CanonicalElement:
    return build_element(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        element_type=ElementType.PARAGRAPH,
        order=order,
        content=content,
        section_path=(section,),
    )


def document() -> DocumentContainer:
    return DocumentContainer(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        source=CanonicalSource(
            relative_source_path="docs/deterministic.md",
            file_name="deterministic.md",
            source_type=SourceType.MD,
        ),
        metadata=DocumentMetadata(title="Deterministic"),
        parse_info=ParseInfo(
            outcome=ParseOutcome.SUCCESS,
            reliability=ParseReliability.RELIABLE,
            parser_name="native",
            parser_version="1",
        ),
        elements=(
            element(0, "Alpha paragraph with words.", "A"),
            element(1, "Beta paragraph.\nWith another line.", "A"),
            element(2, "Gamma 🙂 paragraph.", "B"),
        ),
    )


def test_equivalent_invocations_produce_byte_identical_chunks_and_ids() -> None:
    parsed = document()
    restored = DocumentContainer.model_validate_json(parsed.model_dump_json())
    profile = ChunkingProfile(parent_max_chars=36, child_max_chars=14)

    first = build_chunks(parsed, profile)
    second = build_chunks(parsed, profile)
    roundtrip_input = build_chunks(restored, profile)

    assert first == second == roundtrip_input
    assert [chunk.model_dump_json() for chunk in first] == [
        chunk.model_dump_json() for chunk in second
    ]
    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]
    assert len({chunk.chunk_id for chunk in first}) == len(first)
    assert [chunk.order for chunk in first] == list(range(len(first)))


def test_every_chunk_hash_matches_its_exact_canonical_raw_content() -> None:
    chunks = build_chunks(
        document(),
        ChunkingProfile(parent_max_chars=30, child_max_chars=11),
    )

    for chunk in chunks:
        assert chunk.content_hash == chunk_content_hash(chunk.raw_content)
        assert ChunkRecord.model_validate_json(chunk.model_dump_json()) == chunk


def test_profile_change_is_deterministic_and_changes_affected_output() -> None:
    parsed = document()
    wider = ChunkingProfile(parent_max_chars=80, child_max_chars=20)
    narrower = ChunkingProfile(parent_max_chars=25, child_max_chars=8)

    wide_first = build_chunks(parsed, wider)
    wide_second = build_chunks(parsed, wider)
    narrow_first = build_chunks(parsed, narrower)
    narrow_second = build_chunks(parsed, narrower)

    assert wide_first == wide_second
    assert narrow_first == narrow_second
    assert wider.profile_fingerprint != narrower.profile_fingerprint
    assert [chunk.chunk_id for chunk in wide_first] != [
        chunk.chunk_id for chunk in narrow_first
    ]
    assert sum(
        chunk.chunk_type is ChunkType.TEXT_CHILD for chunk in narrow_first
    ) > sum(chunk.chunk_type is ChunkType.TEXT_CHILD for chunk in wide_first)
