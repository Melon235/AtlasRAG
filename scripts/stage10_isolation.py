#!/usr/bin/env python3
"""Fail-closed Stage 10-B integration isolation, ownership, and cleanup CLI."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import secrets
import socket
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit

STAGE10_PROJECT_PATTERN = re.compile(r"\Aatlasrag-stage1[01](?:[a-z0-9-]*[a-z0-9])?\Z")
STAGE10_RUN_ID_PATTERN = re.compile(r"\Aatlasrag_s10b_[a-f0-9]{16,64}\Z")
STAGE10_REDIS_OWNER_KEY = "atlasrag:test:stage10b:owner"
STAGE10_MILVUS_OWNER_COLLECTION = "atlasrag_stage10b_owner"
STAGE10_MILVUS_COLLECTION = "atlas_chunks"
STAGE10_PG_COMMENT_PREFIX = "atlasrag_stage10b_run_id:"
STAGE10_SERVICES = ("postgres", "redis", "etcd", "minio", "milvus")
STAGE10_VOLUMES = ("postgres_data", "etcd_data", "minio_data", "milvus_data")
STAGE10_DEFAULT_PORTS = frozenset({5432, 6379, 19530, 9091, 8080})
STAGE10_CONFLICTING_ENV_VARS = (
    "ATLASRAG_ENV_FILE",
    "ATLASRAG_POSTGRES_DSN",
    "ATLASRAG_REDIS_URL",
    "ATLASRAG_TEST_REDIS_URL",
    "ATLASRAG_MILVUS_URI",
    "ATLASRAG_BIND_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "REDIS_PORT",
    "MILVUS_PORT",
    "MILVUS_HEALTH_PORT",
    "SEARXNG_PORT",
    "SEARXNG_SECRET",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "ATLASRAG_STAGE10B_ISOLATED",
    "ATLASRAG_STAGE10B_RUN_ID",
    "ATLASRAG_STAGE10B_OWNER_TOKEN",
    "ATLASRAG_STAGE10B_COMPOSE_PROJECT",
    "ATLASRAG_STAGE10B_PG_CONTAINER",
    "ATLASRAG_STAGE10B_REDIS_CONTAINER",
    "ATLASRAG_STAGE10B_MILVUS_CONTAINER",
    "ATLASRAG_STAGE10B_ETCD_CONTAINER",
    "ATLASRAG_STAGE10B_MINIO_CONTAINER",
    "ATLASRAG_STAGE10B_ENV_FILE",
)


class Stage10IsolationError(RuntimeError):
    """Raised when Stage 10-B isolation or positive ownership fails."""


def _require_loopback(host: str | None, service: str) -> str:
    if host is None or not host:
        raise Stage10IsolationError(f"Stage 10-B {service} host must be loopback")
    if host.casefold() == "localhost":
        return host
    try:
        parsed = ip_address(host)
    except ValueError:
        raise Stage10IsolationError(
            f"Stage 10-B {service} host must be loopback"
        ) from None
    if not parsed.is_loopback:
        raise Stage10IsolationError(f"Stage 10-B {service} host must be loopback")
    return host


def validate_stage10_project_name(project_name: str) -> str:
    """Enforce the explicit Stage 10-B Compose project naming contract."""

    if (
        not isinstance(project_name, str)
        or len(project_name) > 63
        or STAGE10_PROJECT_PATTERN.fullmatch(project_name) is None
    ):
        raise Stage10IsolationError(
            "COMPOSE_PROJECT_NAME must match ^atlasrag-stage1[01](?:[a-z0-9-]*[a-z0-9])?$ "
            "(development/Stage 9 project names are forbidden)"
        )
    return project_name


def default_stage10_project_name() -> str:
    """Generate one unique Compose project name per Stage 10-B invocation."""

    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"atlasrag-stage10b-local-{stamp}-{os.getpid()}-{secrets.token_hex(4)}"


def default_lock_directory(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    override = env.get("STAGE10_LOCK_DIR")
    if override:
        return Path(override)
    base = env.get("XDG_RUNTIME_DIR") or env.get("TMPDIR") or "/tmp"
    return Path(base) / f"atlasrag-stage10b-locks-{os.getuid()}"


def check_no_conflicting_shell_env(environ: Mapping[str, str]) -> None:
    """Fail pre-flight if caller exports env vars that collide with Stage 10-B."""

    for name in STAGE10_CONFLICTING_ENV_VARS:
        value = environ.get(name)
        if value:
            raise Stage10IsolationError(
                f"Refusing Stage 10-B run with inherited shell override {name}; "
                "unset conflicting shell variables so Stage 10-B uses an isolated temporary config"
            )
    collection = environ.get("ATLASRAG_MILVUS_COLLECTION")
    if collection and collection != STAGE10_MILVUS_COLLECTION:
        raise Stage10IsolationError(
            "ATLASRAG_MILVUS_COLLECTION must match atlas_chunks for Stage 10-B"
        )


def parse_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE lines from an isolated env file."""

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        values[key.strip()] = val.strip()
    return values


