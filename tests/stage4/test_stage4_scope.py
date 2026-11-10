"""Closed Stage 4 structural scope and representative prohibited imports."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD_STATE_FIELDS = {
    "DocumentBuildInput": {"context": "CandidateBuildContext"},
    "DocumentBuildOutput": {"result": "BuildResult"},
    "DocumentBuildState": {
        "context": "CandidateBuildContext",
        "document": "DocumentContainer | None",
        "chunks": "tuple[ChunkRecord, ...] | None",
        "source_mutated": "bool",
        "failed_stage": "FailedStage | None",
        "reason_code": "BuildFailureCode | None",
        "result": "BuildResult",
    },
}


def run_checker(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_import_boundaries.py"),
            "--root",
            str(root),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


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


def test_build_state_has_only_canonical_business_data_and_no_dependencies() -> None:
    source = (ROOT / "src/atlasrag/graphs/indexing/state.py").read_text()
    assert state_fields(source) == BUILD_STATE_FIELDS


@pytest.mark.parametrize(
    "annotation",
    [
        "vectors: list[list[float]]",
        "provider: EmbeddingProvider",
        "dependencies: BuildResources",
        "lease: DocumentBuildLease",
    ],
)
def test_build_state_guard_rejects_vectors_and_dependencies(annotation: str) -> None:
    source = (ROOT / "src/atlasrag/graphs/indexing/state.py").read_text()
    assert state_fields(source + f"    {annotation}\n") != BUILD_STATE_FIELDS


def assert_state_updates_are_closed(source: str) -> None:
    """Check the graph's explicit update literals and direct State mutations."""
    allowed = set(BUILD_STATE_FIELDS["DocumentBuildState"])
    for node in ast.walk(ast.parse(source)):
        update = None
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            update = node.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "update":
                    if isinstance(node.value, ast.Dict):
                        update = node.value
                elif (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "state"
                ):
                    assert (
                        isinstance(target.slice, ast.Constant)
                        and target.slice.value in allowed
                    )
        if update is not None:
            assert all(
                isinstance(key, ast.Constant) and key.value in allowed
                for key in update.keys
            )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"state", "update"}
            and node.func.attr in {"update", "setdefault"}
        ):
            pytest.fail("State updates must use explicit checked literals")


def test_graph_updates_have_no_unrecognized_state_keys() -> None:
    source = (ROOT / "src/atlasrag/graphs/indexing/document_build.py").read_text()
    assert_state_updates_are_closed(source)


@pytest.mark.parametrize(
    "statement",
    [
        'return {"vectors": []}',
        'update = {"dependencies": None}',
        'state["provider"] = None',
        "return {**unrecognized}",
        'state.update({"lease": None})',
        'update.setdefault("vectors", [])',
    ],
)
def test_state_update_guard_rejects_unknown_keys(statement: str) -> None:
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        assert_state_updates_are_closed(f"def node(state):\n    {statement}\n")


def test_fifteen_public_state_exports_remain_frozen() -> None:
    import atlasrag.graphs.state as state

    assert state.__all__ == (
        "RetrievalGraphInput",
        "RetrievalGraphState",
        "RetrievalGraphOutput",
        "RagCoreInput",
        "RagCoreState",
        "RagCoreOutput",
        "LocalEvidenceSubgraphInput",
        "LocalEvidenceState",
        "LocalEvidenceSubgraphOutput",
        "WebEvidenceSubgraphInput",
        "WebEvidenceState",
        "WebEvidenceSubgraphOutput",
        "AnswerSubgraphInput",
        "AnswerState",
        "AnswerSubgraphOutput",
    )


def test_no_new_migration_or_top_level_maintenance_graph() -> None:
    assert {p.name for p in (ROOT / "migrations/versions").glob("*.py")} == {
        "20260928_0001_stage2_persistence.py"
    }
    assert {p.name for p in (ROOT / "src/atlasrag/graphs/indexing").glob("*.py")} == {
        "__init__.py",
        "contracts.py",
        "state.py",
        "document_build.py",
    }
    for path in (ROOT / "src/atlasrag").rglob("*.py"):
        tree = ast.parse(path.read_text())
        assert path.stem != "index_graph"
        assert not any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name
            in {
                "build_index_graph",
                "reconcile_runtime",
                "enter_maintenance",
                "resume_serving",
            }
            for node in ast.walk(tree)
        ), path


@pytest.mark.parametrize("module", ["sqlalchemy", "sqlite3", "psycopg_pool"])
@pytest.mark.parametrize(
    "path",
    [
        "services/indexing/representation.py",
        "graphs/indexing/document_build.py",
        "graphs/nodes/other.py",
    ],
)
def test_pure_representation_and_graph_forbid_database_plumbing(
    tmp_path: Path, module: str, path: str
) -> None:
    destination = tmp_path / "src/atlasrag" / path
    destination.parent.mkdir(parents=True)
    destination.write_text(f"import {module}\n")
    result = run_checker(tmp_path)
    assert result.returncode != 0, f"accepted forbidden {module} in {path}"


@pytest.mark.parametrize(
    "module",
    [
        "redis",
        "atlasrag.providers.cache",
        "atlasrag.graphs.state",
        "atlasrag.services.retrieval",
        "atlasrag.services.answer",
        "atlasrag.services.web_evidence",
        "atlasrag.services.local_evidence",
        "atlasrag.services.rag_core",
        "pymilvus",
        "psycopg",
        "psycopg_pool",
        "sqlalchemy",
        "sqlite3",
    ],
)
@pytest.mark.parametrize(
    "path", ["services/indexing/publication.py", "services/indexing/candidate.py"]
)
def test_index_services_cannot_import_redis_or_part_ii(
    tmp_path: Path, module: str, path: str
) -> None:
    destination = tmp_path / "src/atlasrag" / path
    destination.parent.mkdir(parents=True)
    destination.write_text(f"import {module}\n")
    result = run_checker(tmp_path)
    assert result.returncode != 0, f"accepted forbidden {module} in {path}"


def test_stage4_real_tree_respects_import_boundaries() -> None:
    result = run_checker(ROOT)
    assert result.returncode == 0, result.stderr
