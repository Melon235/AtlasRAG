"""Complete, fail-closed Knowledge Directory observation."""

from __future__ import annotations

import os
from pathlib import Path
from stat import S_ISREG

from atlasrag.domain.ingestion.source import (
    SourceFile,
    canonical_relative_source_path,
    source_type_for_path,
)
from atlasrag.services.ingestion.identity import content_hash
from atlasrag.services.sync.failures import ScanError
from atlasrag.services.sync.models import ScanEntry, ScanSnapshot


def _validate_root(knowledge_directory: Path) -> Path:
    if not isinstance(knowledge_directory, Path):
        raise TypeError("knowledge_directory must be a pathlib.Path")
    try:
        root = knowledge_directory.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ScanError("SCAN_ROOT_UNAVAILABLE") from None
    if not root.is_dir():
        raise ScanError("SCAN_ROOT_NOT_DIRECTORY")
    return root


def _scandir(directory: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as iterator:
            return list(iterator)
    except PermissionError:
        raise ScanError("SCAN_PERMISSION_DENIED") from None
    except OSError:
        raise ScanError("SCAN_IO_ERROR") from None


def _relative_path(path: Path, root: Path) -> str:
    try:
        return canonical_relative_source_path(path.relative_to(root).as_posix())
    except ValueError:
        raise ScanError("SCAN_INVALID_SOURCE_PATH") from None


def _scan_file(root: Path, path: Path, relative_path: str) -> ScanEntry | None:
    try:
        source_type_for_path(relative_path)
    except ValueError:
        return None
    try:
        before = path.stat(follow_symlinks=False)
        source = SourceFile.from_knowledge_directory(root, relative_path)
        digest = content_hash(source)
        after = source.absolute_path.stat(follow_symlinks=False)
    except FileNotFoundError:
        raise ScanError("SCAN_SOURCE_DISAPPEARED") from None
    except PermissionError:
        raise ScanError("SCAN_PERMISSION_DENIED") from None
    except (OSError, RuntimeError, ValueError):
        if not path.exists():
            raise ScanError("SCAN_SOURCE_DISAPPEARED") from None
        raise ScanError("SCAN_SOURCE_UNREADABLE") from None
    if not S_ISREG(after.st_mode):
        raise ScanError("SCAN_SPECIAL_FILE")
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ino != after.st_ino
        or before.st_dev != after.st_dev
    ):
        raise ScanError("SCAN_SOURCE_CHANGED_DURING_SCAN")
    try:
        return ScanEntry(source=source, content_hash=digest)
    except ValueError:
        raise ScanError("SCAN_INVALID_SOURCE") from None


def _walk(root: Path, current: Path, entries: dict[str, ScanEntry]) -> None:
    directory_entries = _scandir(current)
    for item in directory_entries:
        try:
            if item.is_symlink():
                raise ScanError("SCAN_SYMLINK_REJECTED")
            path = Path(item.path)
            if item.is_dir(follow_symlinks=False):
                _walk(root, path, entries)
                continue
            if not item.is_file(follow_symlinks=False):
                raise ScanError("SCAN_SPECIAL_FILE")
            relative_path = _relative_path(path, root)
            if relative_path in entries:
                raise ScanError("SCAN_DUPLICATE_SOURCE_IDENTITY")
            entry = _scan_file(root, path, relative_path)
            if entry is not None:
                entries[relative_path] = entry
        except ScanError:
            raise
        except OSError:
            raise ScanError("SCAN_IO_ERROR") from None


class KnowledgeDirectoryScanner:
    """Return one immutable snapshot or fail; never expose a partial scan."""

    def scan(self, knowledge_directory: Path) -> ScanSnapshot:
        root = _validate_root(knowledge_directory)
        entries: dict[str, ScanEntry] = {}
        _walk(root, root, entries)
        return ScanSnapshot(entries=tuple(entries[path] for path in sorted(entries)))
