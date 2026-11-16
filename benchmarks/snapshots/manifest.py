"""Immutable snapshot creation and validation."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from benchmarks.contracts.models import SnapshotFile, SnapshotManifest


class SnapshotError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_path(root: Path, relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise SnapshotError("unsafe snapshot path")
    candidate = (root / pure).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise SnapshotError("snapshot path escapes root") from error
    return candidate


def build_manifest(
    root: Path,
    *,
    dataset_revision: str,
    license: str,
    source_url: str,
    relative_files: tuple[str, ...],
    total_documents: int,
    total_queries: int,
    supported_queries: int = 0,
    modality_counts: dict[str, int] | None = None,
) -> SnapshotManifest:
    files = tuple(
        SnapshotFile(
            relative_path=item,
            sha256=sha256_file(_safe_path(root, item)),
            size_bytes=_safe_path(root, item).stat().st_size,
        )
        for item in relative_files
    )
    return SnapshotManifest(
        dataset_revision=dataset_revision,
        license=license,
        source_url=source_url,
        files=files,
        total_documents=total_documents,
        total_queries=total_queries,
        supported_queries=supported_queries,
        modality_counts=modality_counts or {},
    )


def write_manifest(root: Path, manifest: SnapshotManifest) -> None:
    (root / "manifest.json").write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )


def load_manifest(root: Path) -> SnapshotManifest:
    path = root / "manifest.json"
    if not path.is_file():
        raise SnapshotError("manifest.json missing")
    try:
        return SnapshotManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise SnapshotError("invalid snapshot manifest") from error


def validate_snapshot(root: Path) -> SnapshotManifest:
    manifest = load_manifest(root)
    for item in manifest.files:
        path = _safe_path(root, item.relative_path)
        if not path.is_file():
            raise SnapshotError(f"missing snapshot file: {item.relative_path}")
        if path.stat().st_size != item.size_bytes:
            raise SnapshotError(f"size mismatch: {item.relative_path}")
        if sha256_file(path) != item.sha256:
            raise SnapshotError(f"checksum mismatch: {item.relative_path}")
    return manifest
