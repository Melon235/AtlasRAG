from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.contracts.models import SnapshotManifest
from benchmarks.datasets.open_ragbench import DatasetAdapterError, OpenRagBenchAdapter
from benchmarks.snapshots.manifest import (
    SnapshotError,
    build_manifest,
    validate_snapshot,
    write_manifest,
)


def snapshot(root: Path) -> SnapshotManifest:
    queries = {
        "q1": {"query": "text question", "type": "extractive", "source": "text"},
        "q2": {
            "query": "table question",
            "type": "abstractive",
            "source": "text-table",
        },
        "q3": {
            "query": "image question",
            "type": "abstractive",
            "source": "text-image",
        },
    }
    qrels = {
        "q1": {"doc_id": "doc-a", "section_id": 1},
        "q2": {"doc_id": "doc-b", "section_id": 2},
        "q3": {"doc_id": "doc-c", "section_id": 3},
    }
    answers = {"q1": "answer one", "q2": "answer two", "q3": "answer three"}
    for name, value in (
        ("queries.json", queries),
        ("qrels.json", qrels),
        ("answers.json", answers),
    ):
        (root / name).write_text(json.dumps(value), encoding="utf-8")
    manifest = build_manifest(
        root,
        dataset_revision="rev-1",
        license="CC-BY-NC-4.0",
        source_url="fixture",
        relative_files=("queries.json", "qrels.json", "answers.json"),
        total_documents=4,
        total_queries=3,
        supported_queries=2,
        modality_counts={"text": 1, "text-table": 1, "text-image": 1},
    )
    write_manifest(root, manifest)
    return manifest


def test_valid_snapshot_and_supported_profile(tmp_path: Path) -> None:
    manifest = snapshot(tmp_path)
    assert validate_snapshot(tmp_path) == manifest
    adapter = OpenRagBenchAdapter(tmp_path)
    assert adapter.counts() == {
        "total": 3,
        "supported": 2,
        "excluded": 1,
        "by_modality": {
            "text": 1,
            "text-table": 1,
            "text-image": 1,
            "text-table-image": 0,
        },
    }
    assert adapter.supported_profiles()["FULL_CORPUS"] == {
        "documents": 4,
        "queries": 2,
        "positive_documents": 3,
        "hard_negative_documents": 1,
    }
    assert adapter.supported_profiles()["NEGATIVE_ONLY_CORPUS"] == {
        "documents": 1,
        "queries": 2,
    }


def test_checksum_mismatch_and_path_traversal(tmp_path: Path) -> None:
    snapshot(tmp_path)
    (tmp_path / "queries.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SnapshotError, match="size mismatch|checksum mismatch"):
        validate_snapshot(tmp_path)
    with pytest.raises(SnapshotError):
        build_manifest(
            tmp_path,
            dataset_revision="rev",
            license="x",
            source_url="x",
            relative_files=("../escape.json",),
            total_documents=0,
            total_queries=0,
        )


def test_invalid_qrel_mapping_rejected(tmp_path: Path) -> None:
    snapshot(tmp_path)
    qrels = json.loads((tmp_path / "qrels.json").read_text(encoding="utf-8"))
    del qrels["q1"]
    (tmp_path / "qrels.json").write_text(json.dumps(qrels), encoding="utf-8")
    with pytest.raises(SnapshotError, match="size mismatch|checksum mismatch"):
        OpenRagBenchAdapter(tmp_path)


def test_unexpected_file_and_manifest_count_mismatch(tmp_path: Path) -> None:
    snapshot(tmp_path)
    (tmp_path / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SnapshotError, match="unexpected snapshot files"):
        validate_snapshot(tmp_path)
    (tmp_path / "unexpected.json").unlink()
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    manifest["total_queries"] = 999
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DatasetAdapterError, match="total_queries mismatch"):
        OpenRagBenchAdapter(tmp_path)


def test_manifest_must_cover_all_consumed_files(tmp_path: Path) -> None:
    snapshot(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"] = [
        item for item in manifest["files"] if item["relative_path"] != "qrels.json"
    ]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SnapshotError, match="required snapshot files"):
        validate_snapshot(tmp_path)
