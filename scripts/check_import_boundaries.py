#!/usr/bin/env python3
"""Enforce static dependency and transaction boundaries in production code."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_IMPORT_PREFIXES = (
    "atlasrag.benchmarks",
    "atlasrag.tests",
    "benchmarks",
    "tests",
)
LAYER_FORBIDDEN_PREFIXES = {
    "repositories": (
        "atlasrag.benchmarks",
        "atlasrag.graphs",
        "atlasrag.providers",
    ),
    "providers": (
        "atlasrag.benchmarks",
        "atlasrag.graphs",
        "atlasrag.repositories",
    ),
}


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
    """Return whether a module belongs to a prohibited support package."""
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.")
        for prefix in FORBIDDEN_IMPORT_PREFIXES
    )


def _architecture_layer(relative_path: str) -> str | None:
    parts = Path(relative_path).parts
    if len(parts) >= 3 and parts[:2] == ("src", "atlasrag"):
        candidate = parts[2]
        if candidate in LAYER_FORBIDDEN_PREFIXES:
            return candidate
    return None


def _package_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root / "src").with_suffix("")
    parts = list(relative.parts)
    parts.pop()
    return ".".join(parts)


def _import_targets(
    node: ast.Import | ast.ImportFrom, path: Path, root: Path
) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(imported.name for imported in node.names)

    if node.level == 0:
        base = node.module or ""
    else:
        relative_name = "." * node.level + (node.module or "")
        try:
            base = importlib.util.resolve_name(relative_name, _package_name(path, root))
        except (ImportError, ValueError):
            return ()

    targets = [base] if base else []
    if base:
        targets.extend(
            f"{base}.{imported.name}" for imported in node.names if imported.name != "*"
        )
    return tuple(targets)


def _matching_forbidden_prefix(layer: str, module_name: str) -> str | None:
    for prefix in LAYER_FORBIDDEN_PREFIXES[layer]:
        if module_name == prefix or module_name.startswith(f"{prefix}."):
            return prefix
    return None


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
    layer = _architecture_layer(relative_path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            targets = _import_targets(node, path, root)
            if layer is not None:
                forbidden_prefix = next(
                    (
                        prefix
                        for target in targets
                        if (prefix := _matching_forbidden_prefix(layer, target))
                        is not None
                    ),
                    None,
                )
                if forbidden_prefix is not None:
                    layer_name = "repository" if layer == "repositories" else "provider"
                    diagnostics.append(
                        Diagnostic(
                            relative_path,
                            node.lineno,
                            f"{layer_name} layer may not import '{forbidden_prefix}'",
                        )
                    )
                    continue
            global_target = next(
                (target for target in targets if is_forbidden(target)), None
            )
            if global_target is not None:
                diagnostics.append(
                    Diagnostic(
                        relative_path,
                        node.lineno,
                        f"forbidden import '{global_target}'",
                    )
                )
        elif (
            layer == "repositories"
            and path.name != "uow.py"
            and isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "commit"
        ):
            diagnostics.append(
                Diagnostic(
                    relative_path,
                    node.lineno,
                    "repository commit is only allowed in uow.py",
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
