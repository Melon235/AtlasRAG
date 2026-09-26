"""Schema-level and source-level prohibitions for graph State declarations."""

from __future__ import annotations

import ast
import re
import shutil
import sys
from collections import Counter
from importlib import import_module
from pathlib import Path
from typing import Any, NewType, TypedDict, cast, get_args, get_origin, get_type_hints

import pytest
from typing_extensions import TypeAliasType

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STATE_DIRECTORY = REPOSITORY_ROOT / "src" / "atlasrag" / "graphs" / "state"
EXPECTED_STATE_DECLARATIONS = {
    "__init__.py": (),
    "answer.py": ("AnswerSubgraphInput", "AnswerState", "AnswerSubgraphOutput"),
    "local_evidence.py": (
        "LocalEvidenceSubgraphInput",
        "LocalEvidenceState",
        "LocalEvidenceSubgraphOutput",
    ),
    "rag_core.py": ("RagCoreInput", "RagCoreState", "RagCoreOutput"),
    "retrieval.py": (
        "RetrievalGraphInput",
        "RetrievalGraphState",
        "RetrievalGraphOutput",
    ),
    "web_evidence.py": (
        "WebEvidenceSubgraphInput",
        "WebEvidenceState",
        "WebEvidenceSubgraphOutput",
    ),
}
EXPECTED_STATE_FILES = frozenset(EXPECTED_STATE_DECLARATIONS)
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
EXPECTED_IMPORTS: dict[str, dict[str, frozenset[str]]] = {
    "__init__.py": {
        "atlasrag.graphs.state.answer": frozenset(
            {"AnswerState", "AnswerSubgraphInput", "AnswerSubgraphOutput"}
        ),
        "atlasrag.graphs.state.local_evidence": frozenset(
            {
                "LocalEvidenceState",
                "LocalEvidenceSubgraphInput",
                "LocalEvidenceSubgraphOutput",
            }
        ),
        "atlasrag.graphs.state.rag_core": frozenset(
            {"RagCoreInput", "RagCoreOutput", "RagCoreState"}
        ),
        "atlasrag.graphs.state.retrieval": frozenset(
            {"RetrievalGraphInput", "RetrievalGraphOutput", "RetrievalGraphState"}
        ),
        "atlasrag.graphs.state.web_evidence": frozenset(
            {
                "WebEvidenceState",
                "WebEvidenceSubgraphInput",
                "WebEvidenceSubgraphOutput",
            }
        ),
    },
    "answer.py": {
        "typing": frozenset({"NotRequired", "TypedDict"}),
        "atlasrag.domain.answer": frozenset(
            {
                "AnswerInput",
                "GenerationResult",
                "OutputReviewResult",
                "WorkingEvidenceBundle",
            }
        ),
        "atlasrag.domain.enums": frozenset({"AnswerExecutionOutcome"}),
        "atlasrag.domain.results": frozenset({"FinalResponse"}),
    },
    "local_evidence.py": {
        "typing": frozenset({"NotRequired", "TypedDict"}),
        "atlasrag.domain.evidence": frozenset(
            {"EvidenceRef", "LocalEvidence", "LocalEvidenceResult"}
        ),
        "atlasrag.domain.requests": frozenset({"LocalRetrievalRequest"}),
        "atlasrag.domain.retrieval": frozenset(
            {"CandidatePool", "RetrievalBranchResult"}
        ),
    },
    "rag_core.py": {
        "typing": frozenset({"NotRequired", "TypedDict"}),
        "atlasrag.domain.answer": frozenset({"AnswerInput"}),
        "atlasrag.domain.enums": frozenset({"AnswerExecutionOutcome"}),
        "atlasrag.domain.evidence": frozenset({"LocalEvidenceResult"}),
        "atlasrag.domain.requests": frozenset(
            {"LocalRetrievalRequest", "UserTurnRequest"}
        ),
        "atlasrag.domain.results": frozenset({"FinalResponse", "RagCoreResult"}),
        "atlasrag.domain.web": frozenset({"WebEvidenceResult"}),
    },
    "retrieval.py": {
        "typing": frozenset({"NotRequired", "TypedDict"}),
        "atlasrag.domain.requests": frozenset({"UserTurnRequest"}),
        "atlasrag.domain.results": frozenset({"FinalResponse", "RagCoreResult"}),
    },
    "web_evidence.py": {
        "typing": frozenset({"NotRequired", "TypedDict"}),
        "atlasrag.domain.web": frozenset(
            {
                "PreparedWebSource",
                "WebEvidenceRequest",
                "WebEvidenceResult",
                "WebSearchResult",
            }
        ),
    },
}

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

_IDENTIFIER_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+")


