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
    corpus = {
        "doc-a": "https://example.test/a",
        "doc-b": "https://example.test/b",
        "doc-c": "https://example.test/c",
        "doc-d": "https://example.test/d",
    }
    for name, value in (
        ("queries.json", queries),
        ("qrels.json", qrels),
        ("answers.json", answers),
        ("pdf_urls.json", corpus),
    ):
        (root / name).write_text(json.dumps(value), encoding="utf-8")
    manifest = build_manifest(
        root,
        dataset_revision="rev-1",
        license="CC-BY-NC-4.0",
        source_url="fixture",
        relative_files=("queries.json", "qrels.json", "answers.json", "pdf_urls.json"),
        total_documents=4,
        corpus_document_ids=tuple(corpus),
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
        "qrel_referenced_documents": 3,
        "unreferenced_documents": 1,
        "verified_hard_negative_documents": None,
        "partition_status": "UNVERIFIED",
    }
    assert adapter.supported_profiles()["NEGATIVE_ONLY_CORPUS"] == {
        "status": "BLOCKED_UNVERIFIED_PARTITION",
        "observed_unreferenced_documents": 1,
        "verified_hard_negative_documents": None,
        "queries": 2,
    }
    assert adapter.document_partition().verified_hard_negative_ids is None


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


def test_four_unreferenced_documents_cannot_be_certified_hard_negatives(
    tmp_path: Path,
) -> None:
    queries = {
        f"q{i}": {"query": f"question {i}", "type": "extractive", "source": "text"}
        for i in range(1, 5)
    }
    qrels = {f"q{i}": {"doc_id": f"positive-{i}", "section_id": 0} for i in range(1, 5)}
    answers = {f"q{i}": f"answer {i}" for i in range(1, 5)}
    corpus = {
        **{f"positive-{i}": f"https://example.test/positive-{i}" for i in range(1, 5)},
        **{
            f"unreferenced-{i}": f"https://example.test/unreferenced-{i}"
            for i in range(1, 5)
        },
    }
    for name, value in (
        ("queries.json", queries),
        ("qrels.json", qrels),
        ("answers.json", answers),
        ("pdf_urls.json", corpus),
    ):
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    manifest = build_manifest(
        tmp_path,
        dataset_revision="fixture",
        license="CC-BY-NC-4.0",
        source_url="fixture",
        relative_files=("queries.json", "qrels.json", "answers.json", "pdf_urls.json"),
        total_documents=8,
        total_queries=4,
        supported_queries=4,
        corpus_document_ids=tuple(corpus),
    )
    write_manifest(tmp_path, manifest)
    adapter = OpenRagBenchAdapter(tmp_path)
    partition = adapter.document_partition()
    assert len(partition.qrel_referenced_ids) == 4
    assert len(partition.unreferenced_ids) == 4
    assert partition.verified_hard_negative_ids is None
    assert adapter.negative_only_profile()["status"] == "BLOCKED_UNVERIFIED_PARTITION"


def test_in_directory_symlink_rejected_before_resolution(tmp_path: Path) -> None:
    snapshot(tmp_path)
    target = tmp_path / "queries.json"
    link = tmp_path / "linked-queries.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unsupported")
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"].append(
        {
            "relative_path": "linked-queries.json",
            "sha256": "0" * 64,
            "size_bytes": 0,
        }
    )
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SnapshotError, match="symlink"):
        validate_snapshot(tmp_path)


def test_duplicate_corpus_identity_and_qrel_membership_rejected(tmp_path: Path) -> None:
    snapshot(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    manifest["corpus_document_ids"] = ["doc-a", "doc-a", "doc-b", "doc-c", "doc-d"]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SnapshotError, match="invalid snapshot manifest"):
        validate_snapshot(tmp_path)
    # restore and remove a qrel-referenced corpus identity
    snapshot(tmp_path)
    qrels = json.loads((tmp_path / "qrels.json").read_text(encoding="utf-8"))
    qrels["q1"]["doc_id"] = "not-in-corpus"
    (tmp_path / "qrels.json").write_text(json.dumps(qrels), encoding="utf-8")
    # rebuild checksums after the intentional mutation, then test semantic rejection
    from benchmarks.snapshots.manifest import build_manifest, write_manifest

    corpus = json.loads((tmp_path / "pdf_urls.json").read_text(encoding="utf-8"))
    manifest_obj = build_manifest(
        tmp_path,
        dataset_revision="rev-1",
        license="CC-BY-NC-4.0",
        source_url="fixture",
        relative_files=("queries.json", "qrels.json", "answers.json", "pdf_urls.json"),
        total_documents=4,
        total_queries=3,
        supported_queries=2,
        corpus_document_ids=tuple(corpus),
    )
    write_manifest(tmp_path, manifest_obj)
    with pytest.raises(DatasetAdapterError, match="qrel document"):
        OpenRagBenchAdapter(tmp_path)
