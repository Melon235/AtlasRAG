"""Composition of the benchmark runner with the real Part I build boundary."""

from __future__ import annotations

from typing import Any

from atlasrag.application.document_build import DocumentBuildRunner
from atlasrag.services.indexing.build import BuildResources


def production_ingestion_runner(
    resources: BuildResources,
    pool: Any,
    *,
    lock_manager_factory: Any,
    metadata_repository_factory: Any,
    max_concurrent_builds: int = 2,
    pool_capacity: int = 4,
) -> DocumentBuildRunner[Any]:
    """Create the real Part I build backend; parsing/embedding/indexing stay there."""
    return DocumentBuildRunner(
        resources=resources,
        pool=pool,
        lock_manager_factory=lock_manager_factory,
        metadata_repository_factory=metadata_repository_factory,
        max_concurrent_builds=max_concurrent_builds,
        pool_capacity=pool_capacity,
    )