class _ProviderClient:
    pass


class _HiddenStateForTest(TypedDict):
    input: int


class _CyclicAnnotation:
    __name__: str
    __supertype__: object

    def __init__(self) -> None:
        self.__name__ = "_CyclicAnnotation"
        self.__supertype__ = self


OpaqueAlias = TypeAliasType("OpaqueAlias", _ProviderClient)
OpaqueAnyAlias = TypeAliasType("OpaqueAnyAlias", Any)
OpaqueHandle = NewType("OpaqueHandle", _ProviderClient)


def _words(identifier: str) -> frozenset[str]:
    return frozenset(
        match.group(0).casefold() for match in _IDENTIFIER_WORD.finditer(identifier)
    )


def _annotation_names(annotation: object) -> frozenset[str]:
    names: set[str] = set()
    visited: set[int] = set()

    def visit(value: object) -> None:
        identity = id(value)
        if identity in visited:
            return
        visited.add(identity)

        if isinstance(value, str):
            names.add(value)
            return
        if value is Any:
            names.add("Any")

        name = getattr(value, "__name__", None)
        if isinstance(name, str):
            names.add(name)
        qualname = getattr(value, "__qualname__", None)
        if isinstance(qualname, str):
            names.add(qualname)
        module = getattr(value, "__module__", None)
        if isinstance(module, str) and isinstance(qualname, str):
            names.add(f"{module}.{qualname}")

        if isinstance(value, TypeAliasType):
            visit(value.__value__)

        supertype = getattr(value, "__supertype__", None)
        if supertype is not None:
            visit(supertype)

        origin = get_origin(value)
        if origin is not None:
            visit(origin)
        for argument in get_args(value):
            visit(argument)

    visit(annotation)
    return frozenset(names)


def _relative_state_path(path: Path) -> str:
    return path.relative_to(STATE_DIRECTORY).as_posix()


def _state_module_paths() -> tuple[Path, ...]:
    assert STATE_DIRECTORY.is_dir(), f"missing State package: {STATE_DIRECTORY}"
    paths = tuple(
        sorted(
            STATE_DIRECTORY.rglob("*.py"),
            key=lambda path: _relative_state_path(path),
        )
    )
    actual_files = {_relative_state_path(path) for path in paths}
    nested_directories = {
        _relative_state_path(path)
        for path in STATE_DIRECTORY.rglob("*")
        if path.is_dir()
        and "__pycache__" not in path.relative_to(STATE_DIRECTORY).parts
    }
    missing = sorted(EXPECTED_STATE_FILES - actual_files)
    unexpected = sorted(actual_files - EXPECTED_STATE_FILES)
    if missing or unexpected or nested_directories:
        raise AssertionError(
            "State module set mismatch; "
            f"missing: {missing}; unexpected State module(s): {unexpected}; "
            f"unexpected directories: {sorted(nested_directories)}"
        )
    return paths


def _class_declaration_names(tree: ast.Module) -> tuple[str, ...]:
    return tuple(
        statement.name for statement in tree.body if isinstance(statement, ast.ClassDef)
    )


def _schema_types() -> tuple[type[object], ...]:
    schemas: list[type[object]] = []
    for path in _state_module_paths():
        relative_path = _relative_state_path(path)
        if relative_path == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_suffix = Path(relative_path).with_suffix("").parts
        module_name = ".".join(("atlasrag", "graphs", "state", *module_suffix))
        module = import_module(module_name)
        schemas.extend(
            cast(type[object], getattr(module, name))
            for name in _class_declaration_names(tree)
        )
    return tuple(schemas)


def _expected_import_counter(relative_path: str) -> Counter[tuple[str, str]]:
    return Counter(
        (module_name, member_name)
        for module_name, member_names in EXPECTED_IMPORTS[relative_path].items()
        for member_name in member_names
    )


def _import_violations(path: Path, tree: ast.Module) -> list[str]:
    relative_path = _relative_state_path(path)
    violations: list[str] = []
    observed: Counter[tuple[str, str]] = Counter()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = ", ".join(alias.name for alias in node.names)
            violations.append(
                f"{relative_path}:{node.lineno}: plain import is not allowed: {imported}"
            )
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            module_name = f"{'.' * node.level}{node.module or ''}"
            violations.append(
                f"{relative_path}:{node.lineno}: relative import is not allowed: "
                f"{module_name}"
            )
        elif node.module is None:
            module_name = ""
            violations.append(
                f"{relative_path}:{node.lineno}: import has no absolute module"
            )
        else:
            module_name = node.module

        for alias in node.names:
            if alias.name == "*" or alias.asname is not None:
                violations.append(
                    f"{relative_path}:{node.lineno}: import aliases and stars are "
                    "not allowed"
                )
            observed[(module_name, alias.name)] += 1

    expected = _expected_import_counter(relative_path)
    if observed != expected:
        violations.append(
            f"{relative_path}: import allowlist mismatch; "
            f"expected {sorted(expected.elements())}, "
            f"found {sorted(observed.elements())}"
        )
    return violations


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
        violations.extend(_import_violations(path, tree))

        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "Any":
                violations.append(
                    f"{_relative_state_path(path)}:{node.lineno}: Any use"
                )
            elif isinstance(node, ast.Attribute) and node.attr == "Any":
                violations.append(
                    f"{_relative_state_path(path)}:{node.lineno}: Any use"
                )

    assert not violations, "\n".join(violations)