def resolve_and_validate_effective_env(
    env_file: Path,
    environ: Mapping[str, str],
    *,
    repository_root: Path | None = None,
) -> dict[str, str]:
    """Validate effective endpoints after shell-over-env-file precedence."""

    if repository_root is not None:
        default_dev_env = (repository_root / "deploy" / "local" / ".env").resolve()
        if env_file.resolve() == default_dev_env:
            raise Stage10IsolationError(
                "Stage 10-B must not use development deploy/local/.env"
            )
    file_values = parse_env_file(env_file)
    effective = dict(file_values)
    for key in (
        "ATLASRAG_POSTGRES_DSN",
        "ATLASRAG_REDIS_URL",
        "ATLASRAG_TEST_REDIS_URL",
        "ATLASRAG_MILVUS_URI",
        "ATLASRAG_MILVUS_COLLECTION",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "REDIS_PORT",
        "MILVUS_PORT",
        "MILVUS_HEALTH_PORT",
    ):
        shell_val = environ.get(key)
        if shell_val:
            if key in file_values and file_values[key] != shell_val:
                raise Stage10IsolationError(
                    f"Conflicting shell and env-file values detected for {key}"
                )
            effective[key] = shell_val

    required = (
        "ATLASRAG_POSTGRES_DSN",
        "ATLASRAG_REDIS_URL",
        "ATLASRAG_TEST_REDIS_URL",
        "ATLASRAG_MILVUS_URI",
    )
    for key in required:
        if not effective.get(key):
            raise Stage10IsolationError(f"Missing required Stage 10-B setting: {key}")

    pg_url = urlsplit(effective["ATLASRAG_POSTGRES_DSN"])
    if pg_url.scheme not in {"postgresql", "postgresql+psycopg"}:
        raise Stage10IsolationError("ATLASRAG_POSTGRES_DSN must use postgresql")
    _require_loopback(pg_url.hostname, "PostgreSQL")
    pg_port = pg_url.port or 5432
    pg_db = pg_url.path.lstrip("/")
    if not pg_db or pg_db == "postgres":
        raise Stage10IsolationError("Stage 10-B PostgreSQL DB must not be postgres")

    redis_url = urlsplit(effective["ATLASRAG_REDIS_URL"])
    test_redis_url = urlsplit(effective["ATLASRAG_TEST_REDIS_URL"])
    _validate_redis_url_pair(redis_url, test_redis_url)

    milvus_url = urlsplit(effective["ATLASRAG_MILVUS_URI"])
    if milvus_url.scheme not in {"http", "https"}:
        raise Stage10IsolationError("ATLASRAG_MILVUS_URI must use HTTP(S)")
    _require_loopback(milvus_url.hostname, "Milvus")
    milvus_port = milvus_url.port or 19530

    if effective.get("POSTGRES_PORT") and int(effective["POSTGRES_PORT"]) != pg_port:
        raise Stage10IsolationError(
            "POSTGRES_PORT conflicts with ATLASRAG_POSTGRES_DSN"
        )
    if effective.get("REDIS_PORT") and int(effective["REDIS_PORT"]) != (
        redis_url.port or 6379
    ):
        raise Stage10IsolationError("REDIS_PORT conflicts with ATLASRAG_REDIS_URL")
    if effective.get("MILVUS_PORT") and int(effective["MILVUS_PORT"]) != milvus_port:
        raise Stage10IsolationError("MILVUS_PORT conflicts with ATLASRAG_MILVUS_URI")
    collection = effective.get("ATLASRAG_MILVUS_COLLECTION", STAGE10_MILVUS_COLLECTION)
    if collection != STAGE10_MILVUS_COLLECTION:
        raise Stage10IsolationError(
            "ATLASRAG_MILVUS_COLLECTION must be atlas_chunks for Stage 10-B"
        )
    return effective


def _validate_redis_url_pair(app_url: SplitResult, test_url: SplitResult) -> None:
    if app_url.scheme not in {"redis", "rediss"} or test_url.scheme not in {
        "redis",
        "rediss",
    }:
        raise Stage10IsolationError("Stage 10-B Redis URLs must use redis/rediss")
    app_host = _require_loopback(app_url.hostname, "Redis").casefold()
    test_host = _require_loopback(test_url.hostname, "Redis test").casefold()
    app_port = app_url.port or 6379
    test_port = test_url.port or 6379
    if (app_url.scheme, app_host, app_port) != (
        test_url.scheme,
        test_host,
        test_port,
    ):
        raise Stage10IsolationError(
            "ATLASRAG_TEST_REDIS_URL must match ATLASRAG_REDIS_URL host and port"
        )
    if app_url.query or app_url.fragment or app_url.path != "/0":
        raise Stage10IsolationError(
            "ATLASRAG_REDIS_URL must explicitly target isolated DB /0"
        )
    if test_url.query or test_url.fragment or test_url.path != "/15":
        raise Stage10IsolationError(
            "ATLASRAG_TEST_REDIS_URL must explicitly target DB /15"
        )


