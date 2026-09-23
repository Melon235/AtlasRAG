#!/usr/bin/env python3
"""Reject production imports from the test and benchmark trees."""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_TOP_LEVEL_MODULES = frozenset({"benchmarks", "tests"})


@dataclass(frozen=True, order=True)
class Diagnostic:
    """A deterministic diagnostic emitted by the checker."""

    path: str
    line: int
    message: str

    def render(self) -> str:
        """Render the diagnostic in compiler-style form."""
        return f"{self.path}:{self.line}: {self.message}"


def is_forbidden(module_name: str) -> bool:
    """Return whether a module starts with a prohibited top-level package."""
    return module_name.partition(".")[0] in FORBIDDEN_TOP_LEVEL_MODULES


def scan_file(path: Path, root: Path) -> list[Diagnostic]:
    """Parse one production module and return syntax/import diagnostics."""
    relative_path = path.relative_to(root).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative_path)
    except SyntaxError as error:
        line = error.lineno or 1
        message = error.msg or "invalid syntax"
        return [Diagnostic(relative_path, line, f"syntax error: {message}")]
    except (OSError, UnicodeError) as error:
        return [Diagnostic(relative_path, 1, f"unable to inspect file: {error}")]

    diagnostics: list[Diagnostic] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported_name in node.names:
                if is_forbidden(imported_name.name):
                    diagnostics.append(
                        Diagnostic(
                            relative_path,
                            node.lineno,
                            f"forbidden import '{imported_name.name}'",
                        )
                    )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
            and is_forbidden(node.module)
        ):
            diagnostics.append(
                Diagnostic(
                    relative_path,
                    node.lineno,
                    f"forbidden import '{node.module}'",
                )
            )
    return diagnostics


def check_repository(root: Path) -> list[Diagnostic]:
    """Inspect every Python module below the production package."""
    package_root = root / "src" / "atlasrag"
    if not package_root.is_dir():
        return [
            Diagnostic(
                "src/atlasrag",
                1,
                "production package directory is missing",
            )
        ]

    diagnostics: list[Diagnostic] = []
    for path in sorted(package_root.rglob("*.py")):
        diagnostics.extend(scan_file(path, root))
    return sorted(diagnostics)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root to inspect (defaults to the script's repository)",
    )
    return parser.parse_args()


def main() -> int:
    """Run the boundary check and return a process exit code."""
    arguments = parse_args()
    diagnostics = check_repository(arguments.root.resolve())
    for diagnostic in diagnostics:
        print(diagnostic.render(), file=sys.stderr)
    return 1 if diagnostics else 0


if __name__ == "__main__":
    raise SystemExit(main())
