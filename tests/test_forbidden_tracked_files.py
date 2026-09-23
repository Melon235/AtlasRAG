"""Behavioral tests for the forbidden tracked-file checker."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPOSITORY_ROOT / "scripts" / "check_forbidden_tracked_files.py"


def run_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run Git in a temporary real repository."""
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


def initialize_repository(path: Path) -> None:
    """Initialize a repository with deterministic local identity."""
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    run_git(path, "config", "user.name", "AtlasRAG Tests")
    run_git(path, "config", "user.email", "atlasrag-tests@example.invalid")


def write_file(
    repository: Path, relative_path: str, content: str = "fixture\n"
) -> None:
    """Write a fixture path, creating parent directories as needed."""
    target = repository / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def run_checker(
    repository: Path, *arguments: str, working_directory: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Execute the real checker in a temporary Git repository."""
    return subprocess.run(
        [sys.executable, str(CHECKER), *arguments],
        cwd=working_directory or repository,
        check=False,
        capture_output=True,
        text=True,
    )


def test_allowed_tracked_paths_pass(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    allowed_paths = (
        ".env.example",
        "benchmarks/cases.py",
        "docs/runtime-data-policy.md",
        "src/atlasrag/runtime/worker.py",
        "src/atlasrag/tests_helpers.py",
    )
    for path in allowed_paths:
        write_file(tmp_path, path)
    run_git(tmp_path, "add", *allowed_paths)

    result = run_checker(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "forbidden_path",
    [
        ".env",
        ".env.production",
        ".venv/pyvenv.cfg",
        "venv/pyvenv.cfg",
        "env/pyvenv.cfg",
        "src/atlasrag/__pycache__/module.pyc",
        ".pytest_cache/v/cache/nodeids",
        ".mypy_cache/3.11/cache.json",
        ".ruff_cache/cache",
        ".coverage",
        ".coverage.unit",
        "coverage.xml",
        "htmlcov/index.html",
        "data/documents.json",
        "runtime/state.json",
        "logs/app.log",
        "traces/trace.json",
        "benchmark_data/corpus.json",
        "benchmark_runs/run.json",
        "models/model/config.json",
        "model_cache/cache.bin",
        "postgres_data/database",
        "redis_data/dump.rdb",
        "milvus_data/segment",
        "minio_data/object",
        "etcd_data/member",
        ".idea/workspace.xml",
        ".vscode/settings.json",
        "artifacts/model.pt",
        "artifacts/model.ckpt",
        "artifacts/model.safetensors",
        "artifacts/model.onnx",
        "artifacts/model.gguf",
    ],
)
def test_force_tracked_forbidden_paths_fail(
    tmp_path: Path, forbidden_path: str
) -> None:
    initialize_repository(tmp_path)
    write_file(tmp_path, forbidden_path)
    run_git(tmp_path, "add", "--force", "--", forbidden_path)

    result = run_checker(tmp_path)

    assert result.returncode != 0
    assert f"{forbidden_path}: forbidden tracked artifact" in result.stderr


def test_staged_mode_detects_new_force_staged_forbidden_file(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    write_file(tmp_path, "README.md")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "--quiet", "-m", "initial")
    write_file(tmp_path, ".env.local")
    run_git(tmp_path, "add", "--force", ".env.local")

    result = run_checker(tmp_path, "--staged")

    assert result.returncode != 0
    assert ".env.local: forbidden tracked artifact" in result.stderr


def test_staged_mode_ignores_unstaged_forbidden_file(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    write_file(tmp_path, "README.md")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "--quiet", "-m", "initial")
    write_file(tmp_path, "logs/local.log")

    result = run_checker(tmp_path, "--staged")

    assert result.returncode == 0, result.stdout + result.stderr


def test_default_mode_checks_the_whole_repository_from_nested_cwd(
    tmp_path: Path,
) -> None:
    initialize_repository(tmp_path)
    write_file(tmp_path, "nested/tracked.txt")
    write_file(tmp_path, ".env.local")
    run_git(tmp_path, "add", "nested/tracked.txt")
    run_git(tmp_path, "add", "--force", ".env.local")

    result = run_checker(tmp_path, working_directory=tmp_path / "nested")

    assert result.returncode != 0
    assert ".env.local: forbidden tracked artifact" in result.stderr


def test_staged_mode_checks_the_whole_repository_from_nested_cwd(
    tmp_path: Path,
) -> None:
    initialize_repository(tmp_path)
    write_file(tmp_path, "nested/tracked.txt")
    run_git(tmp_path, "add", "nested/tracked.txt")
    run_git(tmp_path, "commit", "--quiet", "-m", "initial")
    write_file(tmp_path, ".env.local")
    run_git(tmp_path, "add", "--force", ".env.local")

    result = run_checker(tmp_path, "--staged", working_directory=tmp_path / "nested")

    assert result.returncode != 0
    assert ".env.local: forbidden tracked artifact" in result.stderr


def test_non_repository_fails_closed_with_clear_diagnostic(tmp_path: Path) -> None:
    result = run_checker(tmp_path)

    assert result.returncode == 2
    assert "unable to resolve Git repository root:" in result.stderr


def test_control_characters_are_escaped_on_one_diagnostic_line(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    forbidden_path = ".env.bad\n\tname"
    write_file(tmp_path, forbidden_path)
    run_git(tmp_path, "add", "--force", "--", forbidden_path)

    result = run_checker(tmp_path)

    assert result.returncode != 0
    assert result.stderr.splitlines() == [
        r".env.bad\n\tname: forbidden tracked artifact"
    ]


def test_surrogateescaped_byte_is_rendered_deterministically(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    forbidden_path = ".env.bad-\udcff"
    write_file(tmp_path, forbidden_path)
    run_git(tmp_path, "add", "--force", "--", forbidden_path)

    result = subprocess.run(
        [sys.executable, str(CHECKER)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
    )

    assert result.returncode != 0
    assert result.stderr == b".env.bad-\\xff: forbidden tracked artifact\n"


def test_violations_are_sorted_deterministically(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    forbidden_paths = ("logs/z.log", ".env.local", "data/a.json")
    for path in forbidden_paths:
        write_file(tmp_path, path)
    run_git(tmp_path, "add", "--force", "--", *forbidden_paths)

    result = run_checker(tmp_path)

    assert result.returncode != 0
    assert result.stderr.splitlines() == [
        ".env.local: forbidden tracked artifact",
        "data/a.json: forbidden tracked artifact",
        "logs/z.log: forbidden tracked artifact",
    ]
