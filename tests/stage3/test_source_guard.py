"""Content-authoritative Stage 3 source mutation checks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from atlasrag.domain.errors import SourceMutationError
from atlasrag.domain.ingestion.source import SourceFile
from atlasrag.services.ingestion.identity import content_hash
from atlasrag.services.ingestion.source_guard import ensure_source_unchanged


def source_file(root: Path, content: bytes = b"original") -> SourceFile:
    path = root / "document.txt"
    path.write_bytes(content)
    return SourceFile.from_knowledge_directory(root, "document.txt")


def test_unchanged_source_passes_and_returns_verified_hash(tmp_path: Path) -> None:
    source = source_file(tmp_path)
    expected = content_hash(source)

    assert ensure_source_unchanged(source, expected) == expected


def test_changed_bytes_raise_frozen_source_mutation_error(tmp_path: Path) -> None:
    source = source_file(tmp_path)
    expected = content_hash(source)
    source.absolute_path.write_bytes(b"mutated")

    with pytest.raises(SourceMutationError) as exc_info:
        ensure_source_unchanged(source, expected)

    assert str(exc_info.value) == "The source changed during processing."


def test_mtime_change_without_byte_change_is_not_mutation(tmp_path: Path) -> None:
    source = source_file(tmp_path)
    expected = content_hash(source)
    before = source.absolute_path.stat()
    os.utime(
        source.absolute_path,
        ns=(before.st_atime_ns + 1_000_000, before.st_mtime_ns + 1_000_000),
    )

    assert ensure_source_unchanged(source, expected) == expected


def test_in_root_symlink_retarget_is_detected_by_content(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    link = tmp_path / "document.txt"
    link.symlink_to(first)
    source = SourceFile.from_knowledge_directory(tmp_path, "document.txt")
    expected = content_hash(source)
    link.unlink()
    link.symlink_to(second)

    with pytest.raises(SourceMutationError):
        ensure_source_unchanged(source, expected)


def test_symlink_retarget_outside_root_is_typed_source_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    inside = root / "inside.txt"
    outside = tmp_path / "outside.txt"
    inside.write_bytes(b"inside")
    outside.write_bytes(b"outside")
    link = root / "document.txt"
    link.symlink_to(inside)
    source = SourceFile.from_knowledge_directory(root, "document.txt")
    expected = content_hash(source)
    link.unlink()
    link.symlink_to(outside)

    with pytest.raises(SourceMutationError):
        ensure_source_unchanged(source, expected)


def test_deleted_source_is_typed_source_mutation(tmp_path: Path) -> None:
    source = source_file(tmp_path)
    expected = content_hash(source)
    source.absolute_path.unlink()

    with pytest.raises(SourceMutationError):
        ensure_source_unchanged(source, expected)


@pytest.mark.parametrize("expected_hash", ["", "0" * 63, "G" * 64])
def test_guard_rejects_noncanonical_expected_hash(
    tmp_path: Path, expected_hash: str
) -> None:
    source = source_file(tmp_path)

    with pytest.raises(ValueError, match="content hash"):
        ensure_source_unchanged(source, expected_hash)
