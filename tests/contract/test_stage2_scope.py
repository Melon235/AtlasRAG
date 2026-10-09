"""Layer-specific Stage 2 dependency and implementation boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from test_stage1_scope import (
    PRODUCTION_ROOT,
    STAGE2_DRIVER_LAYERS,
    _source_import_violations,
)


def _layer_violations(source: str, path: Path) -> tuple[str, ...]:
    """Resolve relative imports too, so aliases cannot bypass layer ownership."""
    tree = ast.parse(source)
    parts = path.parts
    relative = parts[parts.index("atlasrag") + 1 :] if "atlasrag" in parts else parts
    layer = relative[0]
    forbidden = {"tests", "benchmarks"}
    if layer == "repositories":
        forbidden |= {"atlasrag.graphs", "atlasrag.providers"}
    elif layer == "providers":
        forbidden |= {"atlasrag.graphs", "atlasrag.repositories"}
    violations = list(_source_import_violations(source, path))
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [entry.name for entry in node.names]
        elif isinstance(node, ast.ImportFrom):
            prefix = ("atlasrag", *relative[:-1])
            module = node.module or ""
            if node.level:
                module = ".".join((*prefix[: -node.level + 1 or None], module))
                module = module.rstrip(".")
            modules = [module] + [f"{module}.{entry.name}" for entry in node.names]
        for module in modules:
            assert isinstance(node, (ast.Import, ast.ImportFrom))
            if any(
                module == item or module.startswith(item + ".") for item in forbidden
            ):
                violations.append(f"{path}:{node.lineno}: forbidden layer {module}")
        identifier: str | None = None
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.alias)
        ):
            identifier = node.name
        elif isinstance(node, ast.Name):
            identifier = node.id
        elif isinstance(node, ast.Attribute):
            identifier = node.attr
        if identifier is not None and layer in {"repositories", "providers"}:
            name = identifier.replace("_", "").lower()
            if any(
                term in name
                for term in (
                    "runtimegate",
                    "syncplan",
                    "retrievalservice",
                    "rrffusion",
                    "publishdecision",
                    "publishrevision",
                    "retrydecision",
                    "cachepolicy",
                    "evidencesufficient",
                    "retryneeded",
                    "sourcemutationpolicy",
                    "webfallback",
                    "cacheeligibility",
                    "rerankerpolicy",
                    "rrfordering",
                    "candidatesufficient",
                    "rerankerfallback",
                )
            ):
                violations.append(f"{path}: deferred business policy {identifier}")
    return tuple(violations)


def test_production_respects_stage2_layers() -> None:
    violations = [
        violation
        for path in PRODUCTION_ROOT.rglob("*.py")
        for violation in _layer_violations(path.read_text(), path)
    ]
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize("driver,layer", STAGE2_DRIVER_LAYERS.items())
def test_drivers_are_allowed_only_in_their_own_layer(driver: str, layer: str) -> None:
    source = f"import {driver}\nfrom {driver} import Client"
    assert not _layer_violations(source, Path(layer) / "client.py")
    for wrong_layer in {"domain/", "graphs/state/", *STAGE2_DRIVER_LAYERS.values()} - {
        layer
    }:
        assert _layer_violations(source, Path(wrong_layer) / "client.py")


@pytest.mark.parametrize("layer", ["repositories/postgres", "providers/cache"])
@pytest.mark.parametrize(
    "source",
    [
        "from atlasrag.graphs import state",
        "from ...graphs import state",
        "from ... import graphs",
        "import tests.helpers",
        "from benchmarks import data",
        "class RuntimeGate: pass",
        "def publish_revision(): pass",
        "async def retry_decision(): pass",
        "if evidence_sufficient: pass",
        "if retry_needed: pass",
        "from atlasrag.domain.requests import SyncPlan",
        "result = policies.cache_eligibility()",
        "def source_mutation_policy(): pass",
        "def web_fallback(): pass",
        "def reranker_fallback(): pass",
    ],
)
def test_deferred_layer_and_policy_imports_are_rejected(
    layer: str, source: str
) -> None:
    assert _layer_violations(source, Path(layer) / "records.py")


@pytest.mark.parametrize(
    "path,source",
    [
        ("repositories/postgres/records.py", "from atlasrag.providers import cache"),
        ("providers/cache/models.py", "from atlasrag.repositories import postgres"),
    ],
)
def test_provider_repository_coupling_is_rejected(path: str, source: str) -> None:
    assert _layer_violations(source, Path(path))
