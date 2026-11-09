"""Exercise the Stage 4 shell gates with recording executables, without services."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/stage4_verify.sh"
INTEGRATION_FILES = [
    f"tests/integration/test_stage4_{name}.py"
    for name in ("index", "publish", "locks", "build")
]
TRACING_FLAGS = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_TRACING_V2",
)
SECRET_KEYS = ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")


@pytest.fixture
def cli(tmp_path: Path) -> tuple[dict[str, str], Path]:
    directory = tmp_path / "fake tools"
    directory.mkdir()
    log = tmp_path / "commands.jsonl"
    program = (
        f"#!{sys.executable}\n"
        + """
import json
import os
import sys
from pathlib import Path
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
phase = tool
if tool == "docker":
    phase = next(value for value in ("up", "down", "logs") if value in args)
elif tool == "uv":
    phase = "pytest" if "pytest" in args else "readiness"
elif tool == "make":
    phase = args[0]
with open(os.environ["STAGE4_COMMAND_LOG"], "a", encoding="utf-8") as output:
    output.write(json.dumps({"tool": tool, "args": args, "phase": phase,
        "project": os.environ.get("COMPOSE_PROJECT_NAME"),
        "opt_in": os.environ.get("ATLASRAG_RUN_INTEGRATION"),
        "tracing": {name: os.environ.get(name) for name in (
            "LANGSMITH_TRACING", "LANGSMITH_TRACING_V2",
            "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2")},
        "keys": {name: os.environ.get(name) for name in (
            "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")}}) + "\\n")
if os.environ.get("STAGE4_SIGNAL_PHASE") == phase:
    os.kill(os.getppid(), int(os.environ["STAGE4_SIGNAL"]))
