"""Schema-level and source-level prohibitions for graph State declarations."""

from __future__ import annotations

import ast
import re
from importlib import import_module
from pathlib import Path
from typing import Any, cast, get_args, get_origin, get_type_hints

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STATE_DIRECTORY = REPOSITORY_ROOT / "src" / "atlasrag" / "graphs" / "state"
EXPECTED_STATE_FILES = frozenset(
    {
        "__init__.py",
        "answer.py",
        "local_evidence.py",
        "rag_core.py",
        "retrieval.py",
        "web_evidence.py",
    }
)
PUBLIC_STATE_TYPES = (
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

FORBIDDEN_SCHEMA_WORDS = frozenset(
    {
        "any",
        "archive",
        "archives",
        "benchmark",
        "benchmarks",
        "body",
        "budget",
        "budgets",
        "cache",
        "client",
        "clients",
        "config",
        "configuration",
        "connection",
        "connections",
        "container",
        "conversation",
        "database",
        "db",
        "dependencies",
        "dependency",
        "embedding",
        "embeddings",
        "error",
        "errors",
        "exception",
        "exceptions",
        "gold",
        "histories",
        "history",
        "html",
        "http",
        "llm",
        "log",
        "logs",
        "messages",
        "metadata",
        "milvus",
        "model",
        "policy",
        "postgres",
        "postgresql",
        "previous",
        "prior",
        "provider",
        "providers",
        "qrel",
        "qrels",
        "raw",
        "redis",
        "repositories",
        "repository",
        "retries",
        "retry",
        "service",
        "services",
        "settings",
        "span",
        "spans",
        "sql",
        "sqlite",
        "trace",
        "traceback",
        "traces",
        "transcript",
        "vector",
        "vectors",
    }
)

FORBIDDEN_IMPORT_PREFIXES = (
    "aiohttp",
    "asyncpg",
    "atlasrag.config",
    "atlasrag.observability",
    "atlasrag.providers",
    "atlasrag.repositories",
    "atlasrag.runtime",
    "atlasrag.services",
    "benchmarks",
    "dataclasses",
    "http",
    "httpx",
    "langgraph",
    "milvus",
    "psycopg",
    "psycopg2",
    "pydantic",
    "pymilvus",
    "redis",
    "requests",
    "sqlalchemy",
    "sqlite3",
    "sqlmodel",
    "tests",
    "urllib",
)

_IDENTIFIER_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+")


def _words(identifier: str) -> frozenset[str]:
    return frozenset(
        match.group(0).casefold() for match in _IDENTIFIER_WORD.finditer(identifier)
    )


def _annotation_names(annotation: object) -> frozenset[str]:
    names: set[str] = set()
    if annotation is Any:
        names.add("Any")

    origin = get_origin(annotation)
    if origin is not None:
        origin_name = getattr(origin, "__name__", None)
        if isinstance(origin_name, str):
            names.add(origin_name)
        for argument in get_args(annotation):
            names.update(_annotation_names(argument))
        return frozenset(names)

    annotation_name = getattr(annotation, "__name__", None)
    if isinstance(annotation_name, str):
        names.add(annotation_name)
    return frozenset(names)


def _schema_types() -> tuple[type[object], ...]:
    package = import_module("atlasrag.graphs.state")
    return tuple(
        cast(type[object], getattr(package, name)) for name in PUBLIC_STATE_TYPES
    )


def _state_module_paths() -> tuple[Path, ...]:
    assert STATE_DIRECTORY.is_dir(), f"missing State package: {STATE_DIRECTORY}"
    paths = tuple(sorted(STATE_DIRECTORY.glob("*.py")))
    assert {path.name for path in paths} == EXPECTED_STATE_FILES
    return paths


def _module_name_is_forbidden(module_name: str) -> bool:
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.")
        for prefix in FORBIDDEN_IMPORT_PREFIXES
    )


def _imported_module_names(tree: ast.AST) -> tuple[str, ...]:
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
    return tuple(imported)


def _is_docstring(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def test_state_keys_and_recursive_annotation_names_avoid_forbidden_vocabulary() -> None:
    violations: list[str] = []

    for schema in _schema_types():
        resolved = get_type_hints(schema, include_extras=True)
        for key, annotation in resolved.items():
            forbidden_key_words = _words(key) & FORBIDDEN_SCHEMA_WORDS
            if forbidden_key_words:
                violations.append(
                    f"{schema.__name__}.{key} key: {sorted(forbidden_key_words)}"
                )

            for annotation_name in _annotation_names(annotation):
                forbidden_type_words = _words(annotation_name) & FORBIDDEN_SCHEMA_WORDS
                if forbidden_type_words:
                    violations.append(
                        f"{schema.__name__}.{key} annotation {annotation_name}: "
                        f"{sorted(forbidden_type_words)}"
                    )

    assert not violations, "\n".join(violations)


def test_only_frozen_graph_semantic_counters_are_present() -> None:
    counter_keys: set[tuple[str, str]] = set()

    for schema in _schema_types():
        for key in get_type_hints(schema, include_extras=True):
            if _words(key) & {"attempt", "counter", "index", "retries", "retry"}:
                counter_keys.add((schema.__name__, key))

    assert counter_keys == {
        ("AnswerState", "generation_attempt_index"),
        ("RagCoreState", "local_attempt_index"),
    }


def test_state_modules_import_only_declarative_contract_dependencies() -> None:
    violations: list[str] = []

    for path in _state_module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module_name in _imported_module_names(tree):
            if _module_name_is_forbidden(module_name):
                violations.append(f"{path.name}: forbidden import {module_name}")

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module in {"typing", "typing_extensions"}
                and any(alias.name == "Any" for alias in node.names)
            ):
                violations.append(f"{path.name}:{node.lineno}: Any import")
            elif isinstance(node, ast.Name) and node.id == "Any":
                violations.append(f"{path.name}:{node.lineno}: Any use")
            elif isinstance(node, ast.Attribute) and node.attr == "Any":
                violations.append(f"{path.name}:{node.lineno}: Any use")

    assert not violations, "\n".join(violations)


def test_state_definition_modules_are_class_based_declarations_only() -> None:
    violations: list[str] = []

    for path in _state_module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Call))
            for node in ast.walk(tree)
        ):
            violations.append(f"{path.name}: runtime behavior")

        if path.name == "__init__.py":
            continue

        for statement in tree.body:
            if _is_docstring(statement) or isinstance(
                statement, (ast.Import, ast.ImportFrom)
            ):
                continue
            if not isinstance(statement, ast.ClassDef):
                violations.append(
                    f"{path.name}:{statement.lineno}: non-class declaration"
                )
                continue

            base_names = {
                base.id
                if isinstance(base, ast.Name)
                else base.attr
                if isinstance(base, ast.Attribute)
                else ""
                for base in statement.bases
            }
            if base_names != {"TypedDict"} or statement.keywords:
                violations.append(
                    f"{path.name}:{statement.lineno}: {statement.name} is not a "
                    "class-based total TypedDict"
                )

            for class_statement in statement.body:
                if _is_docstring(class_statement):
                    continue
                if not (
                    isinstance(class_statement, ast.AnnAssign)
                    and isinstance(class_statement.target, ast.Name)
                    and class_statement.value is None
                ):
                    violations.append(
                        f"{path.name}:{class_statement.lineno}: "
                        f"{statement.name} contains runtime/default behavior"
                    )

    assert not violations, "\n".join(violations)
