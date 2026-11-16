"""Physical-isolation checks; never touches production resources."""

from __future__ import annotations

import socket
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class IsolationError(ValueError):
    pass


class IsolationConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    postgres_database: StrictStr
    milvus_collection: StrictStr
    redis_database: int = Field(ge=0)
    knowledge_directory: Path
    artifact_root: Path
    owned_compose_project: StrictStr


def port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def validate_isolation(
    config: IsolationConfig, *, production_markers: tuple[str, ...]
) -> None:
    if not config.postgres_database.casefold().startswith("atlasrag_bench"):
        raise IsolationError("PostgreSQL database is not benchmark-owned")
    if not config.milvus_collection.casefold().startswith("atlasrag_bench"):
        raise IsolationError("Milvus collection is not benchmark-owned")
    if config.redis_database == 0:
        raise IsolationError("Redis database 0 is reserved for local/production")
    knowledge = config.knowledge_directory.resolve()
    artifacts = config.artifact_root.resolve()
    if (
        not str(artifacts).casefold().endswith("benchmark_runs")
        and "benchmark" not in str(artifacts).casefold()
    ):
        raise IsolationError("artifact root is not benchmark-owned")
    for marker in production_markers:
        if not marker:
            continue
        protected = Path(marker).resolve()
        if (
            knowledge == protected
            or protected in knowledge.parents
            or knowledge in protected.parents
        ):
            raise IsolationError("benchmark knowledge directory overlaps production")
        if (
            artifacts == protected
            or protected in artifacts.parents
            or artifacts in protected.parents
        ):
            raise IsolationError("benchmark artifact root overlaps production")
    if not config.owned_compose_project.casefold().startswith("atlasrag-stage9"):
        raise IsolationError("Compose project is not Stage 9 owned")
