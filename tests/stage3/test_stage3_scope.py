"""Frozen Stage 3 dependency, package, and migration boundaries."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = REPOSITORY_ROOT / "pyproject.toml"


def _pyproject() -> dict[str, object]:
    return tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))


def _dependency_name(requirement: str) -> str:
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    assert match is not None, f"invalid dependency requirement: {requirement!r}"
    return match.group(1).casefold().replace("_", "-")


def test_parser_dependencies_are_bounded_non_default_groups() -> None:
    document = _pyproject()
    groups = document.get("dependency-groups")
    assert isinstance(groups, dict)

    assert groups.get("parsing-docling") == [
        "docling-slim[format-office]>=2.135.0,<2.136.0"
    ]
    assert groups.get("parsing-mineru") == ["mineru>=4.0.11,<4.1.0"]

    tool = document.get("tool", {})
    assert isinstance(tool, dict)
    uv = tool.get("uv", {})
    assert isinstance(uv, dict)
    default_groups = uv.get("default-groups", ["dev"])
    assert isinstance(default_groups, list)
    assert "parsing-docling" not in default_groups
    assert "parsing-mineru" not in default_groups


def test_parser_dependencies_do_not_leak_into_runtime_or_dev_groups() -> None:
    document = _pyproject()
    project = document.get("project")
    groups = document.get("dependency-groups")
    assert isinstance(project, dict)
    assert isinstance(groups, dict)
    runtime = project.get("dependencies")
    dev = groups.get("dev")
    assert isinstance(runtime, list)
    assert isinstance(dev, list)

    forbidden_direct = {
        "docling",
        "docling-slim",
        "mineru",
        "langgraph",
        "torch",
        "transformers",
        "flagembedding",
    }
    for context, requirements in (("runtime", runtime), ("dev", dev)):
        names = {_dependency_name(item) for item in requirements}
        assert not names & forbidden_direct, (
            f"{context} leaked {names & forbidden_direct}"
        )


def test_stage3_package_roots_are_regular_packages() -> None:
    roots = (
        "src/atlasrag/domain/ingestion",
        "src/atlasrag/providers/parsing",
        "src/atlasrag/services/ingestion",
    )
    for relative_path in roots:
        package = REPOSITORY_ROOT / relative_path
        assert package.is_dir(), f"missing Stage 3 package: {relative_path}"
        assert (package / "__init__.py").is_file()


def test_stage3_adds_no_database_migration() -> None:
    migrations = sorted(
        path.name
        for path in (REPOSITORY_ROOT / "migrations" / "versions").glob("*.py")
        if path.name != "__init__.py"
    )

    assert migrations == ["20260928_0001_stage2_persistence.py"]
