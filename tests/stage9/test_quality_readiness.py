from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.quality_command import _verify_pilot_readiness


def _inputs() -> SimpleNamespace:
    mapping = SimpleNamespace(
        local_source_path="doc.pdf",
        canonical_document_id="doc_canonical",
    )
    return SimpleNamespace(mappings=(mapping,), mapping_fingerprint="fp")


def _evidence() -> dict[str, object]:
    return {
        "status": "READY",
        "mapping_fingerprint": "fp",
        "selected_paths": ["doc.pdf"],
        "canonical_documents": ["doc_canonical"],
        "pipeline_fingerprint": "pipe",
        "readiness": {
            "serving_safety": "SAFE_TO_SERVE",
            "canonical_lineage_verified": True,
            "milvus_exact_set_verified": True,
            "pipeline_fingerprint_compatible": True,
            "stage5_verified_documents": 1,
            "verification_execution_id": "exec",
            "verification_evidence_digest": "digest",
        },
    }


def test_readiness_success_and_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "readiness.json"
    path.write_text(json.dumps(_evidence()))
    args = Namespace(readiness=path)
    runtime = SimpleNamespace(pipeline_fingerprint="pipe")
    _verify_pilot_readiness(args, _inputs(), runtime)
    evidence = _evidence()
    evidence["pipeline_fingerprint"] = "other"
    path.write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="pipeline fingerprint mismatch"):
        _verify_pilot_readiness(args, _inputs(), runtime)


def test_missing_readiness_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires durable readiness"):
        _verify_pilot_readiness(
            Namespace(readiness=None),
            _inputs(),
            SimpleNamespace(pipeline_fingerprint="pipe"),
        )
