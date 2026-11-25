"""Subprocess acceptance for executable synthetic Quality Run."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_run_quality_cli_subprocess_real_stack(
    postgres_dsn: str, milvus_uri: str, tmp_path: Path
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    queries = {
        "q1": {
            "query": "What is the maintenance interval?",
            "type": "extractive",
            "source": "text",
        },
        "q2": {
            "query": "Which maintenance interval does the manual require?",
            "type": "extractive",
            "source": "text",
        },
    }
    qrels = {
        "q1": {"doc_id": "doc-a", "section_id": 0},
        "q2": {"doc_id": "doc-b", "section_id": 0},
    }
    answers = {"q1": "30 days", "q2": "30 days"}
    urls = {"doc-a": "https://example.test/a", "doc-b": "https://example.test/b"}
    for name, value in (
        ("queries.json", queries),
        ("qrels.json", qrels),
        ("answers.json", answers),
        ("pdf_urls.json", urls),
    ):
        (snapshot / name).write_text(json.dumps(value), encoding="utf-8")
    from benchmarks.snapshots.manifest import build_manifest, write_manifest

    # Build a metadata-only synthetic snapshot with exact identities.
    manifest = build_manifest(
        snapshot,
        dataset_revision="synthetic-stage9-cli-v1",
        license="SYNTHETIC",
        source_url="synthetic",
        relative_files=("queries.json", "qrels.json", "answers.json", "pdf_urls.json"),
        total_documents=2,
        total_queries=2,
        supported_queries=2,
        corpus_document_ids=tuple(urls),
    )
    write_manifest(snapshot, manifest)
    gold = tmp_path / "gold.json"
    gold.write_text(
        json.dumps({"q1": {"doc-a": 1}, "q2": {"doc-b": 1}}), encoding="utf-8"
    )
    output = tmp_path / "out"
    env = dict(os.environ)
    env.update(
        {
            "ATLASRAG_POSTGRES_DSN": postgres_dsn,
            "ATLASRAG_MILVUS_URI": milvus_uri,
            "STAGE9_SYNTHETIC_ROOT": str(tmp_path / "corpus"),
            "STAGE9_MILVUS_COLLECTION": f"atlasrag_bench_cli_{tmp_path.name}",
            "STAGE9_SNAPSHOT_ID": "synthetic-stage9-cli-v1",
            "PYTHONPATH": f"{Path.cwd()}:{Path.cwd() / 'tests'}",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmarks.cli",
            "run-quality",
            "--snapshot",
            str(snapshot),
            "--runtime-module",
            "tests/integration/stage9_cli_runtime.py",
            "--gold",
            str(gold),
            "--output",
            str(output),
            "--repetitions",
            "2",
            "--max-cases",
            "4",
        ],
        cwd=Path.cwd(),
        env=env,
        text=True,
        capture_output=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["label"] == "SYNTHETIC_INTEGRATION"
    assert result["status"] == "VALID"
    assert result["cases"] == 4
    assert len({item[2] for item in result["invocations"]}) == 4
    assert result["web_calls"] == 0
    assert result["cache_gets"] == 0 and result["cache_sets"] == 0
    assert result["replay_stages"]
    assert (output / "manifest.json").is_file()
    assert (output / "cases.jsonl").is_file()
    assert (output / "observation_cases.jsonl").is_file()
