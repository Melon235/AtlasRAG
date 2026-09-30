"""Static contracts for deterministic Stage 2 local infrastructure tooling."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPOSITORY_ROOT / "deploy" / "local" / "compose.yaml"
ENV_EXAMPLE_PATH = REPOSITORY_ROOT / "deploy" / "local" / ".env.example"
SEARXNG_SETTINGS_PATH = (
    REPOSITORY_ROOT / "deploy" / "local" / "searxng" / "settings.yml"
)
WAIT_SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "wait_for_infrastructure.py"
MIGRATION_SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "check_migration.py"
VERIFY_SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "stage2_verify.sh"
MAKEFILE_PATH = REPOSITORY_ROOT / "Makefile"


def load_readiness_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "atlasrag_stage2_readiness",
        WAIT_SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def readiness_config(readiness: ModuleType, **overrides: object) -> object:
    values: dict[str, object] = {
        "postgres_dsn": "postgresql://local.invalid/atlasrag",
        "redis_url": "redis://127.0.0.1:6379/0",
        "milvus_uri": "http://127.0.0.1:19530",
        "milvus_collection": "atlas_chunks",
        "embedding_dimension": 3,
        "searxng_url": "http://127.0.0.1:8080/",
        "wait_timeout_seconds": 1.0,
        "probe_timeout_seconds": 0.1,
        "poll_seconds": 0.1,
    }
    values.update(overrides)
    return readiness.ProbeConfig(**values)


def read_required(path: Path) -> str:
    assert path.is_file(), (
        f"missing Stage 2 tooling file: {path.relative_to(REPOSITORY_ROOT)}"
    )
    return path.read_text(encoding="utf-8")


def service_block(compose: str, service: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(service)}:\n(?P<body>.*?)(?=^  [a-z][a-z0-9_-]*:\n|^networks:\n|^volumes:\n|\Z)",
        compose,
    )
    assert match is not None, f"missing Compose service: {service}"
    return match.group("body")


def make_target(makefile: str, target: str) -> str:
    match = re.search(
        rf"(?m)^{re.escape(target)}:\s*\n(?P<body>(?:\t[^\n]*(?:\n|\Z))+)",
        makefile,
    )
    assert match is not None, f"missing Make target: {target}"
    return match.group("body")


@pytest.mark.parametrize(
    "path",
    [
        COMPOSE_PATH,
        ENV_EXAMPLE_PATH,
        SEARXNG_SETTINGS_PATH,
        WAIT_SCRIPT_PATH,
        MIGRATION_SCRIPT_PATH,
        VERIFY_SCRIPT_PATH,
    ],
)
def test_stage2_tooling_files_exist(path: Path) -> None:
    read_required(path)


def test_compose_uses_the_exact_frozen_images() -> None:
    compose = read_required(COMPOSE_PATH)
    expected_images = {
        "postgres": "postgres:17.8-alpine",
        "redis": "redis:8.2.10-alpine",
        "etcd": "quay.io/coreos/etcd:v3.5.25",
        "minio": "minio/minio:RELEASE.2024-12-18T13-15-44Z",
        "milvus": "milvusdb/milvus:v2.6.20",
        "searxng": "docker.io/searxng/searxng:2026.9.25-12f8b6515",
    }

    for service, image in expected_images.items():
        assert f"image: {image}" in service_block(compose, service)
    assert ":latest" not in compose.casefold()


def test_compose_credentials_are_required_substitutions_not_literals() -> None:
    compose = read_required(COMPOSE_PATH)
    postgres = service_block(compose, "postgres")
    minio = service_block(compose, "minio")
    milvus = service_block(compose, "milvus")
    searxng = service_block(compose, "searxng")

    assert "${POSTGRES_PASSWORD:?required}" in postgres
    assert "${MINIO_ROOT_USER:?required}" in minio
    assert "${MINIO_ROOT_PASSWORD:?required}" in minio
    assert "${MINIO_ROOT_USER:?required}" in milvus
    assert "${MINIO_ROOT_PASSWORD:?required}" in milvus
    assert "${SEARXNG_SECRET:?required}" in searxng
    for forbidden_literal in ("minioadmin", "ultrasecretkey", "changeme"):
        assert forbidden_literal not in compose.casefold()


@pytest.mark.parametrize(
    ("service", "published_port"),
    [
        ("postgres", "${POSTGRES_PORT:-5432}:5432"),
        ("redis", "${REDIS_PORT:-6379}:6379"),
        ("milvus", "${MILVUS_PORT:-19530}:19530"),
        ("milvus", "${MILVUS_HEALTH_PORT:-9091}:9091"),
        ("searxng", "${SEARXNG_PORT:-8080}:8080"),
    ],
)
def test_every_published_application_port_defaults_to_loopback(
    service: str,
    published_port: str,
) -> None:
    compose = read_required(COMPOSE_PATH)
    block = service_block(compose, service)

    assert f'"${{ATLASRAG_BIND_HOST:-127.0.0.1}}:{published_port}"' in block
    port_lines = [line.strip() for line in block.splitlines() if ":" in line]
    assert not any("0.0.0.0:" in line for line in port_lines)


def test_etcd_and_minio_have_no_host_ports() -> None:
    compose = read_required(COMPOSE_PATH)

    assert "ports:" not in service_block(compose, "etcd")
    assert "ports:" not in service_block(compose, "minio")


def test_canonical_services_use_named_volumes_and_redis_is_disposable() -> None:
    compose = read_required(COMPOSE_PATH)
    expected_mounts = {
        "postgres": "postgres_data:/var/lib/postgresql/data",
        "etcd": "etcd_data:/etcd",
        "minio": "minio_data:/minio_data",
        "milvus": "milvus_data:/var/lib/milvus",
    }
    for service, mount in expected_mounts.items():
        assert mount in service_block(compose, service)

    volume_section = compose.split("\nvolumes:\n", maxsplit=1)[1]
    for volume in ("postgres_data", "etcd_data", "minio_data", "milvus_data"):
        assert re.search(rf"(?m)^  {volume}:\s*$", volume_section)

    redis = service_block(compose, "redis")
    assert "volumes:" not in redis
    assert "--save" in redis
    assert "--appendonly" in redis
    assert re.search(r'(?m)^\s+- ""\s*$', redis)
    assert re.search(r'(?m)^\s+- "no"\s*$', redis)


def test_all_services_have_healthchecks_and_milvus_waits_for_dependencies() -> None:
    compose = read_required(COMPOSE_PATH)
    for service in ("postgres", "redis", "etcd", "minio", "milvus", "searxng"):
        assert "healthcheck:" in service_block(compose, service)

    milvus = service_block(compose, "milvus")
    assert "condition: service_healthy" in milvus
    assert re.search(r"(?ms)depends_on:.*?etcd:.*?service_healthy", milvus)
    assert re.search(r"(?ms)depends_on:.*?minio:.*?service_healthy", milvus)


def test_searxng_configuration_is_local_minimal_and_secret_free() -> None:
    settings = read_required(SEARXNG_SETTINGS_PATH)

    assert "use_default_settings: true" in settings
    assert re.search(r"(?m)^\s+limiter: false\s*$", settings)
    assert re.search(r"(?m)^\s+public_instance: false\s*$", settings)
    assert "secret_key:" not in settings
    assert "password" not in settings.casefold()
    assert "api_key" not in settings.casefold()


def test_env_example_contains_only_explicit_local_placeholders() -> None:
    source = read_required(ENV_EXAMPLE_PATH)
    values = {
        key: value
        for line in source.splitlines()
        if line and not line.startswith("#")
        for key, value in [line.split("=", maxsplit=1)]
    }
    required = {
        "ATLASRAG_BIND_HOST",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_PORT",
        "REDIS_PORT",
        "MILVUS_PORT",
        "MILVUS_HEALTH_PORT",
        "SEARXNG_PORT",
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
        "SEARXNG_SECRET",
        "ATLASRAG_POSTGRES_DSN",
        "ATLASRAG_REDIS_URL",
        "ATLASRAG_MILVUS_URI",
        "ATLASRAG_MILVUS_COLLECTION",
        "ATLASRAG_EMBEDDING_DIMENSION",
        "ATLASRAG_SEARXNG_URL",
    }
    assert required <= values.keys()
    for secret in (
        "POSTGRES_PASSWORD",
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
        "SEARXNG_SECRET",
    ):
        assert values[secret].startswith("replace-with-local-")

    gitignore = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/deploy/local/.env" in gitignore


@pytest.mark.parametrize(
    "target",
    [
        "infra-up",
        "infra-wait",
        "infra-status",
        "infra-down",
        "migrate",
        "migration-check",
        "test-integration",
        "stage2-verify",
    ],
)
def test_makefile_exposes_every_stage2_command(target: str) -> None:
    make_target(read_required(MAKEFILE_PATH), target)


def test_make_targets_preserve_data_and_make_integration_explicit() -> None:
    makefile = read_required(MAKEFILE_PATH)

    assert " up -d" in make_target(makefile, "infra-up")
    assert " compose " in make_target(makefile, "infra-status")
    assert " ps" in make_target(makefile, "infra-status")
    assert " down" in make_target(makefile, "infra-down")
    assert " -v" not in make_target(makefile, "infra-down")
    assert "alembic upgrade head" in make_target(makefile, "migrate")
    assert "scripts/check_migration.py" in make_target(makefile, "migration-check")
    assert "ATLASRAG_RUN_INTEGRATION=1" in make_target(makefile, "test-integration")
    assert "scripts/stage2_verify.sh" in make_target(makefile, "stage2-verify")
    for target in ("infra-wait", "migrate", "migration-check", "test-integration"):
        body = make_target(makefile, target)
        assert "--env-file" in body
        assert '. "$(ATLASRAG_ENV_FILE)"' not in body


def test_wait_script_uses_real_probes_and_condition_based_polling() -> None:
    source = read_required(WAIT_SCRIPT_PATH)

    assert "PostgresPool" in source
    assert "RedisCacheProvider" in source
    assert "MilvusIndexProvider" in source
    assert "urllib.request" in source
    assert "time.monotonic" in source
    assert "asyncio.sleep" in source
    assert "time.sleep" not in source
    assert "_probe_all" in source
    assert "ATLASRAG_SEARXNG_URL" in source


@pytest.mark.asyncio
async def test_postgres_probe_closes_a_pool_after_failed_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness = load_readiness_module()
    closed: list[bool] = []

    class FailingPool:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def open(self) -> None:
            raise OSError("unavailable")

        async def health(self) -> None:
            raise AssertionError("health must not run after failed open")

        async def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(readiness, "PostgresPool", FailingPool)
    config = readiness_config(readiness)

    with pytest.raises(OSError):
        await readiness._probe_postgres(config)

    assert closed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_probe_succeeds", [True, False])
async def test_total_wait_timeout_bounds_a_slow_probe_round(
    monkeypatch: pytest.MonkeyPatch,
    slow_probe_succeeds: bool,
) -> None:
    readiness = load_readiness_module()

    async def slow_probe(_config: object) -> dict[str, Exception | None]:
        await asyncio.sleep(0.05)
        failure = None if slow_probe_succeeds else OSError("unavailable")
        return {name: failure for name in ("postgres", "redis", "milvus", "searxng")}

    monkeypatch.setattr(readiness, "_probe_all", slow_probe)
    config = readiness_config(
        readiness,
        wait_timeout_seconds=0.005,
        poll_seconds=0.001,
    )
    started = time.monotonic()

    assert not await readiness.wait_for_infrastructure(config)
    assert time.monotonic() - started < 0.04


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ATLASRAG_EMBEDDING_DIMENSION", "1"),
        ("ATLASRAG_EMBEDDING_DIMENSION", "32769"),
        ("ATLASRAG_MILVUS_COLLECTION", ""),
        ("ATLASRAG_MILVUS_COLLECTION", "bad-name"),
    ],
)
def test_readiness_rejects_invalid_milvus_configuration_before_probing(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    readiness = load_readiness_module()
    environment = {
        "ATLASRAG_POSTGRES_DSN": "postgresql://local.invalid/atlasrag",
        "ATLASRAG_REDIS_URL": "redis://127.0.0.1:6379/0",
        "ATLASRAG_MILVUS_URI": "http://127.0.0.1:19530",
        "ATLASRAG_MILVUS_COLLECTION": "atlas_chunks",
        "ATLASRAG_EMBEDDING_DIMENSION": "3",
        "ATLASRAG_SEARXNG_URL": "http://127.0.0.1:8080/",
    }
    environment[name] = value
    for key, configured_value in environment.items():
        monkeypatch.setenv(key, configured_value)

    with pytest.raises(ValueError):
        readiness.config_from_environment()


@pytest.mark.asyncio
async def test_milvus_probe_closes_raw_client_when_provider_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness = load_readiness_module()
    closed: list[bool] = []

    class RawClient:
        def close(self) -> None:
            closed.append(True)

    client = RawClient()

    def client_factory(**_kwargs: object) -> RawClient:
        return client

    class BrokenProvider:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("provider construction failed")

    async def immediate_to_thread(
        function: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        assert callable(function)
        return function(*args, **kwargs)

    monkeypatch.setattr(readiness, "MilvusClient", client_factory)
    monkeypatch.setattr(readiness, "MilvusIndexProvider", BrokenProvider)
    monkeypatch.setattr(readiness.asyncio, "to_thread", immediate_to_thread)

    with pytest.raises(RuntimeError, match="provider construction failed"):
        await readiness._probe_milvus(readiness_config(readiness))

    assert closed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_redis_cleanup_failure_does_not_replace_the_primary_outcome(
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
) -> None:
    readiness = load_readiness_module()

    class FailingCloseClient:
        async def aclose(self) -> None:
            raise RuntimeError("cleanup failed")

    client = FailingCloseClient()

    class RedisFactory:
        @staticmethod
        def from_url(*_args: object, **_kwargs: object) -> FailingCloseClient:
            return client

    class FailingHealthProvider:
        def __init__(self, _client: object) -> None:
            pass

        async def health(self) -> None:
            if cancelled:
                raise asyncio.CancelledError
            raise ValueError("primary failed")

    monkeypatch.setattr(readiness, "Redis", RedisFactory)
    monkeypatch.setattr(readiness, "RedisCacheProvider", FailingHealthProvider)
    expected = asyncio.CancelledError if cancelled else ValueError

    with pytest.raises(expected) as captured:
        await readiness._probe_redis(readiness_config(readiness))

    assert any(
        "RuntimeError" in note for note in getattr(captured.value, "__notes__", ())
    )


@pytest.mark.asyncio
async def test_redis_cleanup_cancellation_supersedes_an_earlier_health_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness = load_readiness_module()
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class BlockingCloseClient:
        async def aclose(self) -> None:
            close_started.set()
            await release_close.wait()

    client = BlockingCloseClient()

    class RedisFactory:
        @staticmethod
        def from_url(*_args: object, **_kwargs: object) -> BlockingCloseClient:
            return client

    class FailingHealthProvider:
        def __init__(self, _client: object) -> None:
            pass

        async def health(self) -> None:
            raise ValueError("health failed")

    monkeypatch.setattr(readiness, "Redis", RedisFactory)
    monkeypatch.setattr(readiness, "RedisCacheProvider", FailingHealthProvider)

    probe = asyncio.create_task(readiness._probe_redis(readiness_config(readiness)))
    await close_started.wait()
    probe.cancel()
    release_close.set()

    with pytest.raises(asyncio.CancelledError):
        await probe


def test_migration_check_compares_database_revision_to_single_head() -> None:
    source = read_required(MIGRATION_SCRIPT_PATH)

    assert "get_current_head" in source
    assert "SELECT version_num FROM alembic_version" in source
    assert "ATLASRAG_POSTGRES_DSN" in source


def test_stage2_verify_has_early_trap_and_ordered_cleanup_safe_pipeline() -> None:
    source = read_required(VERIFY_SCRIPT_PATH)

    assert VERIFY_SCRIPT_PATH.stat().st_mode & stat.S_IXUSR
    trap = source.index("trap ")
    verify = source.index("make verify")
    up = source.index("make infra-up")
    wait = source.index("make infra-wait")
    migrate = source.index("make migrate")
    migration_check = source.index("make migration-check")
    integration = source.index("make test-integration")
    assert trap < verify < up < wait < migrate < migration_check < integration
    assert "make infra-down" in source
    assert "down -v" not in source


@pytest.mark.parametrize(
    ("failing_target", "expected_returncode", "expected_calls"),
    [
        (
            "",
            0,
            [
                "verify",
                "infra-up",
                "infra-wait",
                "migrate",
                "migration-check",
                "test-integration",
                "infra-down",
            ],
        ),
        ("verify", 23, ["verify", "infra-down"]),
        (
            "infra-wait",
            23,
            ["verify", "infra-up", "infra-wait", "infra-down"],
        ),
        (
            "test-integration",
            23,
            [
                "verify",
                "infra-up",
                "infra-wait",
                "migrate",
                "migration-check",
                "test-integration",
                "infra-down",
            ],
        ),
    ],
)
def test_stage2_verify_always_cleans_up_and_preserves_the_exit_code(
    tmp_path: Path,
    failing_target: str,
    expected_returncode: int,
    expected_calls: list[str],
) -> None:
    executable_directory = tmp_path / "bin"
    executable_directory.mkdir()
    call_log = tmp_path / "make-calls.txt"
    fake_make = executable_directory / "make"
    fake_make.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$1" >> "$CALL_LOG"\n'
        'if [ "${FAIL_TARGET:-}" = "$1" ]; then exit 23; fi\n',
        encoding="utf-8",
    )
    fake_make.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{executable_directory}:{os.defpath}",
        "CALL_LOG": str(call_log),
        "FAIL_TARGET": failing_target,
        "ATLASRAG_ENV_FILE": "deploy/local/.env.test-only",
    }

    result = subprocess.run(
        [str(VERIFY_SCRIPT_PATH)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_returncode, result.stdout + result.stderr
    assert call_log.read_text(encoding="utf-8").splitlines() == expected_calls
