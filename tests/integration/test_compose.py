"""Runtime assertions for the pinned local Compose topology."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.integration

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPOSITORY_ROOT / "deploy" / "local" / "compose.yaml"
ENV_FILE = REPOSITORY_ROOT / "deploy" / "local" / ".env"
SERVICES = ("postgres", "redis", "etcd", "minio", "milvus", "searxng")
PUBLISHED_PORTS = {
    "postgres": frozenset({"5432/tcp"}),
    "redis": frozenset({"6379/tcp"}),
    "milvus": frozenset({"19530/tcp", "9091/tcp"}),
    "searxng": frozenset({"8080/tcp"}),
}


def _compose(*arguments: str) -> str:
    completed = subprocess.run(
        (
            "docker",
            "compose",
            "--env-file",
            str(ENV_FILE),
            "-f",
            str(COMPOSE_FILE),
            *arguments,
        ),
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _inspect(container_id: str, template: str) -> object:
    completed = subprocess.run(
        ("docker", "inspect", "--format", template, container_id),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(completed.stdout)


def _service_container(service: str) -> str:
    identities = _compose("ps", "-q", service).splitlines()
    assert len(identities) == 1, f"{service} must have one running container"
    return identities[0]


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required for integration tests")
    return value


def test_required_services_are_running_and_healthy() -> None:
    for service in SERVICES:
        state = _inspect(
            _service_container(service),
            "{{json .State}}",
        )
        assert isinstance(state, dict)
        assert state.get("Status") == "running", f"{service} is not running"
        health = state.get("Health")
        assert isinstance(health, dict)
        assert health.get("Status") == "healthy", f"{service} is not healthy"


def test_published_ports_are_loopback_only_and_internal_services_stay_private() -> None:
    for service in SERVICES:
        raw_ports = _inspect(
            _service_container(service),
            "{{json .NetworkSettings.Ports}}",
        )
        assert isinstance(raw_ports, dict)
        published: set[str] = set()
        for container_port, bindings in raw_ports.items():
            assert isinstance(container_port, str)
            if not bindings:
                continue
            assert isinstance(bindings, list)
            published.add(container_port)
            for binding in bindings:
                assert isinstance(binding, dict)
                assert binding.get("HostIp") == "127.0.0.1"

        if service in {"etcd", "minio"}:
            assert not published
        else:
            assert published == PUBLISHED_PORTS[service]


def test_searxng_root_responds_locally_without_search() -> None:
    url = _required_environment("ATLASRAG_SEARXNG_URL")
    parsed = urlsplit(url)
    assert parsed.scheme == "http"
    assert parsed.hostname == "127.0.0.1"
    assert parsed.path in {"", "/"}
    assert not parsed.query
    assert not parsed.fragment

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "AtlasRAG-Stage2-Health/1.0"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        assert response.read(1)
