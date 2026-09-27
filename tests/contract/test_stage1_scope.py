"""Regression tests that keep Stage 1 free of deferred runtime implementation."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOT = REPOSITORY_ROOT / "src" / "atlasrag"
GRAPH_ROOT = PRODUCTION_ROOT / "graphs"

# These integrations belong to later stages. DeepSeek may be reached through
# native, LangChain, or OpenAI-compatible clients; SearXNG is MCP-backed.
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "benchmarks",
        "deepseek",
        "docling",
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
        "searxng",
        "searxng_client",
        "tests",
        "torch",
    }
)
FORBIDDEN_IMPORT_PREFIXES = (
    "deepseek",
    "docling",
    "mineru",
    "psycopg",
    "searxng",
    "torch",
)
FORBIDDEN_DISTRIBUTION_NAMES = frozenset(
    name.replace("_", "-")
    for name in FORBIDDEN_IMPORT_ROOTS
    if name not in {"benchmarks", "tests"}
)
FORBIDDEN_DISTRIBUTION_PREFIXES = tuple(
    prefix.replace("_", "-") for prefix in FORBIDDEN_IMPORT_PREFIXES
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
STUB_ONLY_PACKAGES = ("providers", "repositories", "runtime", "services")
ALLOWED_BOUNDARY_INITIALIZERS = frozenset(
    f"{package}/__init__.py" for package in STUB_ONLY_PACKAGES
)
FORBIDDEN_IMPLEMENTATION_PATH_PARTS = frozenset(
    {"backend", "backends", "business", "client", "clients", "provider", "providers"}
)
GRAPH_EXECUTION_FUNCTIONS = frozenset(
    {
        "CompiledStateGraph",
        "MessageGraph",
        "StateGraph",
        "ToolNode",
        "create_react_agent",
    }
)
GRAPH_EXECUTION_METHODS = frozenset(
    {
        "abatch",
        "add_conditional_edges",
        "add_edge",
        "add_node",
        "ainvoke",
        "astream",
        "batch",
        "compile",
        "invoke",
        "set_entry_point",
        "set_finish_point",
        "stream",
    }
)

_DISTRIBUTION_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_PATH_TOKEN = re.compile(r"[A-Za-z0-9]+")


def _production_modules() -> tuple[tuple[Path, ast.Module], ...]:
    modules: list[tuple[Path, ast.Module]] = []
    for path in sorted(PRODUCTION_ROOT.rglob("*.py")):
        relative_path = path.relative_to(REPOSITORY_ROOT)
        source = path.read_text(encoding="utf-8")
        modules.append((path, ast.parse(source, filename=relative_path.as_posix())))
    return tuple(modules)


def _normalized_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.casefold())


def _is_forbidden_import(module_name: str) -> bool:
    root = module_name.partition(".")[0].casefold().replace("-", "_")
    return root in FORBIDDEN_IMPORT_ROOTS or root.startswith(FORBIDDEN_IMPORT_PREFIXES)


def _is_forbidden_distribution(distribution_name: str) -> bool:
    normalized = _normalized_distribution_name(distribution_name)
    return normalized in FORBIDDEN_DISTRIBUTION_NAMES or normalized.startswith(
        FORBIDDEN_DISTRIBUTION_PREFIXES
    )


def _dynamic_import_aliases(
    tree: ast.Module,
) -> tuple[set[str], set[str], set[str]]:
    importlib_aliases: set[str] = set()
    import_module_aliases: set[str] = set()
    builtin_import_aliases = {"__import__"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name == "importlib":
                    importlib_aliases.add(imported.asname or imported.name)
                elif imported.name == "builtins":
                    builtin_import_aliases.add(
                        f"{imported.asname or imported.name}.__import__"
                    )
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for imported in node.names:
                if imported.name == "import_module":
                    import_module_aliases.add(imported.asname or imported.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "builtins":
            for imported in node.names:
                if imported.name == "__import__":
                    builtin_import_aliases.add(imported.asname or imported.name)

    return importlib_aliases, import_module_aliases, builtin_import_aliases


def _dynamic_import_target(tree: ast.Module, call: ast.Call) -> str | None:
    argument: ast.expr | None = call.args[0] if call.args else None
    if argument is None:
        argument = next(
            (keyword.value for keyword in call.keywords if keyword.arg == "name"),
            None,
        )
    if not isinstance(argument, ast.Constant):
        return None
    target = argument.value
    if not isinstance(target, str):
        return None

    importlib_aliases, import_module_aliases, builtin_aliases = _dynamic_import_aliases(
        tree
    )
    function = call.func
    if isinstance(function, ast.Name) and function.id in (
        import_module_aliases | builtin_aliases
    ):
        return target
    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        qualified_name = f"{function.value.id}.{function.attr}"
        if (
            function.attr == "import_module" and function.value.id in importlib_aliases
        ) or qualified_name in builtin_aliases:
            return target
    return None


def _declared_dependency_names() -> tuple[str, ...]:
    document = tomllib.loads(
        (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = document.get("project", {})
    dependency_groups = document.get("dependency-groups", {})
    assert isinstance(project, dict)
    assert isinstance(dependency_groups, dict)

    specifications: list[str] = []
    direct = project.get("dependencies", [])
    assert isinstance(direct, list)
    specifications.extend(
        specification for specification in direct if isinstance(specification, str)
    )

    optional = project.get("optional-dependencies", {})
    assert isinstance(optional, dict)
    for group in optional.values():
        assert isinstance(group, list)
        specifications.extend(
            specification for specification in group if isinstance(specification, str)
        )

    for group in dependency_groups.values():
        assert isinstance(group, list)
        specifications.extend(
            specification for specification in group if isinstance(specification, str)
        )

    names: list[str] = []
    for specification in specifications:
        match = _DISTRIBUTION_NAME.match(specification)
        assert match is not None, f"unable to parse dependency: {specification!r}"
        names.append(match.group(1))
    return tuple(names)


def _locked_dependency_names() -> tuple[str, ...]:
    document = tomllib.loads((REPOSITORY_ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = document.get("package", [])
    assert isinstance(packages, list)

    names: list[str] = []
    for package in packages:
        assert isinstance(package, dict)
        name = package.get("name")
        assert isinstance(name, str)
        names.append(name)
    return tuple(names)


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def test_production_has_no_deferred_static_or_dynamic_imports() -> None:
    violations: list[str] = []

    for path, tree in _production_modules():
        relative_path = path.relative_to(REPOSITORY_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for imported in node.names:
                    if _is_forbidden_import(imported.name):
                        violations.append(
                            f"{relative_path}:{node.lineno}: forbidden import "
                            f"{imported.name!r}"
                        )
            elif (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module is not None
                and _is_forbidden_import(node.module)
            ):
                violations.append(
                    f"{relative_path}:{node.lineno}: forbidden import {node.module!r}"
                )
            elif isinstance(node, ast.Call):
                target = _dynamic_import_target(tree, node)
                if target is not None and _is_forbidden_import(target):
                    violations.append(
                        f"{relative_path}:{node.lineno}: forbidden dynamic import "
                        f"{target!r}"
                    )

    assert not violations, "deferred Stage 1 imports found:\n" + "\n".join(
        sorted(violations)
    )


def test_dependency_manifests_have_no_deferred_runtime_packages() -> None:
    violations = {
        "pyproject.toml": sorted(
            name
            for name in _declared_dependency_names()
            if _is_forbidden_distribution(name)
        ),
        "uv.lock": sorted(
            name
            for name in _locked_dependency_names()
            if _is_forbidden_distribution(name)
        ),
    }
    violations = {source: names for source, names in violations.items() if names}

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


def test_provider_runtime_and_service_packages_remain_stubs() -> None:
    violations: list[str] = []
    for package_name in STUB_ONLY_PACKAGES:
        package_root = PRODUCTION_ROOT / package_name
        actual_files = {
            path.relative_to(package_root).as_posix()
            for path in package_root.rglob("*.py")
        }
        if actual_files != {"__init__.py"}:
            violations.append(f"{package_name}: {sorted(actual_files)}")

    assert not violations, (
        "Stage 1 implementation package is no longer a stub:\n" + "\n".join(violations)
    )


def test_no_backend_client_provider_or_business_modules_exist() -> None:
    violations: list[str] = []
    for path in sorted(PRODUCTION_ROOT.rglob("*.py")):
        relative_path = path.relative_to(PRODUCTION_ROOT).as_posix()
        if relative_path in ALLOWED_BOUNDARY_INITIALIZERS:
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


def test_no_graph_execution_calls_exist_in_production() -> None:
    violations: list[str] = []
    for path, tree in _production_modules():
        relative_path = path.relative_to(REPOSITORY_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if (
                isinstance(function, ast.Name)
                and function.id in GRAPH_EXECUTION_FUNCTIONS
            ):
                call_name = function.id
            elif (
                isinstance(function, ast.Attribute)
                and function.attr in GRAPH_EXECUTION_METHODS
            ):
                call_name = function.attr
            else:
                continue
            violations.append(
                f"{relative_path}:{node.lineno}: graph execution call {call_name!r}"
            )

    assert not violations, "graph execution calls found:\n" + "\n".join(
        sorted(violations)
    )