def allocate_stage10_ports(
    *,
    excluded: frozenset[int] = STAGE10_DEFAULT_PORTS,
    count: int = 5,
) -> tuple[int, ...]:
    """Allocate distinct unused loopback ports outside default dev/pilot ports."""

    selected: list[int] = []
    held: list[socket.socket] = []
    try:
        while len(selected) < count:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
            if port in excluded or port in selected:
                sock.close()
                continue
            selected.append(port)
            held.append(sock)
    finally:
        for sock in held:
            sock.close()
    return tuple(selected)


def _generate_runtime_files(
    *,
    runtime_dir: Path,
    repository_root: Path,
    project_name: str,
    run_id: str,
) -> Path:
    short_id = run_id.rsplit("_", 1)[-1][:12]
    pg_port, redis_port, milvus_port, milvus_health_port, searxng_port = (
        allocate_stage10_ports()
    )
    pg_db = f"atlasrag_s10_{short_id}"
    pg_user = f"atlasrag_s10_{short_id}"
    pg_password = secrets.token_hex(16)
    minio_user = f"s10minio_{short_id}"
    minio_password = secrets.token_hex(16)
    searxng_secret = secrets.token_hex(24)
    owner_token = secrets.token_hex(24)

    env_path = runtime_dir / "stage10b.env"
    env_lines = [
        "ATLASRAG_BIND_HOST=127.0.0.1",
        f"COMPOSE_PROJECT_NAME={project_name}",
        f"POSTGRES_DB={pg_db}",
        f"POSTGRES_USER={pg_user}",
        f"POSTGRES_PASSWORD={pg_password}",
        f"POSTGRES_PORT={pg_port}",
        f"REDIS_PORT={redis_port}",
        f"MILVUS_PORT={milvus_port}",
        f"MILVUS_HEALTH_PORT={milvus_health_port}",
        f"MINIO_ROOT_USER={minio_user}",
        f"MINIO_ROOT_PASSWORD={minio_password}",
        f"SEARXNG_PORT={searxng_port}",
        f"SEARXNG_SECRET={searxng_secret}",
        f"ATLASRAG_POSTGRES_DSN=postgresql://{pg_user}:{pg_password}@127.0.0.1:{pg_port}/{pg_db}",
        f"ATLASRAG_REDIS_URL=redis://127.0.0.1:{redis_port}/0",
        f"ATLASRAG_TEST_REDIS_URL=redis://127.0.0.1:{redis_port}/15",
        f"ATLASRAG_MILVUS_URI=http://127.0.0.1:{milvus_port}",
        f"ATLASRAG_MILVUS_COLLECTION={STAGE10_MILVUS_COLLECTION}",
        "ATLASRAG_EMBEDDING_DIMENSION=1024",
        f"ATLASRAG_SEARXNG_URL=http://127.0.0.1:{searxng_port}/",
        "ATLASRAG_POSTGRES_WAIT_TIMEOUT_SECONDS=120",
        "ATLASRAG_POSTGRES_PROBE_TIMEOUT_SECONDS=2",
        "ATLASRAG_POSTGRES_POLL_SECONDS=0.5",
    ]
    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)

    resolve_and_validate_effective_env(
        env_path,
        {},
        repository_root=repository_root,
    )

    override_path = runtime_dir / "compose.override.yaml"
    override_lines = [
        "services:",
    ]
    for svc in STAGE10_SERVICES:
        override_lines.extend(
            [
                f"  {svc}:",
                "    labels:",
                f'      atlasrag.stage10b.run_id: "{run_id}"',
                f'      atlasrag.stage10b.project: "{project_name}"',
            ]
        )
    override_lines.extend(
        [
            "networks:",
            "  atlasrag_internal:",
            "    labels:",
            f'      atlasrag.stage10b.run_id: "{run_id}"',
            f'      atlasrag.stage10b.project: "{project_name}"',
            "volumes:",
        ]
    )
    for vol in STAGE10_VOLUMES:
        override_lines.extend(
            [
                f"  {vol}:",
                "    labels:",
                f'      atlasrag.stage10b.run_id: "{run_id}"',
                f'      atlasrag.stage10b.project: "{project_name}"',
            ]
        )
    override_path.write_text("\n".join(override_lines) + "\n", encoding="utf-8")

    meta_path = runtime_dir / "meta.json"
    meta = {
        "project_name": project_name,
        "run_id": run_id,
        "owner_token": owner_token,
        "env_file": str(env_path),
        "override_file": str(override_path),
        "pg_host": "127.0.0.1",
        "pg_port": pg_port,
        "pg_db": pg_db,
        "pg_user": pg_user,
        "redis_host": "127.0.0.1",
        "redis_port": redis_port,
        "milvus_host": "127.0.0.1",
        "milvus_port": milvus_port,
    }
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    meta_path.chmod(0o600)
    return meta_path


