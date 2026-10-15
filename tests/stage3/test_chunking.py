"""Lossless structural Parent and recursive Child chunking tests."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from itertools import product
from typing import SupportsIndex

import pytest
from pydantic import ValidationError

from atlasrag.domain.enums import SourceType
from atlasrag.domain.ingestion.chunks import (
    ChunkingStrategy,
    ChunkType,
    TextChildProvenance,
    TextParentProvenance,
)
from atlasrag.domain.ingestion.document import (
    DocumentContainer,
    DocumentMetadata,
    ParseInfo,
)
from atlasrag.domain.ingestion.elements import CanonicalElement
from atlasrag.domain.ingestion.enums import ElementType, ParseOutcome, ParseReliability
from atlasrag.domain.ingestion.source import CanonicalSource, derive_document_id
from atlasrag.services.ingestion import chunking as chunking_module
from atlasrag.services.ingestion.analysis import ChunkRouter, DocumentAnalyzer, TextRun
from atlasrag.services.ingestion.chunking import (
    DEFAULT_CHUNKER_BEHAVIOR_VERSION,
    PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
    ChunkingProfile,
    build_chunks,
    require_chunker_behavior_version_change,
)
from atlasrag.services.ingestion.identity import build_element, chunk_content_hash

DOCUMENT_ID = derive_document_id("docs/guide.md")
REVISION_ID = "rev_" + "a" * 64
_V7_SENTENCE_BOUNDARY = re.compile(
    r"(?:[.!?]+[\"'”’\)\]]*(?=\s)|[。！？]+[\"'”’\)\]]*)"
)
_V7_SENTENCE_CLOSERS = frozenset("\"'”’)]")


def _v7_sentence_endpoint_reference(text: str) -> list[int]:
    latest_starts = [-1] * (len(text) + 1)
    for match in _V7_SENTENCE_BOUNDARY.finditer(text):
        punctuation_end = match.end()
        while (
            punctuation_end > match.start()
            and text[punctuation_end - 1] in _V7_SENTENCE_CLOSERS
        ):
            punctuation_end -= 1
        latest_starts[match.end()] = punctuation_end - 1

    for end in range(1, len(text) + 1):
        if latest_starts[end] < 0 and text[end - 1].isspace():
            latest_starts[end] = latest_starts[end - 1]
    return latest_starts


class _CountingText(str):
    indexed_reads = 0

    def __getitem__(self, index: SupportsIndex | slice, /) -> str:
        if not isinstance(index, slice):
            self.indexed_reads += 1
        return super().__getitem__(index)


class _ForbiddenSentenceBoundary:
    def finditer(self, _text: str) -> tuple[()]:
        raise AssertionError("sentence endpoint indexing must not use regex scanning")


def element(
    order: int,
    content: str,
    *,
    section_path: tuple[str, ...] = ("Overview",),
) -> CanonicalElement:
    return build_element(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        element_type=ElementType.PARAGRAPH,
        order=order,
        content=content,
        section_path=section_path,
    )


def document(elements: tuple[CanonicalElement, ...]) -> DocumentContainer:
    return DocumentContainer(
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
        source=CanonicalSource(
            relative_source_path="docs/guide.md",
            file_name="guide.md",
            source_type=SourceType.MD,
        ),
        metadata=DocumentMetadata(title="Guide"),
        parse_info=ParseInfo(
            outcome=ParseOutcome.SUCCESS,
            reliability=ParseReliability.RELIABLE,
            parser_name="test-parser",
            parser_version="1",
        ),
        elements=elements,
    )


def small_profile(**overrides: object) -> ChunkingProfile:
    values: dict[str, object] = {
        "parent_max_chars": 28,
        "child_max_chars": 12,
        "table_slice_max_chars": 80,
    }
    values.update(overrides)
    return ChunkingProfile.model_validate(values)


def test_analyzer_groups_only_contiguous_same_section_text() -> None:
    parsed = document(
        (
            element(0, "First", section_path=("A",)),
            element(1, "Second", section_path=("A",)),
            element(2, "Third", section_path=("B",)),
            element(3, "Fourth", section_path=("A",)),
        )
    )

    analysis = DocumentAnalyzer().analyze(parsed)

    assert [type(item) for item in analysis.items] == [TextRun, TextRun, TextRun]
    assert [
        tuple(item.order for item in run.elements)
        for run in analysis.items
        if isinstance(run, TextRun)
    ] == [
        (0, 1),
        (2,),
        (3,),
    ]
    router = ChunkRouter()
    assert router.parent_strategy is ChunkingStrategy.STRUCTURE
    assert router.child_strategy is ChunkingStrategy.RECURSIVE
    assert router.table_strategy is ChunkingStrategy.TABLE


def test_same_section_elements_group_into_bounded_parent_then_ordered_children() -> (
    None
):
    first = element(0, "Alpha text.")
    second = element(1, "Beta text.")
    chunks = build_chunks(document((first, second)), small_profile())

    parents = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_PARENT]
    assert len(parents) == 1
    parent = parents[0]
    assert parent.raw_content == "Alpha text.\n\nBeta text."
    assert isinstance(parent.provenance, TextParentProvenance)
    assert [
        (span.element_order, span.char_start, span.char_end)
        for span in parent.provenance.source_spans
    ] == [
        (0, 0, len(first.content)),
        (1, 0, len(second.content)),
    ]

    parent_index = chunks.index(parent)
    children = []
    for chunk in chunks[parent_index + 1 :]:
        if chunk.chunk_type is ChunkType.TEXT_PARENT:
            break
        children.append(chunk)
    assert children
    assert all(child.chunk_type is ChunkType.TEXT_CHILD for child in children)
    assert all(child.parent_id == parent.chunk_id for child in children)
    assert "".join(child.raw_content for child in children) == parent.raw_content
    assert [
        (child.provenance.parent_char_start, child.provenance.parent_char_end)
        for child in children
        if isinstance(child.provenance, TextChildProvenance)
    ] == _contiguous_ranges(len(parent.raw_content), children)


def _contiguous_ranges(total: int, children: Sequence[object]) -> list[tuple[int, int]]:
    expected: list[tuple[int, int]] = []
    start = 0
    for child in children:
        raw_content = getattr(child, "raw_content")
        end = start + len(raw_content)
        expected.append((start, end))
        start = end
    assert start == total
    return expected


def test_oversized_element_splits_into_atoms_with_complete_source_coverage() -> None:
    source = element(
        0,
        "First paragraph.\n\nSecond line has words.\nThird line is the tail.",
    )
    profile = small_profile(parent_max_chars=18, child_max_chars=9)

    chunks = build_chunks(document((source,)), profile)
    parents = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_PARENT]

    assert len(parents) > 1
    assert all(
        len(parent.raw_content) <= profile.parent_max_chars for parent in parents
    )
    spans = [
        span
        for parent in parents
        if isinstance(parent.provenance, TextParentProvenance)
        for span in parent.provenance.source_spans
    ]
    assert spans[0].char_start == 0
    assert spans[-1].char_end == len(source.content)
    assert all(
        left.char_end == right.char_start for left, right in zip(spans, spans[1:])
    )
    assert (
        "".join(source.content[span.char_start : span.char_end] for span in spans)
        == source.content
    )


def test_recursive_splitter_prefers_paragraph_then_newline_boundaries() -> None:
    source = element(0, "aaaa\n\nbbbbb\ncccc dddd eeee")

    chunks = build_chunks(
        document((source,)),
        small_profile(parent_max_chars=100, child_max_chars=12),
    )
    children = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD]

    assert children[0].raw_content == "aaaa\n\n"
    assert children[1].raw_content.endswith("\n")
    assert "".join(child.raw_content for child in children) == source.content


def test_recursive_splitter_prefers_sentence_before_later_whitespace() -> None:
    source = element(0, "One. two three")

    chunks = build_chunks(
        document((source,)),
        small_profile(parent_max_chars=100, child_max_chars=12),
    )
    children = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD]

    assert children[0].raw_content == "One. "
    assert "".join(child.raw_content for child in children) == source.content


def test_recursive_splitter_handles_unspaced_cjk_without_splitting_decimals() -> None:
    cjk_source = element(0, "甲乙。丙丁。")
    decimal_source = element(0, "3.14 tail")
    profile = small_profile(parent_max_chars=100, child_max_chars=5)

    cjk_children = [
        chunk
        for chunk in build_chunks(document((cjk_source,)), profile)
        if chunk.chunk_type is ChunkType.TEXT_CHILD
    ]
    decimal_children = [
        chunk
        for chunk in build_chunks(document((decimal_source,)), profile)
        if chunk.chunk_type is ChunkType.TEXT_CHILD
    ]

    assert [child.raw_content for child in cjk_children] == ["甲乙。", "丙丁。"]
    assert decimal_children[0].raw_content == "3.14 "
    assert (
        "".join(child.raw_content for child in decimal_children)
        == decimal_source.content
    )


def test_decimal_at_split_window_end_prefers_earlier_whitespace() -> None:
    source = element(0, "a 3.14 tail")

    chunks = build_chunks(
        document((source,)),
        small_profile(parent_max_chars=100, child_max_chars=4),
    )
    children = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD]

    assert [child.raw_content for child in children[:2]] == ["a ", "3.14"]
    assert "".join(child.raw_content for child in children) == source.content


def test_sentence_at_split_window_end_sees_real_following_whitespace() -> None:
    source = element(0, "a bc. tail")

    chunks = build_chunks(
        document((source,)),
        small_profile(parent_max_chars=100, child_max_chars=5),
    )
    children = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD]

    assert children[0].raw_content == "a bc."
    assert "".join(child.raw_content for child in children) == source.content


def test_long_punctuation_only_canonical_text_avoids_regex_sentence_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chunking_module,
        "_SENTENCE_BOUNDARY",
        _ForbiddenSentenceBoundary(),
        raising=False,
    )
    source = element(0, "!" * 20_000)

    chunks = build_chunks(
        document((source,)),
        small_profile(parent_max_chars=20_000, child_max_chars=1_000),
    )
    parent = next(
        chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_PARENT
    )
    children = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD]

    assert parent.raw_content == source.content
    assert "".join(child.raw_content for child in children) == source.content


def test_sentence_endpoint_indexing_reads_long_punctuation_text_linearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chunking_module,
        "_SENTENCE_BOUNDARY",
        _ForbiddenSentenceBoundary(),
        raising=False,
    )
    text = _CountingText("!" * 20_000)

    latest_starts = chunking_module._sentence_endpoint_latest_starts(text)

    assert latest_starts == [-1] * (len(text) + 1)
    assert text.indexed_reads <= 3 * len(text) + 1


def test_sentence_endpoint_indexing_matches_v7_semantics_exhaustively() -> None:
    alphabet = (
        "a",
        "1",
        ".",
        "!",
        "?",
        "。",
        "！",
        "？",
        '"',
        "'",
        "”",
        "’",
        ")",
        "]",
        " ",
        "\n",
    )

    for length in range(5):
        for characters in product(alphabet, repeat=length):
            text = "".join(characters)
            assert chunking_module._sentence_endpoint_latest_starts(
                text
            ) == _v7_sentence_endpoint_reference(text), repr(text)


def test_recursive_splitter_prefers_whitespace_before_scalar_fallback() -> None:
    source = element(0, "alpha betaXYZ")

    chunks = build_chunks(
        document((source,)),
        small_profile(parent_max_chars=100, child_max_chars=8),
    )
    children = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD]

    assert children[0].raw_content == "alpha "
    assert "".join(child.raw_content for child in children) == source.content


def test_public_chunker_uses_earlier_feasible_whitespace_before_scalar() -> None:
    source = element(0, "a aa  ")

    chunks = build_chunks(
        document((source,)),
        small_profile(parent_max_chars=100, child_max_chars=5),
    )
    children = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD]

    assert [child.raw_content for child in children] == ["a ", "aa  "]
    assert "".join(child.raw_content for child in children) == source.content


def test_public_chunker_skips_infeasible_newline_for_feasible_whitespace() -> None:
    source = element(0, "\na aa")

    chunks = build_chunks(
        document((source,)),
        small_profile(parent_max_chars=100, child_max_chars=4),
    )
    children = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD]

    assert [child.raw_content for child in children] == ["\na ", "aa"]
    assert "".join(child.raw_content for child in children) == source.content


@pytest.mark.parametrize(
    ("text", "maximum", "expected"),
    [
        ("a aa  ", 5, ["a ", "aa  "]),
        ("\na aa", 4, ["\na ", "aa"]),
    ],
)
def test_split_planner_applies_boundary_priority_only_among_feasible_endpoints(
    text: str,
    maximum: int,
    expected: list[str],
) -> None:
    ranges = chunking_module._plan_split_ranges(
        text,
        maximum,
        next_source=None,
        require_nonwhitespace=True,
    )

    assert ranges is not None
    assert [text[start:end] for start, end in ranges] == expected


def test_recursive_splitter_uses_unicode_scalar_fallback_without_loss() -> None:
    source = element(0, "汉字🙂界")

    chunks = build_chunks(
        document((source,)),
        small_profile(parent_max_chars=6, child_max_chars=2),
    )
    children = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD]

    assert [child.raw_content for child in children] == ["汉字", "🙂界"]
    assert "".join(child.raw_content for child in children) == source.content


def test_child_boundaries_never_emit_join_only_chunks_at_exact_limits() -> None:
    first = element(0, "AA")
    second = element(1, "BB")

    chunks = build_chunks(
        document((first, second)),
        small_profile(parent_max_chars=6, child_max_chars=2),
    )
    children = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD]

    assert "".join(child.raw_content for child in children) == "AA\n\nBB"
    assert all(
        isinstance(child.provenance, TextChildProvenance)
        and child.provenance.source_spans
        for child in children
    )


def test_whitespace_heavy_element_uses_a_feasible_lossless_partition() -> None:
    source = element(0, "AB    C")

    chunks = build_chunks(
        document((source,)),
        small_profile(parent_max_chars=3, child_max_chars=3),
    )
    parents = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_PARENT]

    assert "".join(parent.raw_content for parent in parents) == source.content
    assert all(parent.raw_content.strip() for parent in parents)
    assert all(len(parent.raw_content) <= 3 for parent in parents)


def test_long_source_whitespace_is_losslessly_covered_by_default_sized_children() -> (
    None
):
    source = element(0, "A" + " " * 4_000 + "B")
    profile = small_profile(parent_max_chars=8_000, child_max_chars=1_500)

    chunks = build_chunks(document((source,)), profile)
    parent = next(
        chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_PARENT
    )
    children = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD]

    assert parent.raw_content == source.content
    assert "".join(child.raw_content for child in children) == parent.raw_content
    assert all(
        0 < len(child.raw_content) <= profile.child_max_chars for child in children
    )
    assert [
        (child.provenance.parent_char_start, child.provenance.parent_char_end)
        for child in children
        if isinstance(child.provenance, TextChildProvenance)
    ] == _contiguous_ranges(len(parent.raw_content), children)
    assert all(
        isinstance(child.provenance, TextChildProvenance)
        and child.provenance.source_spans
        and child.content_hash == chunk_content_hash(child.raw_content)
        for child in children
    )
    source_spans = [
        span
        for child in children
        if isinstance(child.provenance, TextChildProvenance)
        for span in child.provenance.source_spans
    ]
    assert source_spans[0].char_start == 0
    assert source_spans[-1].char_end == len(source.content)
    assert all(span.element_id == source.element_id for span in source_spans)
    assert all(
        left.char_end == right.char_start
        for left, right in zip(source_spans, source_spans[1:])
    )


def test_farthest_feasible_fallback_avoids_default_child_count_explosion() -> None:
    maximum = 1_500
    source = element(0, "a" * maximum + " " * (2 * maximum - 2) + "b")

    chunks = build_chunks(document((source,)), ChunkingProfile())
    parents = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_PARENT]
    children = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD]

    assert len(parents) == 1
    assert len(children) == 3
    assert [len(child.raw_content) for child in children] == [
        maximum - 1,
        maximum,
        maximum,
    ]
    assert [
        (child.provenance.parent_char_start, child.provenance.parent_char_end)
        for child in children
        if isinstance(child.provenance, TextChildProvenance)
    ] == [
        (0, maximum - 1),
        (maximum - 1, 2 * maximum - 1),
        (2 * maximum - 1, 3 * maximum - 1),
    ]
    assert all(child.raw_content.strip() for child in children)
    assert "".join(child.raw_content for child in children) == source.content


def test_previous_matching_positions_retain_the_farthest_feasible_index() -> None:
    previous_matching = getattr(chunking_module, "_previous_matching_positions", None)

    assert previous_matching is not None
    assert previous_matching([False, True, False, False, True, False]) == [
        -1,
        1,
        1,
        1,
        4,
        4,
    ]


def test_child_partition_shares_join_whitespace_with_source_backed_children() -> None:
    elements = (element(0, "A"), element(1, "BB"))

    chunks = build_chunks(
        document(elements),
        small_profile(parent_max_chars=6, child_max_chars=2),
    )
    children = [chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD]

    assert [child.raw_content for child in children] == [
        "A\n",
        "\nB",
        "B",
    ]
    assert all(
        isinstance(child.provenance, TextChildProvenance)
        and child.provenance.source_spans
        for child in children
    )


def test_every_parent_is_immediately_followed_by_only_its_complete_children() -> None:
    parsed = document(
        (
            element(0, "A" * 35, section_path=("A",)),
            element(1, "B" * 13, section_path=("B",)),
        )
    )

    chunks = build_chunks(parsed, small_profile(parent_max_chars=16, child_max_chars=7))

    assert [chunk.order for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.document_id == parsed.document_id for chunk in chunks)
    assert all(chunk.revision_id == parsed.revision_id for chunk in chunks)
    parent_ids = {
        chunk.chunk_id for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_PARENT
    }
    for index, chunk in enumerate(chunks):
        if chunk.chunk_type is not ChunkType.TEXT_PARENT:
            continue
        following = []
        for candidate in chunks[index + 1 :]:
            if candidate.chunk_type is not ChunkType.TEXT_CHILD:
                break
            following.append(candidate)
        assert following
        assert all(child.parent_id == chunk.chunk_id for child in following)
        assert "".join(child.raw_content for child in following) == chunk.raw_content
    for child in (
        chunk for chunk in chunks if chunk.chunk_type is ChunkType.TEXT_CHILD
    ):
        assert child.parent_id in parent_ids


def test_all_text_element_characters_have_exactly_once_parent_span_coverage() -> None:
    elements = tuple(
        element(index, f"element-{index}-" + "x" * (index + 4)) for index in range(8)
    )

    chunks = build_chunks(
        document(elements),
        small_profile(parent_max_chars=24, child_max_chars=8),
    )
    by_element: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for chunk in chunks:
        if not isinstance(chunk.provenance, TextParentProvenance):
            continue
        for span in chunk.provenance.source_spans:
            by_element[span.element_id].append((span.char_start, span.char_end))

    for source in elements:
        ranges = by_element[source.element_id]
        assert ranges[0][0] == 0
        assert ranges[-1][1] == len(source.content)
        assert all(left[1] == right[0] for left, right in zip(ranges, ranges[1:]))


def test_large_synthetic_document_chunks_without_whole_document_concatenation() -> None:
    elements = tuple(
        element(index, f"item-{index:04d}", section_path=("Large",))
        for index in range(1_500)
    )

    chunks = build_chunks(
        document(elements),
        small_profile(parent_max_chars=96, child_max_chars=24),
    )

    assert chunks
    assert [chunk.order for chunk in chunks] == list(range(len(chunks)))
    covered_ids = {
        span.element_id
        for chunk in chunks
        if isinstance(chunk.provenance, TextParentProvenance)
        for span in chunk.provenance.source_spans
    }
    assert covered_ids == {source.element_id for source in elements}


def test_child_lineage_uses_one_ordered_sweep_for_many_maps_and_children() -> None:
    class CountingSourceMaps:
        def __init__(self, items: tuple[object, ...]) -> None:
            self._items = items
            self.lookups = 0

        def __len__(self) -> int:
            return len(self._items)

        def __getitem__(self, index: int | slice) -> object:
            item = self._items[index]
            self.lookups += len(item) if isinstance(item, tuple) else 1
            return item

    elements = tuple(
        element(index, "x", section_path=("Lineage",)) for index in range(2_000)
    )
    profile = small_profile(parent_max_chars=10_000, child_max_chars=31)
    parsed = document(elements)
    analysis = DocumentAnalyzer().analyze(parsed)
    run = analysis.items[0]
    assert isinstance(run, TextRun)
    drafts = chunking_module._parent_drafts(run, profile)
    assert len(drafts) == 1
    draft = drafts[0]
    ranges = chunking_module._child_ranges(
        draft.raw_content,
        profile.child_max_chars,
        draft.source_maps,
    )
    assert len(draft.source_maps) == 2_000
    assert len(ranges) > 100

    group_spans = getattr(chunking_module, "_child_source_span_groups", None)
    assert group_spans is not None
    counted_maps = CountingSourceMaps(draft.source_maps)
    span_groups = group_spans(ranges, counted_maps)

    assert len(span_groups) == len(ranges)
    assert all(span_group for span_group in span_groups)
    assert sum(len(span_group) for span_group in span_groups) == len(draft.source_maps)
    assert tuple(span for group in span_groups for span in group) == tuple(
        source_map.span for source_map in draft.source_maps
    )
    assert counted_maps.lookups <= len(draft.source_maps) + len(ranges)


@pytest.mark.parametrize(
    ("parent_max_chars", "child_max_chars", "separator"),
    [
        (4, 1, "\n\n"),
        (5, 2, "   "),
        (3, 1, "\u3000"),
    ],
)
def test_chunking_profile_rejects_joinable_unlineaged_separator_gaps(
    parent_max_chars: int,
    child_max_chars: int,
    separator: str,
) -> None:
    with pytest.raises(ValidationError, match="inter_element_separator gap"):
        ChunkingProfile(
            parent_max_chars=parent_max_chars,
            child_max_chars=child_max_chars,
            inter_element_separator=separator,
        )


def test_chunking_profile_rejects_three_minimal_elements_before_chunking() -> None:
    with pytest.raises(ValidationError, match="inter_element_separator gap"):
        ChunkingProfile(
            parent_max_chars=7,
            child_max_chars=2,
            inter_element_separator="\n\n",
        )


@pytest.mark.parametrize(
    ("parent_max_chars", "child_max_chars", "separator"),
    [
        (3, 1, "\n\n"),
        (4, 2, "\n\n"),
        (6, 2, "\n\n"),
        (4, 1, "   "),
        (3, 2, "\u3000"),
    ],
)
def test_chunking_profile_accepts_unreachable_or_partitionable_separator_gaps(
    parent_max_chars: int,
    child_max_chars: int,
    separator: str,
) -> None:
    profile = ChunkingProfile(
        parent_max_chars=parent_max_chars,
        child_max_chars=child_max_chars,
        inter_element_separator=separator,
    )

    assert profile.parent_max_chars == parent_max_chars
    assert profile.child_max_chars == child_max_chars
    assert profile.inter_element_separator == separator


def test_chunking_profile_density_check_handles_large_integers_directly() -> None:
    huge_parent_limit = 10**100

    accepted = ChunkingProfile(
        parent_max_chars=huge_parent_limit,
        child_max_chars=3,
        inter_element_separator="\n\n",
    )

    assert accepted.parent_max_chars == huge_parent_limit
    with pytest.raises(ValidationError, match="inter_element_separator gap"):
        ChunkingProfile(
            parent_max_chars=huge_parent_limit,
            child_max_chars=2,
            inter_element_separator="\n\n",
        )


def test_chunking_profile_is_frozen_versioned_and_output_sensitive() -> None:
    profile = ChunkingProfile()

    assert (
        DEFAULT_CHUNKER_BEHAVIOR_VERSION,
        PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT,
    ) == (
        "chunker-v7",
        "46aba26b91b69ccd6ba414561b594b88f49df674077cf0ef8344b04039737fcc",
    )
    assert profile.profile_fingerprint == PINNED_DEFAULT_CHUNKING_PROFILE_FINGERPRINT
    assert profile.profile_schema_version == 1
    assert profile.parent_max_chars == 8_000
    assert profile.child_max_chars == 1_500
    assert profile.table_slice_max_chars == 8_000
    assert profile.child_overlap_chars == 0
    assert profile.inter_element_separator == "\n\n"
    with pytest.raises(ValidationError, match="frozen"):
        profile.parent_max_chars = 1

    changed = profile.model_copy(update={"parent_max_chars": 7_999})
    assert changed.profile_fingerprint != profile.profile_fingerprint
    with pytest.raises(ValueError, match="chunker_behavior_version"):
        require_chunker_behavior_version_change(
            previous_profile=profile,
            previous_version="chunker-v7",
            current_profile=changed,
            current_version="chunker-v7",
        )
    require_chunker_behavior_version_change(
        previous_profile=profile,
        previous_version="chunker-v7",
        current_profile=changed,
        current_version="chunker-v8",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"parent_max_chars": 0},
        {"child_max_chars": 0},
        {"table_slice_max_chars": 0},
        {"child_overlap_chars": 1},
        {"inter_element_separator": ""},
        {"inter_element_separator": "JOIN"},
        {"profile_schema_version": 2},
        {"profile_schema_version": True},
        {"profile_schema_version": 1.0},
        {"child_overlap_chars": False},
        {"child_overlap_chars": 0.0},
    ],
)
def test_chunking_profile_rejects_unsafe_or_unsupported_values(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ChunkingProfile.model_validate(overrides)
