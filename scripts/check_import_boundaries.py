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
PARSER_SDK_PREFIXES = ("docling", "mineru")
EMBEDDING_SDK_PREFIXES = ("torch", "transformers", "huggingface_hub")
INDEXING_PURE_FORBIDDEN_PREFIXES = (
    "atlasrag.graphs",
    "atlasrag.providers",
    "atlasrag.repositories",
    "torch",
    "transformers",
    "huggingface_hub",
    "pymilvus",
    "psycopg",
    "psycopg_pool",
    "sqlalchemy",
    "sqlite3",
    "redis",
    "httpx",
    "requests",
    "aiohttp",
    "socket",
    "urllib.request",
    "http.client",
)
INDEXING_SERVICE_FORBIDDEN_PREFIXES = (
    "atlasrag.graphs",
    "atlasrag.providers.cache",
    "atlasrag.services.retrieval",
    "atlasrag.services.rag_core",
    "atlasrag.services.local_evidence",
    "atlasrag.services.web_evidence",
    "atlasrag.services.answer",
    "pymilvus",
    "psycopg",
    "psycopg_pool",
    "sqlalchemy",
    "sqlite3",
    "redis",
)
STAGE3_BOUNDARY_FORBIDDEN_PREFIXES = {
    ("domain", "ingestion"): (
        "atlasrag.graphs",
        "atlasrag.providers",
        "atlasrag.repositories",
        *PARSER_SDK_PREFIXES,
    ),
    ("providers", "parsing"): (
        "atlasrag.benchmarks",
        "atlasrag.graphs",
        "atlasrag.repositories",
        "atlasrag.services",
    ),
    ("services", "ingestion"): (
        "atlasrag.graphs",
        "atlasrag.providers.cache",
        "atlasrag.providers.index",
    ),
}
STAGE3_BOUNDARY_NAMES = {
    ("domain", "ingestion"): "ingestion domain",
    ("providers", "parsing"): "parsing provider",
    ("services", "ingestion"): "ingestion service",
}
BUILD_GRAPH_PATH = "src/atlasrag/graphs/indexing/document_build.py"
SYNC_GRAPH_PATH = "src/atlasrag/graphs/indexing/index_graph.py"
LOCAL_EVIDENCE_GRAPH_PATH = "src/atlasrag/graphs/local_evidence/subgraph.py"
WEB_EVIDENCE_GRAPH_PATH = "src/atlasrag/graphs/web_evidence/subgraph.py"
ANSWER_GRAPH_PATH = "src/atlasrag/graphs/answer/subgraph.py"
RAG_CORE_GRAPH_PATH = "src/atlasrag/graphs/rag_core/subgraph.py"
RETRIEVAL_GRAPH_PATH = "src/atlasrag/graphs/retrieval/graph.py"
BUILD_LANGGRAPH_IMPORTS = frozenset(
    {
        "langgraph.graph",
        "langgraph.graph.START",
        "langgraph.graph.END",
        "langgraph.graph.StateGraph",
        "langgraph.graph.state",
        "langgraph.graph.state.CompiledStateGraph",
    }
)
BUILD_GRAPH_CAPABILITIES = {
    BUILD_GRAPH_PATH: frozenset(
        {
            "atlasrag.services.indexing.build",
            "atlasrag.services.indexing.build.BuildBusinessFailure",
            "atlasrag.services.indexing.ports",
            "atlasrag.services.indexing.ports.DocumentBuildSteps",
        }
    ),
    "src/atlasrag/graphs/indexing/contracts.py": frozenset(
        {
            "atlasrag.repositories.postgres.records",
            "atlasrag.repositories.postgres.records.FailedStage",
            "atlasrag.services.indexing.ports",
            "atlasrag.services.indexing.ports.BuildFailureCode",
        }
    ),
    "src/atlasrag/graphs/indexing/state.py": frozenset(
        {
            "atlasrag.repositories.postgres.records",
            "atlasrag.repositories.postgres.records.FailedStage",
            "atlasrag.services.indexing.ports",
            "atlasrag.services.indexing.ports.BuildFailureCode",
        }
    ),
    "src/atlasrag/graphs/indexing/sync_state.py": frozenset(
        {
            "atlasrag.services.sync.models",
            "atlasrag.services.sync.models.BuildExecutionSummary",
            "atlasrag.services.sync.models.DeletionSummary",
            "atlasrag.services.sync.models.ReconciliationResult",
            "atlasrag.services.sync.models.ScanSnapshot",
            "atlasrag.services.sync.models.SyncPlan",
            "atlasrag.services.sync.models.SyncRequest",
            "atlasrag.services.sync.models.SyncResult",
        }
    ),
    "src/atlasrag/graphs/indexing/sync_contracts.py": frozenset(
        {
            "atlasrag.services.sync.models",
            "atlasrag.services.sync.models.BuildExecutionSummary",
            "atlasrag.services.sync.models.DeletionSummary",
            "atlasrag.services.sync.models.GlobalSafetyResult",
            "atlasrag.services.sync.models.ReconciliationResult",
            "atlasrag.services.sync.models.ScanSnapshot",
            "atlasrag.services.sync.models.SyncPlan",
        }
    ),
    SYNC_GRAPH_PATH: frozenset(
        {
            "atlasrag.services.sync.failures",
            "atlasrag.services.sync.failures.ScanError",
            "atlasrag.services.sync.failures.SyncPlanError",
            "atlasrag.services.sync.models",
            "atlasrag.services.sync.models.ReconciliationOutcome",
            "atlasrag.services.sync.results",
            "atlasrag.services.sync.results.finalize_sync_result",
        }
    ),
    LOCAL_EVIDENCE_GRAPH_PATH: frozenset(
        {
            "atlasrag.services.local_evidence.fusion",
            "atlasrag.services.local_evidence.fusion.fuse",
            "atlasrag.services.local_evidence.service",
            "atlasrag.services.local_evidence.service.LocalEvidenceServices",
        }
    ),
    WEB_EVIDENCE_GRAPH_PATH: frozenset(
        {
            "atlasrag.services.web_evidence.profile",
            "atlasrag.services.web_evidence.profile.WebEvidenceProfile",
            "atlasrag.services.web_evidence.service",
            "atlasrag.services.web_evidence.service.WebEvidenceStrategy",
            "atlasrag.services.web_evidence.service.WebPreparationService",
            "atlasrag.services.web_evidence.service.preparation_degraded",
            "atlasrag.services.web_evidence.ports",
            "atlasrag.services.web_evidence.ports.SearchProvider",
        }
    ),
    RAG_CORE_GRAPH_PATH: frozenset(
        {
            "atlasrag.services.rag_core.boundary",
            "atlasrag.services.rag_core.boundary.AnswerInputBuilder",
            "atlasrag.services.rag_core.completion",
            "atlasrag.services.rag_core.completion.CompletionAssessor",
            "atlasrag.services.rag_core.query",
            "atlasrag.services.rag_core.query.QueryConstructionService",
            "atlasrag.services.rag_core.routing",
            "atlasrag.services.rag_core.routing.CorrectiveAction",
            "atlasrag.services.rag_core.routing.CorrectiveRoutingService",
        }
    ),
    RETRIEVAL_GRAPH_PATH: frozenset(
        {
            "atlasrag.services.cache.session",
            "atlasrag.services.cache.session.SessionQueryCache",
        }
    ),
    ANSWER_GRAPH_PATH: frozenset(
        {
            "atlasrag.services.answer.citations",
            "atlasrag.services.answer.citations.CitationResolver",
            "atlasrag.services.answer.compression",
            "atlasrag.services.answer.compression.CompressionError",
            "atlasrag.services.answer.compression.CompressionStrategy",
            "atlasrag.services.answer.generation_input",
            "atlasrag.services.answer.generation_input.GenerationInputBuilder",
            "atlasrag.services.answer.ports",
            "atlasrag.services.answer.ports.GenerationProtocolError",
            "atlasrag.services.answer.ports.GenerationProvider",
            "atlasrag.services.answer.profile",
            "atlasrag.services.answer.profile.AnswerProfile",
            "atlasrag.services.answer.review",
            "atlasrag.services.answer.review.OutputReviewService",
        }
    ),
}
GRAPH_PLUMBING_PREFIXES = (
    "atlasrag.providers",
    "psycopg",
    "psycopg_pool",
    "sqlalchemy",
    "sqlite3",
    "pymilvus",
    "redis",
    "httpx",
    "requests",
    "aiohttp",
    "socket",
    "urllib.request",
    "http.client",
    "torch",
    "transformers",
    "huggingface_hub",
)


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