def _run_docker(args: list[str], *, env: Mapping[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["docker", *args],
        env=dict(os.environ if env is None else env),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "docker error"
        raise Stage10IsolationError(f"docker {' '.join(args[:2])} failed: {detail}")
    return completed.stdout.strip()


def preflight_empty_project(
    *,
    project_name: str,
    compose_file: Path,
    env_file: Path,
    override_file: Path,
) -> None:
    """Verify zero pre-existing containers, volumes, or networks for project."""

    validate_stage10_project_name(project_name)
    _run_docker(["info"])
    compose_ps = _run_docker(
        [
            "compose",
            "-p",
            project_name,
            "--env-file",
            str(env_file),
            "-f",
            str(compose_file),
            "-f",
            str(override_file),
            "ps",
            "-a",
            "-q",
        ]
    )
    if compose_ps:
        raise Stage10IsolationError(
            f"Pre-existing Compose containers found for project {project_name}; refusing provisioning"
        )
    labeled_containers = _run_docker(
        [
            "ps",
            "-a",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project_name}",
        ]
    )
    if labeled_containers:
        raise Stage10IsolationError(
            f"Pre-existing containers found for project {project_name}; refusing provisioning"
        )
    labeled_volumes = _run_docker(
        [
            "volume",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project_name}",
        ]
    )
    if labeled_volumes:
        raise Stage10IsolationError(
            f"Pre-existing volumes found for project {project_name}; refusing provisioning"
        )
    labeled_networks = _run_docker(
        [
            "network",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project_name}",
        ]
    )
    if labeled_networks:
        raise Stage10IsolationError(
            f"Pre-existing networks found for project {project_name}; refusing provisioning"
        )


def _inspect_single_container(container_id: str) -> dict[str, Any]:
    raw = _run_docker(["inspect", container_id])
    parsed = json.loads(raw)
    if isinstance(parsed, list):
        if len(parsed) != 1 or not isinstance(parsed[0], dict):
            raise Stage10IsolationError(
                f"Unexpected docker inspect output for {container_id}"
            )
        return parsed[0]
    if isinstance(parsed, dict):
        return parsed
    raise Stage10IsolationError(f"Unexpected docker inspect format for {container_id}")


def verify_compose_containers(
    *,
    project_name: str,
    run_id: str,
    compose_file: Path,
    env_file: Path,
    override_file: Path,
    expected_pg_port: int,
    expected_pg_db: str,
    expected_pg_user: str,
    expected_redis_port: int,
    expected_milvus_port: int,
    check_compose_port: bool = True,
) -> dict[str, str]:
    """Verify Compose service identity, run_id labels, loopback bindings, and published ports."""

    validate_stage10_project_name(project_name)
    if STAGE10_RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise Stage10IsolationError("Invalid Stage 10-B run_id")

    containers: dict[str, str] = {}
    for service in STAGE10_SERVICES:
        ids = [
            line.strip()
            for line in _run_docker(
                [
                    "compose",
                    "-p",
                    project_name,
                    "--env-file",
                    str(env_file),
                    "-f",
                    str(compose_file),
                    "-f",
                    str(override_file),
                    "ps",
                    "-q",
                    service,
                ]
            ).splitlines()
            if line.strip()
        ]
        if len(ids) != 1:
            raise Stage10IsolationError(
                f"Expected exactly 1 running container for {service} in {project_name}, got {len(ids)}"
            )
        cid = ids[0]
        info = _inspect_single_container(cid)
        state = info.get("State", {})
        if not isinstance(state, dict) or state.get("Running") is not True:
            raise Stage10IsolationError(f"Container {service} ({cid}) is not running")
        config = info.get("Config", {})
        labels = config.get("Labels", {}) if isinstance(config, dict) else {}
        if not isinstance(labels, dict):
            raise Stage10IsolationError(f"Missing labels on {service} ({cid})")
        if labels.get("com.docker.compose.project") != project_name:
            raise Stage10IsolationError(f"Container {cid} project label mismatch")
        if labels.get("com.docker.compose.service") != service:
            raise Stage10IsolationError(f"Container {cid} service label mismatch")
        if labels.get("atlasrag.stage10b.run_id") != run_id:
            raise Stage10IsolationError(
                f"Container {cid} run_id ownership label mismatch"
            )

        containers[service] = cid

        if service == "postgres":
            _verify_container_port(
                info=info,
                service="postgres",
                internal_port=5432,
                expected_port=expected_pg_port,
                project_name=project_name,
                compose_file=compose_file,
                env_file=env_file,
                override_file=override_file,
                check_compose_port=check_compose_port,
            )
            env_list = config.get("Env", []) if isinstance(config, dict) else []
            env_dict = _split_env_entries(env_list)
            if env_dict.get("POSTGRES_DB") != expected_pg_db or (
                env_dict.get("POSTGRES_USER") != expected_pg_user
            ):
                raise Stage10IsolationError(
                    "PostgreSQL container POSTGRES_DB/POSTGRES_USER mismatch with endpoint DSN"
                )
        elif service == "redis":
            _verify_container_port(
                info=info,
                service="redis",
                internal_port=6379,
                expected_port=expected_redis_port,
                project_name=project_name,
                compose_file=compose_file,
                env_file=env_file,
                override_file=override_file,
                check_compose_port=check_compose_port,
            )
        elif service == "milvus":
            _verify_container_port(
                info=info,
                service="milvus",
                internal_port=19530,
                expected_port=expected_milvus_port,
                project_name=project_name,
                compose_file=compose_file,
                env_file=env_file,
                override_file=override_file,
                check_compose_port=check_compose_port,
            )

    return containers