sys.exit(int(os.environ.get("STAGE4_EXIT_" + phase, "0")))
"""
    )
    for name in ("uv", "docker", "make"):
        path = directory / name
        path.write_text(program, encoding="utf-8")
        path.chmod(0o755)
    env = dict(os.environ)
    env.pop("COMPOSE_PROJECT_NAME", None)
    env.pop("ATLASRAG_RUN_INTEGRATION", None)
    for name in TRACING_FLAGS:
        env[name] = "true"
    for name in SECRET_KEYS:
        env[name] = "inherited-test-key"
    env.update(
        PATH=f"{directory}:{env['PATH']}",
        ATLASRAG_ENV_FILE=str(tmp_path / "configuration with spaces.env"),
        COMPOSE_FILE=str(tmp_path / "compose with spaces.yaml"),
        STAGE4_COMMAND_LOG=str(log),
    )
    return env, log


def run_script(
    cli: tuple[dict[str, str], Path], *args: str
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
    assert SCRIPT.is_file(), "Stage 4 verification script is missing"
    env, log = cli
    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    records = (
        [json.loads(line) for line in log.read_text().splitlines()]
        if log.exists()
        else []
    )
    return result, records


@pytest.mark.parametrize("mode", [(), ("full",), ("--integration-only",)])
@pytest.mark.parametrize("project", [None, "atlasrag-stage4-explicit"])
def test_script_runs_exact_bounded_dependencies_and_selectors(
    cli: tuple[dict[str, str], Path], mode: tuple[str, ...], project: str | None
) -> None:
    env, _ = cli
    if project:
        env["COMPOSE_PROJECT_NAME"] = project
    result, calls = run_script(cli, *mode)
    assert result.returncode == 0, result.stderr
    phases = [call["phase"] for call in calls]
    prefix = [] if mode == ("--integration-only",) else ["verify", "test-stage4"]
    assert phases == [*prefix, "up", "readiness", "pytest", "down"]
    assert all(
        call["project"] == (project or "atlasrag-stage4-local") for call in calls
    )
    assert all(
        call["tracing"] == {name: "false" for name in TRACING_FLAGS} for call in calls
    )
    assert all(call["keys"] == {name: None for name in SECRET_KEYS} for call in calls)
    compose = [
        "compose",
        "--env-file",
        env["ATLASRAG_ENV_FILE"],
        "-f",
        env["COMPOSE_FILE"],
    ]
    startup = calls[len(prefix)]
    assert startup["args"] == [
        *compose,
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        "300",
        "postgres",
        "etcd",
        "minio",
        "milvus",
    ]
    assert calls[-3]["args"] == [
        "run",
        "--env-file",
        env["ATLASRAG_ENV_FILE"],
        "--frozen",
        "python",
        "scripts/wait_for_postgres.py",
    ]
    assert calls[-2]["opt_in"] == "1"
    assert calls[-2]["args"] == [
        "run",
        "--env-file",
        env["ATLASRAG_ENV_FILE"],
        "--frozen",
        "pytest",
        *INTEGRATION_FILES,
        "-m",
        "integration",
        "-q",
    ]
    assert calls[-1]["args"] == [*compose, "down", "--remove-orphans"]


@pytest.mark.parametrize(
    "args", [("--bad",), ("",), ("full", "extra"), ("--integration-only", "extra")]
)
def test_invalid_arguments_do_not_start_or_cleanup_services(
    cli: tuple[dict[str, str], Path], args: tuple[str, ...]
) -> None:
    result, calls = run_script(cli, *args)
    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert calls == []


@pytest.mark.parametrize(
    "phase", ["verify", "test-stage4", "up", "readiness", "pytest"]
)
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_failure_preserves_status_logs_before_cleanup_and_stops_later_steps(
    cli: tuple[dict[str, str], Path], phase: str, cleanup_fails: bool
) -> None:
    env, _ = cli
    env[f"STAGE4_EXIT_{phase}"] = "23"
    if cleanup_fails:
        env["STAGE4_EXIT_down"] = "31"
        env["STAGE4_EXIT_logs"] = "32"
    result, calls = run_script(cli)
    assert result.returncode == 23
    expected = ["verify", "test-stage4", "up", "readiness", "pytest"]
    expected = expected[: expected.index(phase) + 1]
    if phase not in {"verify", "test-stage4"}:
        expected += ["logs", "down"]
        assert calls[-2]["args"][-6:] == [
            "logs",
            "--no-color",
            "postgres",
            "etcd",
            "minio",
            "milvus",
        ]
    assert [call["phase"] for call in calls] == expected


def test_successful_run_fails_when_cleanup_fails(
    cli: tuple[dict[str, str], Path],
) -> None:
    cli[0]["STAGE4_EXIT_down"] = "31"
    result, calls = run_script(cli, "--integration-only")
    assert result.returncode == 31
    assert calls[-1]["phase"] == "down"


@pytest.mark.parametrize("signal_number,expected", [(2, 130), (15, 143)])
@pytest.mark.parametrize("phase", ["up", "readiness", "pytest"])
def test_signals_cleanup_started_services_and_preserve_signal_exit(
    cli: tuple[dict[str, str], Path], signal_number: int, expected: int, phase: str
) -> None:
    cli[0].update(STAGE4_SIGNAL_PHASE=phase, STAGE4_SIGNAL=str(signal_number))
    result, calls = run_script(cli, "--integration-only")
    assert result.returncode == expected
    assert [call["phase"] for call in calls][-2:] == ["logs", "down"]


@pytest.mark.parametrize(
    "target,expected",
    [
        ("test-stage4", ["run", "--frozen", "pytest", "tests/stage4", "-q"]),
        (
            "stage4-model-smoke",
            [
                "run",
                "--frozen",
                "--group",
                "indexing-embedding",
                "pytest",
                "tests/integration/test_stage4_model_smoke.py",
                "-m",
                "integration",
                "-q",
                "-s",
            ],
        ),
    ],
)
def test_make_offline_and_model_gates_are_separate(
    cli: tuple[dict[str, str], Path], target: str, expected: list[str]
) -> None:
    env, log = cli
    result = subprocess.run(
        ["/usr/bin/make", target],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 1
    assert calls[0]["args"] == expected
    assert calls[0]["opt_in"] == ("1" if target == "stage4-model-smoke" else None)


@pytest.mark.parametrize(
    "target,full", [("test-stage4-integration", False), ("stage4-verify", True)]
)
def test_make_integration_and_full_gates_use_the_verification_script(
    cli: tuple[dict[str, str], Path], target: str, full: bool
) -> None:
    env, log = cli
    result = subprocess.run(
        ["/usr/bin/make", target],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call["phase"] for call in calls] == [
        *(["verify", "test-stage4"] if full else []),
        "up",
        "readiness",
        "pytest",
        "down",
    ]
