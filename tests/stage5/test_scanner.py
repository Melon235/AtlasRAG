"""Complete fail-closed directory scanning."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from atlasrag.services.sync import scanner as scanner_module
from atlasrag.services.sync.failures import ScanError
from atlasrag.services.sync.scanner import KnowledgeDirectoryScanner


def test_complete_snapshot_is_sorted_and_ignores_unsupported_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "nested" / "a.md").write_text("# a", encoding="utf-8")
    (tmp_path / "ignored.json").write_text("{}", encoding="utf-8")
    snapshot = KnowledgeDirectoryScanner().scan(tmp_path)
    assert tuple(entry.source.relative_source_path for entry in snapshot.entries) == (
        "b.txt",
        "nested/a.md",
    )
    assert all(len(entry.content_hash) == 64 for entry in snapshot.entries)


def test_empty_knowledge_directory_is_a_valid_complete_snapshot(tmp_path: Path) -> None:
    assert KnowledgeDirectoryScanner().scan(tmp_path).entries == ()


def test_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(target)
    with pytest.raises(ScanError) as caught:
        KnowledgeDirectoryScanner().scan(tmp_path)
    assert caught.value.code == "SCAN_SYMLINK_REJECTED"


def test_directory_symlink_is_rejected(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "link-dir").symlink_to(sub, target_is_directory=True)
    with pytest.raises(ScanError) as caught:
        KnowledgeDirectoryScanner().scan(tmp_path)
    assert caught.value.code == "SCAN_SYMLINK_REJECTED"


def test_disappearing_source_fails_the_whole_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    original: Callable[[object], str] = getattr(scanner_module, "content_hash")

    def vanish(source: object) -> str:
        (tmp_path / "a.txt").unlink()
        return original(source)

    monkeypatch.setattr("atlasrag.services.sync.scanner.content_hash", vanish)
    with pytest.raises(ScanError) as caught:
        KnowledgeDirectoryScanner().scan(tmp_path)
    assert caught.value.code == "SCAN_SOURCE_DISAPPEARED"


def test_source_changed_during_hash_fails_the_whole_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    original: Callable[[object], str] = getattr(scanner_module, "content_hash")

    def mutate(source: object) -> str:
        (tmp_path / "a.txt").write_text("changed-content", encoding="utf-8")
        return original(source)

    monkeypatch.setattr("atlasrag.services.sync.scanner.content_hash", mutate)
    with pytest.raises(ScanError) as caught:
        KnowledgeDirectoryScanner().scan(tmp_path)
    assert caught.value.code == "SCAN_SOURCE_CHANGED_DURING_SCAN"


def test_permission_failure_is_not_an_empty_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")

    def denied(path: object) -> object:
        raise PermissionError

    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(ScanError) as caught:
        KnowledgeDirectoryScanner().scan(tmp_path)
    assert caught.value.code == "SCAN_PERMISSION_DENIED"


def test_missing_root_is_a_scan_failure(tmp_path: Path) -> None:
    with pytest.raises(ScanError) as caught:
        KnowledgeDirectoryScanner().scan(tmp_path / "missing")
    assert caught.value.code == "SCAN_ROOT_UNAVAILABLE"
