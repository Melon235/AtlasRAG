from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.environment.isolation import (
    IsolationConfig,
    IsolationError,
    validate_isolation,
)


def config(tmp_path: Path, **overrides: object) -> IsolationConfig:
    values: dict[str, object] = dict(
        postgres_database="atlasrag_bench_stage9",
        milvus_collection="atlasrag_bench_stage9",
        redis_database=15,
        knowledge_directory=tmp_path / "benchmark_data" / "knowledge",
        artifact_root=tmp_path / "benchmark_runs",
        owned_compose_project="atlasrag-stage9-isolated",
    )
    values.update(overrides)
    return IsolationConfig.model_validate(values)


def test_valid_benchmark_isolation(tmp_path: Path) -> None:
    validate_isolation(config(tmp_path), production_markers=("/home/user/knowledge",))


def test_production_paths_and_redis_zero_rejected(tmp_path: Path) -> None:
    production = tmp_path / "production"
    with pytest.raises(IsolationError, match="production"):
        validate_isolation(
            config(tmp_path, knowledge_directory=production),
            production_markers=(str(production),),
        )
    nested_production = tmp_path / "bench-root" / "prod-knowledge"
    with pytest.raises(IsolationError, match="overlaps production"):
        validate_isolation(
            config(tmp_path, knowledge_directory=tmp_path / "bench-root"),
            production_markers=(str(nested_production),),
        )
    with pytest.raises(IsolationError, match="Redis database 0"):
        validate_isolation(config(tmp_path, redis_database=0), production_markers=())
    with pytest.raises(IsolationError, match="Milvus collection"):
        validate_isolation(
            config(tmp_path, milvus_collection="atlas_chunks"), production_markers=()
        )
