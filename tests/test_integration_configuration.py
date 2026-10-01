"""Service-free checks for explicit Stage 2 integration-test configuration."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_pytest_registers_and_excludes_integration_tests_by_default() -> None:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as source:
        configuration = tomllib.load(source)

    pytest_options = configuration["tool"]["pytest"]["ini_options"]
    assert pytest_options["addopts"][-2:] == ["-m", "not integration"]
    assert pytest_options["markers"] == [
        "integration: requires explicitly opted-in local infrastructure",
    ]


def test_example_environment_uses_dedicated_redis_test_database() -> None:
    lines = (
        (REPOSITORY_ROOT / "deploy" / "local" / ".env.example")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assignments = {
        name: value
        for line in lines
        if line and not line.startswith("#")
        for name, value in [line.split("=", maxsplit=1)]
    }

    assert assignments["ATLASRAG_REDIS_URL"].endswith("/0")
    assert assignments["ATLASRAG_TEST_REDIS_URL"].endswith("/15")