def _split_env_entries(entries: object) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not isinstance(entries, list):
        return parsed
    for item in entries:
        if isinstance(item, str) and "=" in item:
            key, val = item.split("=", 1)
            parsed[key] = val
    return parsed


def _verify_container_port(
    *,
    info: dict[str, Any],
    service: str,
    internal_port: int,
    expected_port: int,
    project_name: str,
    compose_file: Path,
    env_file: Path,
    override_file: Path,
    check_compose_port: bool,
) -> None:
    ports = info.get("NetworkSettings", {}).get("Ports", {})
    if not isinstance(ports, dict):
        raise Stage10IsolationError(f"Missing published ports for {service}")
    bindings = ports.get(f"{internal_port}/tcp")
    if not isinstance(bindings, list) or len(bindings) != 1:
        raise Stage10IsolationError(
            f"Expected single loopback port binding on {service}:{internal_port}/tcp"
        )
    host_ip = str(bindings[0].get("HostIp", ""))
    _require_loopback(host_ip, service)
    actual_port = int(str(bindings[0].get("HostPort", "0")))
    if actual_port != expected_port:
        raise Stage10IsolationError(
            f"Published port mismatch for {service}: expected {expected_port}, observed {actual_port}"
        )
    if check_compose_port:
        port_out = _run_docker(
            [
                "compose",
                "-p",
                project_name,
                "--env-file",
                str(env_file),
                "-f",
                str(compose_file),
                "-f",
                str(override_file),
                "port",
                service,
                str(internal_port),
            ]
        )
        host_part, _, port_part = port_out.strip().rpartition(":")
        _require_loopback(host_part.strip("[]"), service)
        if int(port_part) != expected_port:
            raise Stage10IsolationError(
                f"docker compose port mismatch for {service}: expected {expected_port}, got {port_part}"
            )


def _verify_exported_containers_direct(
    *,
    environ: Mapping[str, str],
    project_name: str,
    run_id: str,
    pg_port: int,
    pg_db: str,
    pg_user: str,
    redis_port: int,
    milvus_port: int,
) -> None:
    expected_map = {
        "postgres": environ.get("ATLASRAG_STAGE10B_PG_CONTAINER", ""),
        "redis": environ.get("ATLASRAG_STAGE10B_REDIS_CONTAINER", ""),
        "milvus": environ.get("ATLASRAG_STAGE10B_MILVUS_CONTAINER", ""),
        "etcd": environ.get("ATLASRAG_STAGE10B_ETCD_CONTAINER", ""),
        "minio": environ.get("ATLASRAG_STAGE10B_MINIO_CONTAINER", ""),
    }
    for service, cid in expected_map.items():
        if not cid:
            raise Stage10IsolationError(
                f"Missing Stage 10-B container identity for {service}; direct unowned pytest is forbidden"
            )
        info = _inspect_single_container(cid)
        state = info.get("State", {})
        if not isinstance(state, dict) or state.get("Running") is not True:
            raise Stage10IsolationError(f"Container {service} ({cid}) is not running")
        config = info.get("Config", {})
        labels = config.get("Labels", {}) if isinstance(config, dict) else {}
        if not isinstance(labels, dict):
            raise Stage10IsolationError(f"Container {cid} missing labels")
        if (
            labels.get("com.docker.compose.project") != project_name
            or labels.get("com.docker.compose.service") != service
            or labels.get("atlasrag.stage10b.run_id") != run_id
        ):
            raise Stage10IsolationError(
                f"Container {service} ({cid}) failed Stage 10-B ownership label check"
            )
        ports = info.get("NetworkSettings", {}).get("Ports", {})
        if service == "postgres":
            _assert_port_binding(ports, 5432, pg_port, "postgres")
            env_dict = _split_env_entries(config.get("Env", []))
            if (
                env_dict.get("POSTGRES_DB") != pg_db
                or env_dict.get("POSTGRES_USER") != pg_user
            ):
                raise Stage10IsolationError("PostgreSQL container env mismatch")
        elif service == "redis":
            _assert_port_binding(ports, 6379, redis_port, "redis")
        elif service == "milvus":
            _assert_port_binding(ports, 19530, milvus_port, "milvus")


def _assert_port_binding(
    ports: object, internal_port: int, expected_port: int, service: str
) -> None:
    if not isinstance(ports, dict):
        raise Stage10IsolationError(f"Missing ports on {service}")
    bindings = ports.get(f"{internal_port}/tcp")
    if not isinstance(bindings, list) or len(bindings) != 1:
        raise Stage10IsolationError(
            f"Missing loopback binding on {service}:{internal_port}"
        )
    _require_loopback(str(bindings[0].get("HostIp", "")), service)
    if int(str(bindings[0].get("HostPort", "0"))) != expected_port:
        raise Stage10IsolationError(
            f"Published port mismatch for {service}:{internal_port}"
        )