def test_state_definition_modules_are_class_based_declarations_only() -> None:
    violations: list[str] = []

    for path in _state_module_paths():
        relative_path = _relative_state_path(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        declarations = _class_declaration_names(tree)
        expected_declarations = EXPECTED_STATE_DECLARATIONS[relative_path]
        if declarations != expected_declarations:
            violations.append(
                f"{relative_path}: declaration mismatch; "
                f"expected {expected_declarations}, found {declarations}"
            )

        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.decorator_list:
                    violations.append(
                        f"{relative_path}:{node.lineno}: decorator is not allowed"
                    )
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                violations.append(
                    f"{relative_path}:{node.lineno}: function is not allowed"
                )
            elif isinstance(node, ast.Call):
                violations.append(f"{relative_path}:{node.lineno}: call is not allowed")
            elif isinstance(
                node,
                (
                    ast.Await,
                    ast.DictComp,
                    ast.GeneratorExp,
                    ast.Lambda,
                    ast.ListComp,
                    ast.NamedExpr,
                    ast.SetComp,
                    ast.Yield,
                    ast.YieldFrom,
                ),
            ):
                violations.append(
                    f"{relative_path}:{node.lineno}: runtime expression is not allowed"
                )

        if relative_path == "__init__.py":
            all_exports: list[tuple[str, ...]] = []
            for index, statement in enumerate(tree.body):
                if index == 0 and _is_docstring(statement):
                    continue
                if isinstance(statement, ast.ImportFrom):
                    continue
                if isinstance(statement, ast.Assign):
                    valid_target = (
                        len(statement.targets) == 1
                        and isinstance(statement.targets[0], ast.Name)
                        and statement.targets[0].id == "__all__"
                    )
                    if valid_target and isinstance(statement.value, ast.Tuple):
                        export_names: list[str] = []
                        for element in statement.value.elts:
                            if not (
                                isinstance(element, ast.Constant)
                                and isinstance(element.value, str)
                            ):
                                break
                            export_names.append(element.value)
                        else:
                            all_exports.append(tuple(export_names))
                            continue
                violations.append(
                    f"{relative_path}:{statement.lineno}: __init__ contains "
                    "non-declarative content"
                )
            if all_exports != [PUBLIC_STATE_TYPES]:
                violations.append(
                    f"{relative_path}: __all__ must be the exact public export tuple"
                )
            continue

        for index, statement in enumerate(tree.body):
            if (index == 0 and _is_docstring(statement)) or isinstance(
                statement, (ast.Import, ast.ImportFrom)
            ):
                continue
            if not isinstance(statement, ast.ClassDef):
                violations.append(
                    f"{relative_path}:{statement.lineno}: non-class declaration"
                )
                continue

            valid_base = (
                len(statement.bases) == 1
                and isinstance(statement.bases[0], ast.Name)
                and statement.bases[0].id == "TypedDict"
            )
            if not valid_base or statement.keywords:
                violations.append(
                    f"{relative_path}:{statement.lineno}: {statement.name} is not a "
                    "class-based total TypedDict"
                )

            for class_index, class_statement in enumerate(statement.body):
                if class_index == 0 and _is_docstring(class_statement):
                    continue
                if not (
                    isinstance(class_statement, ast.AnnAssign)
                    and isinstance(class_statement.target, ast.Name)
                    and class_statement.value is None
                    and class_statement.simple == 1
                ):
                    violations.append(
                        f"{relative_path}:{class_statement.lineno}: "
                        f"{statement.name} contains runtime/default behavior"
                    )

    assert not violations, "\n".join(violations)


def _copied_state_directory(tmp_path: Path) -> Path:
    copied = tmp_path / "state"
    shutil.copytree(STATE_DIRECTORY, copied)
    return copied


def _use_state_directory(
    monkeypatch: pytest.MonkeyPatch, state_directory: Path
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "STATE_DIRECTORY", state_directory)


def test_state_file_discovery_rejects_nested_python_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_directory = _copied_state_directory(tmp_path)
    nested_module = state_directory / "nested" / "stealth.py"
    nested_module.parent.mkdir()
    nested_module.write_text(
        "from typing import TypedDict\n\n"
        "class StealthState(TypedDict):\n"
        "    value: int\n",
        encoding="utf-8",
    )
    _use_state_directory(monkeypatch, state_directory)

    with pytest.raises(AssertionError, match="unexpected State module"):
        _state_module_paths()


def test_declaration_guard_rejects_unexported_typed_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_directory = _copied_state_directory(tmp_path)
    answer_module = state_directory / "answer.py"
    answer_module.write_text(
        answer_module.read_text(encoding="utf-8")
        + "\n\nclass HiddenState(TypedDict):\n"
        + "    input: AnswerInput\n",
        encoding="utf-8",
    )
    _use_state_directory(monkeypatch, state_directory)

    with pytest.raises(AssertionError, match="declaration"):
        test_state_definition_modules_are_class_based_declarations_only()


def test_runtime_schema_discovery_includes_unexported_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_directory = _copied_state_directory(tmp_path)
    answer_source = state_directory / "answer.py"
    answer_source.write_text(
        answer_source.read_text(encoding="utf-8")
        + "\n\nclass HiddenState(TypedDict):\n"
        + "    input: AnswerInput\n",
        encoding="utf-8",
    )
    answer_module = import_module("atlasrag.graphs.state.answer")
    monkeypatch.setattr(
        answer_module, "HiddenState", _HiddenStateForTest, raising=False
    )
    _use_state_directory(monkeypatch, state_directory)

    assert _HiddenStateForTest in _schema_types()


@pytest.mark.parametrize(
    "statement",
    (
        "from atlasrag import runtime",
        "from ...runtime import X",
        "import subprocess",
        "from atlasrag.domain.answer import ProviderClient",
    ),
    ids=(
        "package-member",
        "relative-import",
        "plain-import",
        "unexpected-member",
    ),
)
def test_import_guard_rejects_allowlist_bypasses(
    statement: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_directory = _copied_state_directory(tmp_path)
    answer_module = state_directory / "answer.py"
    answer_module.write_text(
        answer_module.read_text(encoding="utf-8") + f"\n{statement}\n",
        encoding="utf-8",
    )
    _use_state_directory(monkeypatch, state_directory)

    with pytest.raises(AssertionError, match="import"):
        test_state_modules_import_only_declarative_contract_dependencies()


@pytest.mark.parametrize("target", ("class", "function"))
def test_definition_guard_explicitly_rejects_decorators(
    target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_directory = _copied_state_directory(tmp_path)
    answer_module = state_directory / "answer.py"
    source = answer_module.read_text(encoding="utf-8")
    if target == "class":
        source = source.replace(
            "class AnswerState(TypedDict):",
            "@atexit.register\nclass AnswerState(TypedDict):",
        )
    else:
        source += "\n\n@atexit.register\ndef hidden_hook() -> None:\n    pass\n"
    answer_module.write_text(source, encoding="utf-8")
    _use_state_directory(monkeypatch, state_directory)

    with pytest.raises(AssertionError, match="decorator"):
        test_state_definition_modules_are_class_based_declarations_only()


def test_init_guard_rejects_non_declarative_assignments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_directory = _copied_state_directory(tmp_path)
    init_module = state_directory / "__init__.py"
    init_module.write_text(
        init_module.read_text(encoding="utf-8") + "\nSIDE_EFFECT = 1\n",
        encoding="utf-8",
    )
    _use_state_directory(monkeypatch, state_directory)

    with pytest.raises(AssertionError, match="__init__ contains"):
        test_state_definition_modules_are_class_based_declarations_only()


def test_annotation_inspection_unwraps_type_alias_value() -> None:
    names = _annotation_names(OpaqueAlias)

    assert _ProviderClient.__name__ in names
    assert f"{_ProviderClient.__module__}.{_ProviderClient.__qualname__}" in names


def test_annotation_inspection_unwraps_type_alias_any() -> None:
    assert "Any" in _annotation_names(OpaqueAnyAlias)


def test_annotation_inspection_unwraps_new_type_supertype() -> None:
    names = _annotation_names(OpaqueHandle)

    assert _ProviderClient.__name__ in names
    assert f"{_ProviderClient.__module__}.{_ProviderClient.__qualname__}" in names


def test_annotation_inspection_stops_at_recursive_wrapper_cycles() -> None:
    assert "_CyclicAnnotation" in _annotation_names(_CyclicAnnotation())
