from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlasrag.domain.ingestion.source import derive_document_id
from benchmarks.pilot.identity import (
    PilotIdentityError,
    load_pilot_inputs,
    preflight_status,
)
from benchmarks.snapshots.manifest import build_manifest, write_manifest


def _fixture(root: Path) -> tuple[Path, Path]:
    root.mkdir()
    queries = {
        f"q{index}": {
            "query": f"Question {index}?",
            "type": "extractive",
            "source": "text" if index < 7 else "text-table",
        }
        for index in range(10)
    }
    qrels = {
        f"q{index}": {"doc_id": f"2401.{index:05d}v1", "section_id": 0}
        for index in range(10)
    }
    answers = {f"q{index}": "answer" for index in range(10)}
    urls = {
        f"2401.{index:05d}v1": f"https://arxiv.org/pdf/2401.{index:05d}v1"
        for index in range(10)
    }
    for name, value in (
        ("queries.json", queries),
        ("qrels.json", qrels),
        ("answers.json", answers),
        ("pdf_urls.json", urls),
    ):
        (root / name).write_text(json.dumps(value), encoding="utf-8")
    manifest = build_manifest(
        root,
        dataset_revision="63f6b052ff83508b08e242db42263ee708815c26",
        license="CC-BY-NC-4.0",
        source_url="synthetic",
        relative_files=("queries.json", "qrels.json", "answers.json", "pdf_urls.json"),
        total_documents=10,
        total_queries=10,
        supported_queries=10,
        modality_counts={"text": 7, "text-table": 3},
        corpus_document_ids=tuple(urls),
    )
    write_manifest(root, manifest)
    selected = []
    gold = {}
    for qid, raw in qrels.items():
        doc = str(raw["doc_id"])
        selected.append(
            {
                "query_id": qid,
                "official_document_id": doc,
                "section_id": 0,
                "relevance": 1,
                "modality": queries[qid]["source"],
                "source_url": urls[doc],
                "local_source_path": f"{doc}.pdf",
                "derived_local_document_id": derive_document_id(f"{doc}.pdf"),
            }
        )
        gold[qid] = {doc: 1}
    selection = root.parent / "selection.json"
    gold_path = root.parent / "gold.json"
    selection.write_text(
        json.dumps(
            {
                "dataset_revision": "63f6b052ff83508b08e242db42263ee708815c26",
                "selected": selected,
            }
        ),
        encoding="utf-8",
    )
    gold_path.write_text(json.dumps(gold), encoding="utf-8")
    return selection, gold_path


def test_pilot_identity_maps_official_to_canonical(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    selection, gold = _fixture(snapshot)
    inputs = load_pilot_inputs(snapshot, selection, gold)
    assert len(inputs.queries) == 10
    assert len(inputs.mappings) == 10
    assert all(query.reference_answer is None for query in inputs.queries)
    assert set(inputs.gold_canonical) == {f"q{i}" for i in range(10)}
    assert preflight_status(False) == "PILOT_AWAITING_AUTHORIZED_INGESTION"


def test_pilot_identity_rejects_duplicate_document(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    selection, gold = _fixture(snapshot)
    value = json.loads(selection.read_text())
    value["selected"][1]["official_document_id"] = value["selected"][0][
        "official_document_id"
    ]
    selection.write_text(json.dumps(value))
    with pytest.raises(PilotIdentityError, match="duplicate"):
        load_pilot_inputs(snapshot, selection, gold)


@pytest.mark.asyncio
async def test_pilot_ingestion_rejects_unexpected_pdf_set(tmp_path: Path) -> None:
    from benchmarks.pilot.ingestion import ingest_selected_pdfs

    snapshot = tmp_path / "snapshot"
    selection, gold = _fixture(snapshot)
    inputs = load_pilot_inputs(snapshot, selection, gold)
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    (pdfs / "unexpected.pdf").write_bytes(b"not a real PDF")
    with pytest.raises(ValueError, match="pilot PDF set mismatch"):
        await ingest_selected_pdfs(
            pdf_directory=pdfs,
            inputs=inputs,
            backend=object(),
            uow_factory=object(),
            runtime=object(),  # type: ignore[arg-type]
            verifier=object(),  # type: ignore[arg-type]
            expected_pipeline_fingerprint="0" * 64,
        )
