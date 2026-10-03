"""Behavioral tests for the production import-boundary checker."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPOSITORY_ROOT / "scripts" / "check_import_boundaries.py"


def run_checker(root: Path) -> subprocess.CompletedProcess[str]:
    """Execute the real checker against a repository-shaped fixture tree."""
    return subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(root)],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def write_production_module(root: Path, source: str, name: str = "module.py") -> Path:
    """Write a module below the production package in a temporary tree."""
    module = root / "src" / "atlasrag" / "nested" / name
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(source, encoding="utf-8")
    return module


def write_layer_module(
    root: Path,
    layer: str,
    source: str,
    name: str = "module.py",
) -> Path:
    """Write a module inside one production architecture layer."""

    module = root / "src" / "atlasrag" / layer / "nested" / name
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(source, encoding="utf-8")
    return module


def test_real_repository_respects_import_boundaries() -> None:
    result = run_checker(REPOSITORY_ROOT)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("source", "prohibited_module"),
    [
        ("import tests\n", "tests"),
        ("import tests.helpers as helpers\n", "tests.helpers"),
        ("import os, tests.fixtures as fixtures\n", "tests.fixtures"),
        ("from tests import fixtures\n", "tests"),
        ("from tests.fixtures import example\n", "tests.fixtures"),
        ("import benchmarks\n", "benchmarks"),
        ("import benchmarks.corpus as corpus\n", "benchmarks.corpus"),
        ("from benchmarks import corpus\n", "benchmarks"),
        ("from benchmarks.corpus import example\n", "benchmarks.corpus"),
        ("import atlasrag.tests.helpers\n", "atlasrag.tests.helpers"),
        ("from atlasrag.benchmarks import corpus\n", "atlasrag.benchmarks"),
    ],
)
def test_prohibited_import_families_fail(
    tmp_path: Path, source: str, prohibited_module: str
) -> None:
    write_production_module(tmp_path, source)

    result = run_checker(tmp_path)

    assert result.returncode != 0
    assert (
        f"src/atlasrag/nested/module.py:1: forbidden import '{prohibited_module}'"
    ) in result.stderr


def test_comments_strings_and_similar_names_are_allowed(tmp_path: Path) -> None:
    write_production_module(
        tmp_path,
        '# import tests\nMESSAGE = "from benchmarks import corpus"\nimport tests_helpers\n',
    )

    result = run_checker(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_violations_are_reported_in_deterministic_path_and_line_order(
    tmp_path: Path,
) -> None:
    write_production_module(tmp_path, "\nimport tests\n", "z_last.py")
    write_production_module(tmp_path, "import benchmarks\n", "a_first.py")

    result = run_checker(tmp_path)

    assert result.returncode != 0
    assert result.stderr.splitlines() == [
        "src/atlasrag/nested/a_first.py:1: forbidden import 'benchmarks'",
        "src/atlasrag/nested/z_last.py:2: forbidden import 'tests'",
    ]


def test_invalid_production_syntax_fails_closed(tmp_path: Path) -> None:
    write_production_module(tmp_path, "def broken(:\n")

    result = run_checker(tmp_path)

    assert result.returncode != 0
    assert "src/atlasrag/nested/module.py:1: syntax error:" in result.stderr


@pytest.mark.parametrize(
    ("source", "prohibited_module"),
    [
        ("import atlasrag.graphs\n", "atlasrag.graphs"),
        ("from atlasrag.providers import cache\n", "atlasrag.providers"),
        ("from atlasrag import providers\n", "atlasrag.providers"),
        ("from ...providers import index\n", "atlasrag.providers"),
        ("import atlasrag.benchmarks\n", "atlasrag.benchmarks"),
    ],
)
def test_repositories_cannot_import_orchestration_provider_or_benchmark_layers(
    tmp_path: Path,
    source: str,
    prohibited_module: str,
) -> None:
    write_layer_module(tmp_path, "repositories", source)

    result = run_checker(tmp_path)

    assert result.returncode != 0
    assert (f"repository layer may not import '{prohibited_module}'") in result.stderr


@pytest.mark.parametrize(
    ("source", "prohibited_module"),
    [
        ("import atlasrag.repositories\n", "atlasrag.repositories"),
        ("from atlasrag.graphs import state\n", "atlasrag.graphs"),
        ("from atlasrag import repositories\n", "atlasrag.repositories"),
        ("from ...repositories import postgres\n", "atlasrag.repositories"),
        ("import atlasrag.benchmarks\n", "atlasrag.benchmarks"),
    ],
)
def test_providers_cannot_import_repository_graph_or_benchmark_layers(
    tmp_path: Path,
    source: str,
    prohibited_module: str,
) -> None:
    write_layer_module(tmp_path, "providers", source)

    result = run_checker(tmp_path)

    assert result.returncode != 0
    assert f"provider layer may not import '{prohibited_module}'" in result.stderr


def test_allowed_domain_and_same_layer_imports_pass(tmp_path: Path) -> None:
    write_layer_module(
        tmp_path,
        "repositories",
        "from atlasrag.domain import errors\n"
        "from atlasrag.repositories import postgres\n",
        "repository_module.py",
    )
    write_layer_module(
        tmp_path,
        "providers",
        "from atlasrag.domain import errors\nfrom atlasrag.providers import cache\n",
        "provider_module.py",
    )

    result = run_checker(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_repository_commit_calls_are_owned_only_by_uow(tmp_path: Path) -> None:
    write_layer_module(tmp_path, "repositories", "connection.commit()\n")

    result = run_checker(tmp_path)

    assert result.returncode != 0
    assert "repository commit is only allowed in uow.py" in result.stderr


def test_uow_commit_and_non_call_commit_references_are_allowed(tmp_path: Path) -> None:
    write_layer_module(
        tmp_path,
        "repositories",
        "connection.commit()\n",
        "uow.py",
    )
    write_layer_module(
        tmp_path,
        "repositories",
        "commit_callback = connection.commit\ncommit(connection)\n",
        "records.py",
    )

    result = run_checker(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
