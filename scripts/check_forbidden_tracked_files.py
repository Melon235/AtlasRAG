#!/usr/bin/env python3
"""Reject forbidden repository artifacts recorded in the Git index.

The default mode checks every tracked path. ``--staged`` checks only paths in
the staged added/copied/modified/renamed diff, making it suitable for a final
pre-commit gate.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT_FORBIDDEN_DIRECTORIES = frozenset(
    {
        ".venv",
        "benchmark_data",
        "benchmark_runs",
        "data",
        "env",
        "etcd_data",
        "logs",
        "milvus_data",
        "minio_data",
        "model_cache",
        "models",
        "postgres_data",
        "redis_data",
        "runtime",
        "traces",
        "venv",
    }
)
CACHE_DIRECTORIES = frozenset(
    {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)
IDE_DIRECTORIES = frozenset({".idea", ".vscode"})
COVERAGE_NAMES = frozenset({"coverage.xml", "htmlcov"})
MODEL_WEIGHT_SUFFIXES = frozenset(
    {".ckpt", ".gguf", ".h5", ".onnx", ".pb", ".pt", ".pth", ".safetensors"}
)


def is_forbidden(path: str) -> bool:
    """Return whether a tracked repository-relative path is forbidden."""
    parsed_path = PurePosixPath(path)
    parts = parsed_path.parts
    if not parts:
        return False

    name = parsed_path.name
    if name != ".env.example" and (name == ".env" or name.startswith(".env.")):
        return True
    if parts[0] in ROOT_FORBIDDEN_DIRECTORIES:
        return True
    if any(part in CACHE_DIRECTORIES | IDE_DIRECTORIES for part in parts):
        return True
    if name.endswith((".pyc", ".pyo")):
        return True
    if name == ".coverage" or name.startswith(".coverage."):
        return True
    if any(part in COVERAGE_NAMES for part in parts):
        return True
    return parsed_path.suffix.lower() in MODEL_WEIGHT_SUFFIXES


def escape_path(path: str) -> str:
    """Render control characters and surrogateescaped bytes on one line."""
    escaped: list[str] = []
    for character in path:
        code_point = ord(character)
        if character == "\\":
            escaped.append("\\\\")
        elif character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif character == "\t":
            escaped.append("\\t")
        elif 0xDC80 <= code_point <= 0xDCFF:
            escaped.append(f"\\x{code_point - 0xDC00:02x}")
        elif code_point < 0x20 or code_point == 0x7F:
            escaped.append(f"\\x{code_point:02x}")
        elif character.isprintable():
            escaped.append(character)
        elif code_point <= 0xFFFF:
            escaped.append(f"\\u{code_point:04x}")
        else:
            escaped.append(f"\\U{code_point:08x}")
    return "".join(escaped)


def git_repository_root() -> tuple[Path | None, str | None]:
    """Resolve the repository root from any working-tree subdirectory."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
        return None, diagnostic or "Git repository root query failed"
    decoded = result.stdout.decode("utf-8", errors="surrogateescape").removesuffix("\n")
    if not decoded:
        return None, "Git returned an empty repository root"
    return Path(decoded), None


def git_paths(staged: bool, repository_root: Path) -> tuple[list[str], str | None]:
    """Read tracked or staged paths from Git without consulting the worktree."""
    command = (
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]
        if staged
        else ["git", "ls-files", "-z"]
    )
    result = subprocess.run(
        command,
        cwd=repository_root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
        return [], diagnostic or "Git index query failed"
    decoded = result.stdout.decode("utf-8", errors="surrogateescape")
    return [path for path in decoded.split("\0") if path], None


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--staged",
        action="store_true",
        help="check only staged added/copied/modified/renamed paths",
    )
    return parser.parse_args()


def main() -> int:
    """Run the Git-index policy check and return a process exit code."""
    arguments = parse_args()
    repository_root, root_error = git_repository_root()
    if root_error is not None or repository_root is None:
        print(
            f"unable to resolve Git repository root: {root_error}",
            file=sys.stderr,
        )
        return 2

    paths, git_error = git_paths(arguments.staged, repository_root)
    if git_error is not None:
        print(f"unable to inspect Git paths: {git_error}", file=sys.stderr)
        return 2

    violations = sorted(path for path in paths if is_forbidden(path))
    for path in violations:
        print(f"{escape_path(path)}: forbidden tracked artifact", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