def _stage3_boundary(relative_path: str) -> tuple[str, str] | None:
    parts = Path(relative_path).parts
    if len(parts) < 4 or parts[:2] != ("src", "atlasrag"):
        return None
    candidate = (parts[2], parts[3])
    return candidate if candidate in STAGE3_BOUNDARY_FORBIDDEN_PREFIXES else None


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


def _matching_prefix(prefixes: tuple[str, ...], module_name: str) -> str | None:
    for prefix in prefixes:
        if module_name == prefix or module_name.startswith(f"{prefix}."):
            return prefix
    return None


def _parser_sdk_prefix(module_name: str) -> str | None:
    return _matching_prefix(PARSER_SDK_PREFIXES, module_name)


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
    stage3_boundary = _stage3_boundary(relative_path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            targets = _import_targets(node, path, root)
            if relative_path.startswith("src/atlasrag/services/indexing/"):
                for target in targets:
                    if _matching_prefix(INDEXING_SERVICE_FORBIDDEN_PREFIXES, target):
                        diagnostics.append(
                            Diagnostic(
                                relative_path,
                                node.lineno,
                                f"indexing service may not import '{target}'",
                            )
                        )
            for target in targets:
                if target == "langgraph" or target.startswith("langgraph."):
                    if (
                        relative_path
                        not in {
                            BUILD_GRAPH_PATH,
                            SYNC_GRAPH_PATH,
                            LOCAL_EVIDENCE_GRAPH_PATH,
                            WEB_EVIDENCE_GRAPH_PATH,
                            ANSWER_GRAPH_PATH,
                            RAG_CORE_GRAPH_PATH,
                            RETRIEVAL_GRAPH_PATH,
                        }
                        or target not in BUILD_LANGGRAPH_IMPORTS
                    ):
                        diagnostics.append(
                            Diagnostic(
                                relative_path,
                                node.lineno,
                                f"LangGraph import '{target}' is not an approved build graph capability",
                            )
                        )
                if relative_path.startswith("src/atlasrag/graphs/"):
                    if (
                        isinstance(node, ast.ImportFrom)
                        and any(alias.name == "*" for alias in node.names)
                        and _matching_prefix(
                            ("langgraph", "atlasrag.repositories", "atlasrag.services"),
                            target,
                        )
                    ):
                        diagnostics.append(
                            Diagnostic(
                                relative_path,
                                node.lineno,
                                "thin graph capabilities require explicit named imports",
                            )
                        )
                    capability = _matching_prefix(
                        ("atlasrag.repositories", "atlasrag.services"), target
                    )
                    if (
                        _matching_prefix(GRAPH_PLUMBING_PREFIXES, target) is not None
                        or capability is not None
                        and target
                        not in BUILD_GRAPH_CAPABILITIES.get(relative_path, frozenset())
                    ):
                        diagnostics.append(
                            Diagnostic(
                                relative_path,
                                node.lineno,
                                f"thin graph may not import '{target}'",
                            )
                        )
            embedding_sdk = next(
                (
                    sdk
                    for target in targets
                    if (sdk := _matching_prefix(EMBEDDING_SDK_PREFIXES, target))
                    is not None
                ),
                None,
            )
            if embedding_sdk is not None and relative_path not in {
                "src/atlasrag/providers/embedding/qwen.py",
                "src/atlasrag/providers/reranker/bge.py",
            }:
                diagnostics.append(
                    Diagnostic(
                        relative_path,
                        node.lineno,
                        f"embedding SDK '{embedding_sdk}' is only allowed in approved model adapters",
                    )
                )
                # Retain the pure-boundary diagnostic below for existing callers.
            pure_indexing = (
                relative_path.startswith("src/atlasrag/domain/indexing/")
                or relative_path == "src/atlasrag/services/indexing/representation.py"
            )
            if pure_indexing:
                forbidden_prefix = next(
                    (
                        prefix
                        for target in targets
                        if (
                            prefix := _matching_prefix(
                                INDEXING_PURE_FORBIDDEN_PREFIXES, target
                            )
                        )
                        is not None
                    ),
                    None,
                )
                if forbidden_prefix is not None:
                    diagnostics.append(
                        Diagnostic(
                            relative_path,
                            node.lineno,
                            f"pure indexing boundary may not import '{forbidden_prefix}'",
                        )
                    )
                    continue
            if stage3_boundary is not None:
                forbidden_prefix = next(
                    (
                        prefix
                        for target in targets
                        if (
                            prefix := _matching_prefix(
                                STAGE3_BOUNDARY_FORBIDDEN_PREFIXES[stage3_boundary],
                                target,
                            )
                        )
                        is not None
                    ),
                    None,
                )
                if forbidden_prefix is not None:
                    diagnostics.append(
                        Diagnostic(
                            relative_path,
                            node.lineno,
                            f"{STAGE3_BOUNDARY_NAMES[stage3_boundary]} may not "
                            f"import '{forbidden_prefix}'",
                        )
                    )
                    continue
            parser_sdk = next(
                (
                    sdk
                    for target in targets
                    if (sdk := _parser_sdk_prefix(target)) is not None
                ),
                None,
            )
            if parser_sdk is not None and stage3_boundary != (
                "providers",
                "parsing",
            ):
                diagnostics.append(
                    Diagnostic(
                        relative_path,
                        node.lineno,
                        f"parser SDK '{parser_sdk}' is only allowed in parsing "
                        "providers",
                    )
                )
                continue
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
            relative_path.startswith("src/atlasrag/graphs/")
            and isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.lstrip()
            .upper()
            .startswith(("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "CREATE TABLE "))
        ):
            diagnostics.append(
                Diagnostic(
                    relative_path, node.lineno, "SQL is not allowed in graph modules"
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
