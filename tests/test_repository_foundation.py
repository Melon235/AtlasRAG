"""Stage 0 repository foundation checks."""

from __future__ import annotations

import importlib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_package_imports() -> None:
    """The AtlasRAG package is importable from the src layout."""
    assert importlib.import_module("atlasrag") is not None


def test_repository_has_architecture_manual() -> None:
    """The authoritative Architecture Manual is stored at its frozen path."""
    manual = (
        REPOSITORY_ROOT
        / "docs"
        / "architecture"
        / "AtlasRAG_Complete_Final_Architecture_Design_Manual.md"
    )

    assert manual.is_file()


def test_repository_has_stage_zero_structure() -> None:
    """The repository exposes the directories frozen for Stage 0."""
    required_directories = (
        "benchmarks",
        "deploy/local",
        "docs/architecture",
        "docs/stages",
        "scripts",
        "src/atlasrag/application",
        "src/atlasrag/config",
        "src/atlasrag/domain",
        "src/atlasrag/graphs",
        "src/atlasrag/observability",
        "src/atlasrag/providers",
        "src/atlasrag/repositories",
        "src/atlasrag/runtime",
        "src/atlasrag/services",
        "tests/contract",
        "tests/integration",
        "tests/unit",
    )

    missing = [
        relative_path
        for relative_path in required_directories
        if not (REPOSITORY_ROOT / relative_path).is_dir()
    ]

    assert not missing, f"Missing Stage 0 directories: {missing}"
