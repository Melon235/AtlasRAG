"""Frozen Stage 1 contracts with explicit Stage 2 infrastructure exceptions."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOT = REPOSITORY_ROOT / "src" / "atlasrag"
GRAPH_ROOT = PRODUCTION_ROOT / "graphs"

# These integrations belong to later stages. DeepSeek may be reached through
# native, LangChain, or OpenAI-compatible clients; SearXNG is MCP-backed.
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "benchmarks",
        "aiohttp",
        "builtins",
        "deepseek",
        "docling",
        "httpx",
        "importlib",
        "langchain_deepseek",
        "langchain_openai",
        "langgraph",
        "magic_pdf",
        "mcp",
        "mineru",
        "openai",
        "psycopg",
        "psycopg2",
        "pymilvus",
        "redis",
        "requests",
        "runpy",
        "searxng",
        "searxng_client",
        "socket",
        "subprocess",
        "tests",
        "torch",
        "urllib3",
        "websocket",
        "websockets",
    }
)
FORBIDDEN_IMPORT_MODULES = frozenset({"http.client", "urllib.request"})
ALLOWED_NON_STDLIB_IMPORT_ROOTS = frozenset({"atlasrag", "pydantic"})
FORBIDDEN_IMPORT_PREFIXES = (
    "deepseek",
    "docling",
    "mineru",
    "psycopg",
    "searxng",
    "torch",
)
EXPECTED_DECLARED_DISTRIBUTIONS = {
    "project.dependencies": frozenset(
        {"pydantic", "alembic", "psycopg", "redis", "pymilvus"}
    ),
    "dependency-groups.dev": frozenset(
        {"mypy", "pytest", "pytest-cov", "pytest-asyncio", "ruff"}
    ),
    "build-system.requires": frozenset({"setuptools"}),
}
EXPECTED_LOCKED_DISTRIBUTIONS = frozenset(
    {
        "alembic",
        "annotated-types",
        "async-timeout",
        "atlasrag",
        "cachetools",
        "certifi",
        "charset-normalizer",
        "colorama",
        "coverage",
        "grpcio",
        "idna",
        "iniconfig",
        "librt",
        "mako",
        "markupsafe",
        "mypy",
        "mypy-extensions",
        "numpy",
        "orjson",
        "packaging",
        "pandas",
        "pathspec",
        "pluggy",
        "protobuf",
        "psycopg",
        "psycopg-binary",
        "psycopg-pool",
        "pydantic",
        "pydantic-core",
        "pygments",
        "pymilvus",
        "pytest",
        "pytest-asyncio",
        "pytest-cov",
        "python-dateutil",
        "python-dotenv",
        "redis",
        "requests",
        "ruff",
        "six",
        "sqlalchemy",
        "tomli",
        "typing-extensions",
        "typing-inspection",
        "tzdata",
        "urllib3",
    }
)

EXPECTED_GRAPH_FILES = frozenset(
    {
        "__init__.py",
        "state/__init__.py",
        "state/answer.py",
        "state/local_evidence.py",
        "state/rag_core.py",
        "state/retrieval.py",
        "state/web_evidence.py",
    }
)
EXPECTED_PRODUCTION_FILES = frozenset(
    {
        "__init__.py",
        "_canonical.py",
        "application/__init__.py",
        "application/hashing.py",
        "config/__init__.py",
        "config/fingerprint.py",
        "config/models.py",
        "domain/__init__.py",
        "domain/answer.py",
        "domain/base.py",
        "domain/enums.py",
        "domain/errors.py",
        "domain/evidence.py",
        "domain/requests.py",
        "domain/results.py",
        "domain/retrieval.py",
        "domain/trace.py",
        "domain/web.py",
        "graphs/__init__.py",
        "graphs/state/__init__.py",
        "graphs/state/answer.py",
        "graphs/state/local_evidence.py",
        "graphs/state/rag_core.py",
        "graphs/state/retrieval.py",
        "graphs/state/web_evidence.py",
        "observability/__init__.py",
        "providers/__init__.py",
        "repositories/__init__.py",
        "runtime/__init__.py",
        "services/__init__.py",
    }
)
STAGE2_PRODUCTION_FILES = frozenset(
    {
        "repositories/postgres/__init__.py",
        "repositories/postgres/records.py",
    }
)
EXPECTED_PRODUCTION_FILES |= STAGE2_PRODUCTION_FILES
STAGE2_DRIVER_LAYERS = {
    "psycopg": "repositories/postgres/",
    "psycopg_pool": "repositories/postgres/",
    "redis": "providers/cache/",
    "pymilvus": "providers/index/",
}
STUB_ONLY_PACKAGES = (
    "observability",
    "providers",
    "repositories",
    "runtime",
    "services",
)
ALLOWED_BOUNDARY_INITIALIZERS = frozenset(
    f"{package}/__init__.py" for package in STUB_ONLY_PACKAGES
)
FORBIDDEN_IMPLEMENTATION_PATH_PARTS = frozenset(
    {"backend", "backends", "business", "client", "clients", "provider", "providers"}
)
_DISTRIBUTION_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_PATH_TOKEN = re.compile(r"[A-Za-z0-9]+")


def _normalized_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.casefold())


def _is_forbidden_import(module_name: str, path: Path | None = None) -> bool:
    normalized = module_name.casefold()
    root = normalized.partition(".")[0].replace("-", "_")
    if path is not None and root in STAGE2_DRIVER_LAYERS:
        parts = path.parts
        relative = (
            Path(*parts[parts.index("atlasrag") + 1 :]).as_posix()
            if "atlasrag" in parts
            else path.as_posix()
        )
        return not relative.startswith(STAGE2_DRIVER_LAYERS[root])
    explicitly_forbidden = (
        root in FORBIDDEN_IMPORT_ROOTS
        or root.startswith(FORBIDDEN_IMPORT_PREFIXES)
        or any(
            normalized == forbidden or normalized.startswith(f"{forbidden}.")
            for forbidden in FORBIDDEN_IMPORT_MODULES
        )
    )
    return explicitly_forbidden or (
        root not in sys.stdlib_module_names
        and root not in ALLOWED_NON_STDLIB_IMPORT_ROOTS
    )


def _dynamic_import_aliases(
    tree: ast.Module,
) -> tuple[set[str], set[str], set[str], set[str]]:
    importlib_aliases: set[str] = set()
    import_module_aliases: set[str] = set()
    builtins_aliases: set[str] = set()
    builtin_import_aliases = {"__import__"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name == "importlib" or imported.name.startswith(
                    "importlib."
                ):
                    importlib_aliases.add(imported.asname or "importlib")
                elif imported.name == "builtins":
                    builtins_aliases.add(imported.asname or imported.name)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == "importlib"
        ):
            for imported in node.names:
                if imported.name == "import_module":
                    import_module_aliases.add(imported.asname or imported.name)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == "builtins"
        ):
            for imported in node.names:
                if imported.name == "__import__":
                    builtin_import_aliases.add(imported.asname or imported.name)

    return (
        importlib_aliases,
        import_module_aliases,
        builtins_aliases,
        builtin_import_aliases,
    )


def _dynamic_import_reference_name(
    tree: ast.Module, expression: ast.expr
) -> str | None:
    (
        importlib_aliases,
        import_module_aliases,
        builtins_aliases,
        builtin_import_aliases,
    ) = _dynamic_import_aliases(tree)
    if (
        isinstance(expression, ast.Name)
        and isinstance(expression.ctx, ast.Load)
        and expression.id in (import_module_aliases | builtin_import_aliases)
    ):
        return expression.id
    if not isinstance(expression, ast.Attribute) or not isinstance(
        expression.ctx, ast.Load
    ):
        return None
    if not isinstance(expression.value, ast.Name):
        return None
    if expression.attr == "import_module" and expression.value.id in importlib_aliases:
        return f"{expression.value.id}.{expression.attr}"
    if expression.attr == "__import__" and expression.value.id in builtins_aliases:
        return f"{expression.value.id}.{expression.attr}"
    return None


def _imported_module_names(node: ast.Import | ast.ImportFrom) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(imported.name for imported in node.names)
    if node.level != 0 or node.module is None:
        return ()
    return (node.module,) + tuple(
        f"{node.module}.{imported.name}"
        for imported in node.names
        if imported.name != "*"
    )


def _source_import_violations(source: str, path: Path) -> tuple[str, ...]:
    tree = ast.parse(source, filename=path.as_posix())
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for module_name in _imported_module_names(node):
                if _is_forbidden_import(module_name, path):
                    violations.append(
                        f"{path.as_posix()}:{node.lineno}: forbidden import "
                        f"{module_name!r}"
                    )
        elif isinstance(node, (ast.Name, ast.Attribute)):
            reference_name = _dynamic_import_reference_name(tree, node)
            if reference_name is not None:
                violations.append(
                    f"{path.as_posix()}:{node.lineno}: dynamic import "
                    f"callable {reference_name!r} is forbidden"
                )
    return tuple(violations)


def _production_import_violations(
    production_root: Path, repository_root: Path
) -> tuple[str, ...]:
    violations: list[str] = []
    for path in sorted(production_root.rglob("*.py")):
        relative_path = path.relative_to(repository_root)
        violations.extend(
            _source_import_violations(path.read_text(encoding="utf-8"), relative_path)
        )
    return tuple(violations)


def _production_manifest_violations(production_root: Path) -> tuple[str, ...]:
    actual_files = frozenset(
        path.relative_to(production_root).as_posix()
        for path in production_root.rglob("*.py")
    )
    violations: list[str] = []
    if unexpected := sorted(actual_files - EXPECTED_PRODUCTION_FILES):
        violations.append(f"unexpected production files: {unexpected}")
    if missing := sorted(EXPECTED_PRODUCTION_FILES - actual_files):
        violations.append(f"missing production files: {missing}")
    return tuple(violations)


def _dependency_names(specifications: object, context: str) -> frozenset[str]:
    assert isinstance(specifications, list), f"{context} must be a list"
    names: set[str] = set()
    for specification in specifications:
        assert isinstance(specification, str), f"{context} entries must be strings"
        match = _DISTRIBUTION_NAME.match(specification)
        assert match is not None, f"unable to parse dependency: {specification!r}"
        names.add(_normalized_distribution_name(match.group(1)))
    return frozenset(names)


def _declared_dependency_groups(
    pyproject_path: Path,
) -> dict[str, frozenset[str]]:
    document = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = document.get("project", {})
    dependency_groups = document.get("dependency-groups", {})
    build_system = document.get("build-system", {})
    assert isinstance(project, dict)
    assert isinstance(dependency_groups, dict)
    assert isinstance(build_system, dict)

    groups = {
        "project.dependencies": _dependency_names(
            project.get("dependencies", []), "project.dependencies"
        ),
        "build-system.requires": _dependency_names(
            build_system.get("requires", []), "build-system.requires"
        ),
    }

    optional = project.get("optional-dependencies", {})
    assert isinstance(optional, dict)
    for group_name, specifications in optional.items():
        assert isinstance(group_name, str)
        context = f"project.optional-dependencies.{group_name}"
        groups[context] = _dependency_names(specifications, context)

    for group_name, specifications in dependency_groups.items():
        assert isinstance(group_name, str)
        context = f"dependency-groups.{group_name}"
        groups[context] = _dependency_names(specifications, context)

    return groups


def _manifest_delta(
    actual: dict[str, frozenset[str]], expected: dict[str, frozenset[str]]
) -> list[str]:
    violations: list[str] = []
    for context in sorted(actual.keys() | expected.keys()):
        actual_names = actual.get(context, frozenset())
        expected_names = expected.get(context, frozenset())
        violations.extend(
            f"{context}: unexpected {name}"
            for name in sorted(actual_names - expected_names)
        )
        violations.extend(
            f"{context}: missing {name}"
            for name in sorted(expected_names - actual_names)
        )
    return violations


def _locked_dependency_names(lock_path: Path) -> tuple[str, ...]:
    document = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages = document.get("package", [])
    assert isinstance(packages, list)

    names: list[str] = []
    for package in packages:
        assert isinstance(package, dict)
        name = package.get("name")
        assert isinstance(name, str)
        names.append(name)
    return tuple(names)


def _dependency_manifest_violations(
    pyproject_path: Path, lock_path: Path
) -> dict[str, list[str]]:
    locked = {
        _normalized_distribution_name(name)
        for name in _locked_dependency_names(lock_path)
    }
    violations = {
        pyproject_path.name: _manifest_delta(
            _declared_dependency_groups(pyproject_path),
            EXPECTED_DECLARED_DISTRIBUTIONS,
        ),
        lock_path.name: [
            *(
                f"unexpected {name}"
                for name in sorted(locked - EXPECTED_LOCKED_DISTRIBUTIONS)
            ),
            *(
                f"missing {name}"
                for name in sorted(EXPECTED_LOCKED_DISTRIBUTIONS - locked)
            ),
        ],
    }
    return {source: names for source, names in violations.items() if names}


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _stub_package_violations(production_root: Path) -> tuple[str, ...]:
    violations: list[str] = []
    for package_name in STUB_ONLY_PACKAGES:
        package_root = production_root / package_name
        actual_files = {
            path.relative_to(package_root).as_posix()
            for path in package_root.rglob("*.py")
        }
        allowed_files = {"__init__.py"} | {
            path.removeprefix(f"{package_name}/")
            for path in STAGE2_PRODUCTION_FILES
            if path.startswith(f"{package_name}/")
        }
        if unexpected := actual_files - allowed_files:
            violations.append(
                f"{package_name}: unexpected package files: {sorted(unexpected)}"
            )

        initializer = package_root / "__init__.py"
        if not initializer.is_file():
            continue
        relative_path = initializer.relative_to(production_root).as_posix()
        tree = ast.parse(
            initializer.read_text(encoding="utf-8"), filename=relative_path
        )
        if len(tree.body) != 1 or not _is_docstring(tree.body[0]):
            violations.append(
                f"{relative_path}: initializer must contain only a module docstring"
            )
    return tuple(violations)


def test_production_python_file_manifest_is_frozen() -> None:
    violations = _production_manifest_violations(PRODUCTION_ROOT)

    assert not violations, "Stage 1 production manifest changed:\n" + "\n".join(
        violations
    )


def test_production_has_no_deferred_static_or_dynamic_imports() -> None:
    violations = _production_import_violations(PRODUCTION_ROOT, REPOSITORY_ROOT)

    assert not violations, "deferred Stage 1 imports found:\n" + "\n".join(
        sorted(violations)
    )


def test_dependency_manifests_have_no_deferred_runtime_packages() -> None:
    violations = _dependency_manifest_violations(
        REPOSITORY_ROOT / "pyproject.toml", REPOSITORY_ROOT / "uv.lock"
    )

    assert not violations, f"deferred Stage 1 dependencies found: {violations}"


def test_graph_package_contains_only_declarative_state_modules() -> None:
    actual_files = frozenset(
        path.relative_to(GRAPH_ROOT).as_posix() for path in GRAPH_ROOT.rglob("*.py")
    )
    assert actual_files == EXPECTED_GRAPH_FILES, (
        f"unexpected graph files: {sorted(actual_files - EXPECTED_GRAPH_FILES)}; "
        f"missing graph files: {sorted(EXPECTED_GRAPH_FILES - actual_files)}"
    )

    violations: list[str] = []
    for path in sorted(GRAPH_ROOT.rglob("*.py")):
        relative_path = path.relative_to(REPOSITORY_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative_path)
        is_state_schema = path.parent.name == "state" and path.name != "__init__.py"

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                violations.append(
                    f"{relative_path}:{node.lineno}: executable function is not declarative State"
                )
            elif isinstance(node, ast.Call):
                violations.append(
                    f"{relative_path}:{node.lineno}: call expression is not declarative State"
                )

        if is_state_schema:
            for node in tree.body:
                if not isinstance(
                    node, (ast.Import, ast.ImportFrom, ast.ClassDef)
                ) and not _is_docstring(node):
                    violations.append(
                        f"{relative_path}:{node.lineno}: unexpected top-level "
                        f"{type(node).__name__}"
                    )
                if isinstance(node, ast.ClassDef):
                    if not any(
                        isinstance(base, ast.Name) and base.id == "TypedDict"
                        for base in node.bases
                    ):
                        violations.append(
                            f"{relative_path}:{node.lineno}: {node.name} is not a TypedDict"
                        )
                    for member in node.body:
                        if not isinstance(member, ast.AnnAssign) and not _is_docstring(
                            member
                        ):
                            violations.append(
                                f"{relative_path}:{member.lineno}: {node.name} contains "
                                f"non-declarative {type(member).__name__}"
                            )

    assert not violations, "graph execution/business logic found:\n" + "\n".join(
        sorted(violations)
    )


def test_stub_only_packages_remain_docstring_only() -> None:
    violations = _stub_package_violations(PRODUCTION_ROOT)

    assert not violations, (
        "Stage 1 implementation package is no longer a stub:\n" + "\n".join(violations)
    )


def test_no_backend_client_provider_or_business_modules_exist() -> None:
    violations: list[str] = []
    for path in sorted(PRODUCTION_ROOT.rglob("*.py")):
        relative_path = path.relative_to(PRODUCTION_ROOT).as_posix()
        if relative_path in ALLOWED_BOUNDARY_INITIALIZERS | STAGE2_PRODUCTION_FILES:
            continue
        tokens = {
            token.casefold()
            for part in path.relative_to(PRODUCTION_ROOT).parts
            for token in _PATH_TOKEN.findall(Path(part).stem)
        }
        if forbidden := tokens & FORBIDDEN_IMPLEMENTATION_PATH_PARTS:
            violations.append(f"{relative_path}: {sorted(forbidden)}")

    assert not violations, "deferred implementation modules found:\n" + "\n".join(
        violations
    )


@pytest.mark.parametrize(
    "source",
    [
        'import importlib\nimportlib.import_module("json")',
        'import importlib as loader\nloader.import_module("json")',
        'import importlib.util\nimportlib.import_module("json")',
        'from importlib import import_module\nimport_module("json")',
        'from importlib import import_module as load\nload("json")',
        '__import__("json")',
        'import builtins\nbuiltins.__import__("json")',
        'import builtins as runtime\nruntime.__import__("json")',
        'from builtins import __import__ as load\nload("json")',
        'import importlib\nmodule_name = "json"\nimportlib.import_module(module_name)',
        'module_name = "json"\n__import__(module_name)',
        'import importlib\nload = importlib.import_module\nload("json")',
        (
            "from importlib import import_module\n"
            'module_name = "json"\nload = import_module\nload(module_name)'
        ),
        'module_name = "json"\nload = __import__\nload(module_name)',
        (
            "import builtins\n"
            'module_name = "json"\nload = builtins.__import__\nload(module_name)'
        ),
    ],
    ids=[
        "importlib",
        "importlib-alias",
        "importlib-submodule-binding",
        "from-importlib",
        "from-importlib-alias",
        "builtin",
        "builtins-module",
        "builtins-module-alias",
        "from-builtins-alias",
        "nonliteral-importlib",
        "nonliteral-builtin",
        "assigned-importlib-callable",
        "assigned-from-import-callable",
        "assigned-builtin-callable",
        "assigned-builtins-attribute",
    ],
)
def test_dynamic_imports_are_rejected_fail_closed(source: str) -> None:
    violations = _source_import_violations(source, Path("adversarial.py"))

    assert violations
    assert any("dynamic import" in violation for violation in violations)


@pytest.mark.parametrize(
    "source",
    [
        (
            "import importlib\n"
            'load = getattr(importlib, "import_module")\n'
            'load("urllib.request")'
        ),
        (
            "import importlib.util\n"
            'spec = importlib.util.spec_from_file_location("plugin", "/tmp/plugin.py")\n'
            "assert spec is not None and spec.loader is not None\n"
            "spec.loader.exec_module(object())"
        ),
    ],
    ids=["getattr-import-module", "importlib-util-loader"],
)
def test_dynamic_loader_modules_are_rejected(source: str) -> None:
    violations = _source_import_violations(source, Path("adversarial.py"))

    assert violations
    assert any("forbidden import" in violation for violation in violations)


@pytest.mark.parametrize(
    "source",
    [
        "import requests",
        "import httpx",
        "import httpcore",
        "import aiohttp",
        "import urllib3",
        "import urllib.request",
        "from urllib import request",
        "import http.client",
        "from http import client",
        "import socket",
        "import websocket",
        "import websockets",
        "import subprocess",
    ],
    ids=[
        "requests",
        "httpx",
        "httpcore",
        "aiohttp",
        "urllib3",
        "urllib-request",
        "from-urllib-request",
        "http-client",
        "from-http-client",
        "socket",
        "websocket",
        "websockets",
        "subprocess",
    ],
)
def test_network_and_process_imports_are_rejected(source: str) -> None:
    violations = _source_import_violations(source, Path("adversarial.py"))

    assert violations
    assert all("forbidden import" in violation for violation in violations)


def test_urllib_parse_imports_remain_allowed() -> None:
    source = (
        "import json\n"
        "import urllib.parse\n"
        "from urllib import parse\n"
        "from pydantic import BaseModel\n"
        "from atlasrag.domain.base import FrozenModel"
    )

    assert not _source_import_violations(source, Path("parsing.py"))


def test_ordinary_compile_and_invoke_calls_remain_allowed() -> None:
    source = "builder.compile()\nrunner.invoke()"

    assert not _source_import_violations(source, Path("ordinary_calls.py"))


def test_production_python_manifest_rejects_new_adapter_module(tmp_path: Path) -> None:
    production_root = tmp_path / "atlasrag"
    for relative_path in EXPECTED_PRODUCTION_FILES:
        path = production_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('"""Test module."""\n', encoding="utf-8")
    adapter_path = production_root / "adapters" / "search.py"
    adapter_path.parent.mkdir(parents=True)
    adapter_path.write_text('"""Deferred search adapter."""\n', encoding="utf-8")

    violations = _production_manifest_violations(production_root)

    assert violations == ("unexpected production files: ['adapters/search.py']",)


def _write_stub_initializers(production_root: Path) -> None:
    for package_name in (
        "observability",
        "providers",
        "repositories",
        "runtime",
        "services",
    ):
        initializer = production_root / package_name / "__init__.py"
        initializer.parent.mkdir(parents=True, exist_ok=True)
        initializer.write_text('"""Boundary package."""\n', encoding="utf-8")


@pytest.mark.parametrize(
    ("package_name", "implementation"),
    [
        ("providers", "def provide() -> None:\n    pass"),
        ("repositories", "class Repository:\n    pass"),
        ("runtime", "configure()"),
        ("observability", "import logging"),
    ],
    ids=["function", "class", "call", "import"],
)
def test_stub_initializers_reject_executable_or_imported_content(
    tmp_path: Path,
    package_name: str,
    implementation: str,
) -> None:
    production_root = tmp_path / "atlasrag"
    _write_stub_initializers(production_root)
    initializer = production_root / package_name / "__init__.py"
    initializer.write_text(
        f'"""Boundary package."""\n\n{implementation}\n', encoding="utf-8"
    )

    violations = _stub_package_violations(production_root)

    assert any(f"{package_name}/__init__.py" in item for item in violations)


def test_stub_packages_reject_provider_implementation_module(tmp_path: Path) -> None:
    production_root = tmp_path / "atlasrag"
    _write_stub_initializers(production_root)
    provider_module = production_root / "providers" / "search.py"
    provider_module.write_text("class SearchProvider:\n    pass\n", encoding="utf-8")

    violations = _stub_package_violations(production_root)

    assert any("providers" in item and "search.py" in item for item in violations)


@pytest.mark.parametrize("dependency", ["psycopg", "websocket-client", "httpcore"])
def test_build_system_requires_are_checked_without_lockfile_mutation(
    tmp_path: Path, dependency: str
) -> None:
    source = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    original = 'requires = ["setuptools>=83.0.0,<84.0.0"]'
    replacement = f'requires = ["setuptools>=83.0.0,<84.0.0", "{dependency}>=1.0.0"]'
    assert original in source
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(source.replace(original, replacement), encoding="utf-8")

    violations = _dependency_manifest_violations(
        pyproject_path, REPOSITORY_ROOT / "uv.lock"
    )

    assert violations == {
        "pyproject.toml": [f"build-system.requires: unexpected {dependency}"]
    }


def test_dependency_manifest_rejects_missing_runtime_dependency(
    tmp_path: Path,
) -> None:
    source = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    original = '"pydantic>=2.12.0,<3.0.0"'
    assert original in source
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(
        source.replace(original + ",", "").replace(original, ""), encoding="utf-8"
    )

    violations = _dependency_manifest_violations(
        pyproject_path, REPOSITORY_ROOT / "uv.lock"
    )

    assert violations == {"pyproject.toml": ["project.dependencies: missing pydantic"]}


def test_dependency_manifest_rejects_dev_dependency_promoted_to_runtime(
    tmp_path: Path,
) -> None:
    source = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    original = '"pydantic>=2.12.0,<3.0.0"'
    replacement = '"pydantic>=2.12.0,<3.0.0", "pytest>=9.0.3,<10.0.0"'
    assert original in source
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(source.replace(original, replacement), encoding="utf-8")

    violations = _dependency_manifest_violations(
        pyproject_path, REPOSITORY_ROOT / "uv.lock"
    )

    assert violations == {"pyproject.toml": ["project.dependencies: unexpected pytest"]}


def test_dependency_manifest_rejects_missing_locked_distribution(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(
        "\n".join(
            f'[[package]]\nname = "{name}"'
            for name in sorted(EXPECTED_LOCKED_DISTRIBUTIONS - {"colorama"})
        ),
        encoding="utf-8",
    )

    violations = _dependency_manifest_violations(
        REPOSITORY_ROOT / "pyproject.toml", lock_path
    )

    assert violations == {"uv.lock": ["missing colorama"]}