async def establish_service_ownership(meta_path: Path) -> dict[str, str]:
    """Mint positive per-service ownership markers inside freshly created PG/Redis/Milvus."""

    import psycopg
    from psycopg import sql as pg_sql
    from pymilvus import MilvusClient  # type: ignore[import-untyped]
    from redis.asyncio import Redis

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    env_file = Path(meta["env_file"])
    file_env = resolve_and_validate_effective_env(env_file, {})
    run_id = meta["run_id"]
    owner_token = meta["owner_token"]
    pg_dsn = file_env["ATLASRAG_POSTGRES_DSN"]
    pg_db = meta["pg_db"]
    redis_url = file_env["ATLASRAG_REDIS_URL"]
    test_redis_url = file_env["ATLASRAG_TEST_REDIS_URL"]
    milvus_uri = file_env["ATLASRAG_MILVUS_URI"]
    expected_marker = f"{STAGE10_PG_COMMENT_PREFIX}{run_id}"

    # 1) PostgreSQL positive ownership: refuse pre-existing atlasrag_test_* DBs and stamp DB comment.
    conn = await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True)
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE 'atlasrag_test_%'"
            )
            existing_test_dbs = await cur.fetchall()
            if existing_test_dbs:
                raise Stage10IsolationError(
                    "Refusing Stage 10-B PostgreSQL ownership: pre-existing atlasrag_test_* DBs present"
                )
            await cur.execute(
                pg_sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    pg_sql.Identifier(pg_db),
                    pg_sql.Literal(expected_marker),
                )
            )
    finally:
        await conn.close()

    # 2) Redis positive ownership: assert DB /0 & /15 empty before minting NX owner key.
    redis_0 = Redis.from_url(
        redis_url,
        decode_responses=False,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )
    try:
        if await redis_0.ping() is not True:
            raise Stage10IsolationError("Stage 10-B Redis ping failed")
        if await redis_0.dbsize() != 0:
            raise Stage10IsolationError(
                "Refusing Stage 10-B Redis ownership: target DB 0 is not empty"
            )
        if await redis_0.set(STAGE10_REDIS_OWNER_KEY, owner_token, nx=True) is not True:
            raise Stage10IsolationError(
                "Failed to establish exclusive Redis DB 0 owner key"
            )
    finally:
        await redis_0.aclose()

    redis_15 = Redis.from_url(
        test_redis_url,
        decode_responses=False,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )
    try:
        if await redis_15.dbsize() != 0:
            raise Stage10IsolationError(
                "Refusing Stage 10-B Redis ownership: target DB 15 is not empty"
            )
    finally:
        await redis_15.aclose()

    # 3) Milvus positive ownership: assert 0 pre-existing collections, create run sentinel collection.
    def _stamp_milvus() -> None:
        from pymilvus import DataType

        client = MilvusClient(uri=milvus_uri, timeout=10.0)
        try:
            existing = client.list_collections()
            if existing:
                raise Stage10IsolationError(
                    "Refusing Stage 10-B Milvus ownership: non-empty Milvus instance has existing collections"
                )
            schema = MilvusClient.create_schema(
                auto_id=True,
                enable_dynamic_field=False,
                description=expected_marker,
            )
            schema.add_field(field_name="id", datatype=DataType.INT64, is_primary=True)
            schema.add_field(field_name="vec", datatype=DataType.FLOAT_VECTOR, dim=2)
            client.create_collection(
                collection_name=STAGE10_MILVUS_OWNER_COLLECTION,
                schema=schema,
            )
            desc = client.describe_collection(STAGE10_MILVUS_OWNER_COLLECTION)
            if desc.get("description") != expected_marker:
                raise Stage10IsolationError(
                    "Failed to verify Stage 10-B Milvus sentinel collection description"
                )
        finally:
            client.close()

    await asyncio.to_thread(_stamp_milvus)
    return {
        "pg_marker": expected_marker,
        "milvus_sentinel": STAGE10_MILVUS_OWNER_COLLECTION,
    }


