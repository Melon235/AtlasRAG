"""Closed Stage 5 graph state, ownership, and command scope."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE_FIELDS = {
    "IndexGraphInput": {"request": "SyncRequest"},
    "IndexGraphOutput": {"final_result": "SyncResult"},
    "IndexGraphState": {
        "request": "SyncRequest",
        "reconciliation_result": "ReconciliationResult",
        "scan_snapshot": "ScanSnapshot",
        "sync_plan": "SyncPlan",
        "build_summary": "BuildExecutionSummary",
        "deletion_summary": "DeletionSummary",
        "final_result": "SyncResult",
    },
}
FORBIDDEN_STATE_NAMES = {
    "RuntimeConfig",
    "EmbeddingProvider",
    "CandidateIndex",
    "PostgresPool",
    "MilvusIndexProvider",
    "dense_vector",
    "lease",
    "receipt",
    "traceback",
}


def state_fields(source: str) -> dict[str, dict[str, str]]:
    return {
        node.name: {
            field.target.id: ast.unparse(field.annotation)
            for field in node.body
            if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)
        }
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef)
    }


def test_index_graph_state_is_closed_and_dependency_free() -> None:
    source = (ROOT / "src/atlasrag/graphs/indexing/sync_state.py").read_text()
    assert state_fields(source) == STATE_FIELDS
    annotations = " ".join(
        annotation for fields in STATE_FIELDS.values() for annotation in fields.values()
    )
    assert not any(name in annotations for name in FORBIDDEN_STATE_NAMES)


def test_index_graph_has_no_lifecycle_or_plumbing_control() -> None:
    path = ROOT / "src/atlasrag/graphs/indexing/index_graph.py"
    tree = ast.parse(path.read_text())
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "build_index_graph" in names
    assert "enter_maintenance" not in names
    assert "resume_serving" not in names
    text = path.read_text()
    assert 'add_node("reconcile_runtime"' in text
    for forbidden in ("psycopg", "pymilvus", "redis", "torch", "sqlite3"):
        assert forbidden not in text


def test_stage5_commands_exist_and_stay_out_of_default_verify() -> None:
    makefile = (ROOT / "Makefile").read_text()
    for target in ("test-stage5", "test-stage5-integration", "stage5-verify"):
        assert f"{target}:" in makefile
    script = ROOT / "scripts/stage5_verify.sh"
    assert script.is_file()
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_import_boundaries.py"),
            "--root",
            str(ROOT),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