def verify_stage10_service_ownership(
    environ: Mapping[str, str] | None = None,
    *,
    verify_docker: bool = True,
    verify_postgres: bool = True,
    verify_redis: bool = True,
    verify_milvus: bool = True,
) -> dict[str, str]:
    """Positively verify Stage 10-B container, endpoint, and in-service ownership."""

    env = os.environ if environ is None else environ
    if env.get("ATLASRAG_STAGE10B_ISOLATED") != "1":
        raise Stage10IsolationError(
            "Stage 10-B ownership proof missing (ATLASRAG_STAGE10B_ISOLATED != 1); "
            "run via scripts/stage10_verify.sh or make test-stage10-integration"
        )
    run_id = env.get("ATLASRAG_STAGE10B_RUN_ID", "")
    owner_token = env.get("ATLASRAG_STAGE10B_OWNER_TOKEN", "")
    project_name = env.get("ATLASRAG_STAGE10B_COMPOSE_PROJECT", "")
    if not run_id or STAGE10_RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise Stage10IsolationError("Missing or invalid ATLASRAG_STAGE10B_RUN_ID")
    if not owner_token:
        raise Stage10IsolationError("Missing ATLASRAG_STAGE10B_OWNER_TOKEN")
    validate_stage10_project_name(project_name)

    raw_env_file = env.get("ATLASRAG_STAGE10B_ENV_FILE") or env.get(
        "ATLASRAG_ENV_FILE", ""
    )
    if not raw_env_file:
        raise Stage10IsolationError("Missing Stage 10-B temporary env file path")
    env_file = Path(raw_env_file)
    if not env_file.is_file():
        raise Stage10IsolationError("Stage 10-B temporary env file does not exist")
    repo_root = Path(__file__).resolve().parents[1]
    effective = resolve_and_validate_effective_env(
        env_file, env, repository_root=repo_root
    )

    pg_url = urlsplit(effective["ATLASRAG_POSTGRES_DSN"])
    pg_port = pg_url.port or 5432
    pg_db = pg_url.path.lstrip("/")
    pg_user = pg_url.username or ""
    redis_url = urlsplit(effective["ATLASRAG_REDIS_URL"])
    redis_port = redis_url.port or 6379
    milvus_url = urlsplit(effective["ATLASRAG_MILVUS_URI"])
    milvus_port = milvus_url.port or 19530
    expected_marker = f"{STAGE10_PG_COMMENT_PREFIX}{run_id}"

    if verify_docker:
        _verify_exported_containers_direct(
            environ=env,
            project_name=project_name,
            run_id=run_id,
            pg_port=pg_port,
            pg_db=pg_db,
            pg_user=pg_user,
            redis_port=redis_port,
            milvus_port=milvus_port,
        )

    if verify_postgres:
        import psycopg

        with psycopg.Connection.connect(
            effective["ATLASRAG_POSTGRES_DSN"], connect_timeout=2
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
                    (pg_db,),
                )
                row = cur.fetchone()
                if not row or row[0] != expected_marker:
                    raise Stage10IsolationError(
                        "Stage 10-B PostgreSQL instance failed run_id ownership check"
                    )

    if verify_redis:
        import redis as sync_redis

        client = sync_redis.Redis.from_url(
            effective["ATLASRAG_REDIS_URL"],
            decode_responses=False,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
        try:
            actual = client.get(STAGE10_REDIS_OWNER_KEY)
            if actual != owner_token.encode("utf-8"):
                raise Stage10IsolationError(
                    "Stage 10-B Redis DB 0 owner token mismatch or absent"
                )
        finally:
            client.close()

    if verify_milvus:
        from pymilvus import MilvusClient

        mc = MilvusClient(uri=effective["ATLASRAG_MILVUS_URI"], timeout=5.0)
        try:
            if not mc.has_collection(STAGE10_MILVUS_OWNER_COLLECTION, timeout=5.0):
                raise Stage10IsolationError(
                    "Stage 10-B Milvus ownership sentinel collection absent"
                )
            desc = mc.describe_collection(STAGE10_MILVUS_OWNER_COLLECTION, timeout=5.0)
            if desc.get("description") != expected_marker:
                raise Stage10IsolationError(
                    "Stage 10-B Milvus ownership sentinel description mismatch"
                )
        finally:
            mc.close()

    return {
        "project_name": project_name,
        "run_id": run_id,
        "owner_token": owner_token,
        "pg_marker": expected_marker,
    }


def cleanup_run_owned_resources(
    *,
    project_name: str,
    run_id: str,
    show_logs: bool = False,
) -> dict[str, Any]:
    """Delete ONLY Docker containers, volumes, and networks owned by this run_id."""

    validate_stage10_project_name(project_name)
    if STAGE10_RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise Stage10IsolationError("Invalid run_id for cleanup")

    owned_filter = [
        "--filter",
        f"label=com.docker.compose.project={project_name}",
        "--filter",
        f"label=atlasrag.stage10b.run_id={run_id}",
    ]

    containers = [
        c.strip()
        for c in _run_docker(["ps", "-a", "-q", *owned_filter]).splitlines()
        if c.strip()
    ]
    volumes = [
        v.strip()
        for v in _run_docker(["volume", "ls", "-q", *owned_filter]).splitlines()
        if v.strip()
    ]
    networks = [
        n.strip()
        for n in _run_docker(["network", "ls", "-q", *owned_filter]).splitlines()
        if n.strip()
    ]

    if show_logs and containers:
        for cid in containers:
            subprocess.run(
                ["docker", "logs", "--tail", "200", cid],
                check=False,
                timeout=15,
            )

    if containers:
        _run_docker(["rm", "-f", "-v", *containers])
    if volumes:
        _run_docker(["volume", "rm", *volumes])
    if networks:
        _run_docker(["network", "rm", *networks])

    rem_c = [
        c.strip()
        for c in _run_docker(["ps", "-a", "-q", *owned_filter]).splitlines()
        if c.strip()
    ]
    rem_v = [
        v.strip()
        for v in _run_docker(["volume", "ls", "-q", *owned_filter]).splitlines()
        if v.strip()
    ]
    rem_n = [
        n.strip()
        for n in _run_docker(["network", "ls", "-q", *owned_filter]).splitlines()
        if n.strip()
    ]
    remaining = len(rem_c) + len(rem_v) + len(rem_n)
    evidence = {
        "project": project_name,
        "run_id": run_id,
        "containers_removed": len(containers),
        "volumes_removed": len(volumes),
        "networks_removed": len(networks),
        "remaining": remaining,
    }
    print(
        "Stage 10-B cleanup_evidence: "
        f"project={project_name} run_id={run_id} "
        f"containers_removed={len(containers)} volumes_removed={len(volumes)} "
        f"networks_removed={len(networks)} remaining={remaining}",
        flush=True,
    )
    if remaining != 0:
        raise Stage10IsolationError(
            f"Run-owned Docker resources remained after cleanup: {evidence}"
        )
    return evidence


def _cmd_prepare(args: argparse.Namespace) -> int:
    check_no_conflicting_shell_env(os.environ)
    project_name = args.project_name or default_stage10_project_name()
    validate_stage10_project_name(project_name)
    run_id = args.run_id or f"atlasrag_s10b_{secrets.token_hex(12)}"
    if STAGE10_RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise Stage10IsolationError("Invalid STAGE10_RUN_ID")
    meta_path = _generate_runtime_files(
        runtime_dir=Path(args.runtime_dir),
        repository_root=Path(args.repository_root),
        project_name=project_name,
        run_id=run_id,
    )
    print(str(meta_path))
    return 0


def _cmd_hold_locks(args: argparse.Namespace) -> int:
    meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    project_name = validate_stage10_project_name(meta["project_name"])
    run_id = meta["run_id"]
    lock_dir = default_lock_directory(os.environ)
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(lock_dir, 0o700)

    project_lock = lock_dir / f"project-{project_name}.lock"
    run_lock = lock_dir / f"run-{run_id}.lock"
    fds: list[int] = []
    try:
        for lock_path in (project_lock, run_lock):
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise Stage10IsolationError(
                    f"Concurrent/conflicting Stage 10-B lock held: {lock_path.name}"
                ) from None
            fds.append(fd)
        print("LOCKED", flush=True)
        sys.stdin.read()
        return 0
    finally:
        for fd in fds:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _cmd_preflight(args: argparse.Namespace) -> int:
    meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    preflight_empty_project(
        project_name=meta["project_name"],
        compose_file=Path(args.compose_file),
        env_file=Path(meta["env_file"]),
        override_file=Path(meta["override_file"]),
    )
    return 0


def _cmd_verify_containers(args: argparse.Namespace) -> int:
    meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    containers = verify_compose_containers(
        project_name=meta["project_name"],
        run_id=meta["run_id"],
        compose_file=Path(args.compose_file),
        env_file=Path(meta["env_file"]),
        override_file=Path(meta["override_file"]),
        expected_pg_port=int(meta["pg_port"]),
        expected_pg_db=str(meta["pg_db"]),
        expected_pg_user=str(meta["pg_user"]),
        expected_redis_port=int(meta["redis_port"]),
        expected_milvus_port=int(meta["milvus_port"]),
    )
    print(json.dumps(containers))
    return 0


def _cmd_establish(args: argparse.Namespace) -> int:
    asyncio.run(establish_service_ownership(Path(args.meta)))
    return 0


def _cmd_cleanup(args: argparse.Namespace) -> int:
    meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    cleanup_run_owned_resources(
        project_name=meta["project_name"],
        run_id=meta["run_id"],
        show_logs=bool(args.show_logs),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 10-B isolation helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prep = sub.add_parser("prepare")
    p_prep.add_argument("--runtime-dir", required=True)
    p_prep.add_argument("--repository-root", required=True)
    p_prep.add_argument("--project-name", default="")
    p_prep.add_argument("--run-id", default="")

    p_lock = sub.add_parser("hold-locks")
    p_lock.add_argument("--meta", required=True)

    p_pre = sub.add_parser("preflight")
    p_pre.add_argument("--meta", required=True)
    p_pre.add_argument("--compose-file", required=True)

    p_vc = sub.add_parser("verify-containers")
    p_vc.add_argument("--meta", required=True)
    p_vc.add_argument("--compose-file", required=True)

    p_est = sub.add_parser("establish")
    p_est.add_argument("--meta", required=True)

    p_cln = sub.add_parser("cleanup")
    p_cln.add_argument("--meta", required=True)
    p_cln.add_argument("--show-logs", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "prepare":
            return _cmd_prepare(args)
        if args.cmd == "hold-locks":
            return _cmd_hold_locks(args)
        if args.cmd == "preflight":
            return _cmd_preflight(args)
        if args.cmd == "verify-containers":
            return _cmd_verify_containers(args)
        if args.cmd == "establish":
            return _cmd_establish(args)
        if args.cmd == "cleanup":
            return _cmd_cleanup(args)
        return 2
    except Stage10IsolationError as error:
        print(f"Stage 10-B isolation error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
