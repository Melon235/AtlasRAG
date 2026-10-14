"""Offline contract tests for the Stage 3 verification tooling."""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPOSITORY_ROOT / "Makefile"
VERIFY_SCRIPT = REPOSITORY_ROOT / "scripts" / "stage3_verify.sh"
PARSER_SMOKE_SCRIPT = REPOSITORY_ROOT / "scripts" / "stage3_parser_smoke.py"
MINERU_ADAPTER = (
    REPOSITORY_ROOT / "src" / "atlasrag" / "providers" / "parsing" / "mineru.py"
)
MINERU_INTEGRATION_TEST = (
    REPOSITORY_ROOT / "tests" / "integration" / "test_stage3_mineru_smoke.py"
)


def _make_target(makefile: str, target: str) -> str:
    match = re.search(
        rf"(?m)^{re.escape(target)}:\s*\n(?P<body>(?:\t[^\n]*\n)+)",
        makefile,
    )
    assert match is not None, f"missing Make target: {target}"
    return match.group("body")


def _load_parser_smoke() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "atlasrag_stage3_parser_smoke_tooling_test",
        PARSER_SMOKE_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_make_exposes_isolated_stage3_gates() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")

    offline = _make_target(makefile, "test-stage3")
    integration = _make_target(makefile, "test-stage3-integration")
    mineru = _make_target(makefile, "stage3-parser-smoke")
    complete = _make_target(makefile, "stage3-verify")

    assert "uv run --frozen pytest tests/stage3" in offline
    assert "--group" not in offline
    assert "docker" not in offline
    assert "--env-file" not in offline

    assert "scripts/stage3_verify.sh --integration-only" in integration
    assert "stage2-verify" not in integration

    assert "ATLASRAG_RUN_INTEGRATION=1" in mineru
    assert "uv run --frozen --group parsing-mineru" in mineru
    assert "tests/integration/test_stage3_mineru_smoke.py" in mineru
    assert "-m integration -q -s" in mineru

    assert complete.strip().endswith("scripts/stage3_verify.sh")
    assert "stage3-parser-smoke" not in complete

    ordinary = _make_target(makefile, "verify")
    assert "--group" not in ordinary
    assert "docker" not in ordinary
    assert "stage3-parser-smoke" not in ordinary


def test_stage3_verify_has_fail_safe_postgres_only_cleanup() -> None:
    script = VERIFY_SCRIPT.read_text(encoding="utf-8")

    assert "set -Eeuo pipefail" in script
    assert "trap cleanup EXIT" in script
    assert "trap 'exit 130' INT" in script
    assert "trap 'exit 143' TERM" in script
    assert "docker compose" in script
    assert "up -d postgres" in script
    assert "down --remove-orphans" in script
    assert "down -v" not in script
    assert '"${compose[@]}" down --remove-orphans || true' not in script
    assert "cleanup_exit_code" in script
    assert "--group parsing-docling" in script
    assert "scripts/wait_for_postgres.py" in script
    assert "tests/integration/test_stage3_postgres.py" in script
    assert "tests/integration/test_stage3_docling.py" in script
    assert "ATLASRAG_RUN_INTEGRATION=1" in script
    assert "make verify" in script
    assert "make test-stage3" in script
    assert "stage3-parser-smoke" not in script
    for unrelated_service in (" redis", " milvus", " searxng", " mineru"):
        assert unrelated_service not in script.casefold()

    completed = subprocess.run(
        ("bash", "-n", str(VERIFY_SCRIPT)),
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("configured_project", "expected_project"),
    [
        (None, "atlasrag-stage3-local"),
        ("atlasrag-stage3-explicit", "atlasrag-stage3-explicit"),
    ],
)
def test_stage3_integration_uses_isolated_project_and_postgres_only(
    tmp_path: Path,
    configured_project: str | None,
    expected_project: str,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tool_log = tmp_path / "tool.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'printf \'docker project=%s args=%s\\n\' "${COMPOSE_PROJECT_NAME-}" "$*" '
        '>>"${STAGE3_TOOL_LOG}"\n',
        encoding="utf-8",
    )
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'printf \'uv project=%s args=%s\\n\' "${COMPOSE_PROJECT_NAME-}" "$*" '
        '>>"${STAGE3_TOOL_LOG}"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    uv.chmod(0o755)
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "STAGE3_TOOL_LOG": str(tool_log),
            "ATLASRAG_ENV_FILE": str(tmp_path / "unused.env"),
        }
    )
    if configured_project is None:
        environment.pop("COMPOSE_PROJECT_NAME", None)
    else:
        environment["COMPOSE_PROJECT_NAME"] = configured_project

    completed = subprocess.run(
        (str(VERIFY_SCRIPT), "--integration-only"),
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""
    lines = tool_log.read_text(encoding="utf-8").splitlines()
    docker_lines = [line for line in lines if line.startswith("docker ")]
    assert len(docker_lines) == 2
    assert all(f"project={expected_project}" in line for line in lines)
    assert docker_lines[0].endswith("up -d postgres")
    assert docker_lines[1].endswith("down --remove-orphans")
    assert " -v" not in docker_lines[1]
    assert all(
        unrelated not in docker_lines[0]
        for unrelated in ("redis", "milvus", "searxng", "mineru")
    )


def test_mineru_smoke_is_real_flash_and_emits_only_bounded_summary() -> None:
    source = PARSER_SMOKE_SCRIPT.read_text(encoding="utf-8")
    adapter_source = MINERU_ADAPTER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    output_fields = {
        "sdk_version",
        "producer_version",
        "page_count",
        "element_count",
        "element_type_counts",
    }

    assert "MinerUParser" in source
    assert "simple.pdf" in source
    assert 'tier="flash"' in adapter_source
    assert "DocumentContainer" in source
    assert "importlib.metadata" in source
    assert "vendor" not in source.casefold()
    assert "payload" not in source.casefold()
    assert "traceback" not in source

    report_literal_keys = {
        key.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert report_literal_keys == output_fields


@pytest.mark.asyncio
async def test_mineru_boundary_discards_python_fd_and_child_process_noise(
    capfd: pytest.CaptureFixture[str],
) -> None:
    smoke = _load_parser_smoke()
    stdout_sentinel = "DO_NOT_LEAK_PROVIDER_STDOUT"
    stderr_sentinel = "DO_NOT_LEAK_PROVIDER_STDERR"
    child_sentinel = "DO_NOT_LEAK_PROVIDER_CHILD"
    expected = object()

    async def noisy_provider() -> object:
        print(stdout_sentinel)
        print(stderr_sentinel, file=sys.stderr)
        os.write(1, f"{stdout_sentinel}\n".encode())
        os.write(2, f"{stderr_sentinel}\n".encode())
        subprocess.run(
            (
                sys.executable,
                "-c",
                (
                    "import os; "
                    f"os.write(1, {child_sentinel.encode()!r}); "
                    f"os.write(2, {child_sentinel.encode()!r})"
                ),
            ),
            check=True,
        )
        return expected

    result = await smoke._run_with_discarded_process_output(noisy_provider)
    print("outer stdout restored")
    print("outer stderr restored", file=sys.stderr)
    captured = capfd.readouterr()

    assert result is expected
    assert stdout_sentinel not in captured.out
    assert stdout_sentinel not in captured.err
    assert stderr_sentinel not in captured.out
    assert stderr_sentinel not in captured.err
    assert child_sentinel not in captured.out
    assert child_sentinel not in captured.err
    assert captured.out == "outer stdout restored\n"
    assert captured.err == "outer stderr restored\n"


@pytest.mark.asyncio
async def test_mineru_boundary_restores_fds_and_preserves_provider_error(
    capfd: pytest.CaptureFixture[str],
) -> None:
    smoke = _load_parser_smoke()
    sentinel = "DO_NOT_LEAK_FAILED_PROVIDER"
    provider_error = RuntimeError("controlled provider failure")

    async def failing_provider() -> object:
        os.write(1, sentinel.encode())
        os.write(2, sentinel.encode())
        raise provider_error

    with pytest.raises(RuntimeError) as captured_error:
        await smoke._run_with_discarded_process_output(failing_provider)
    print("restored stdout")
    print("restored stderr", file=sys.stderr)
    captured = capfd.readouterr()

    assert captured_error.value is provider_error
    assert sentinel not in captured.out
    assert sentinel not in captured.err
    assert captured.out == "restored stdout\n"
    assert captured.err == "restored stderr\n"


@pytest.mark.asyncio
async def test_mineru_boundary_rolls_back_partial_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    smoke = _load_parser_smoke()
    setup_error = OSError("DO_NOT_LEAK_SETUP_FAILURE")
    real_dup2 = os.dup2
    dup2_calls = 0
    provider_called = False

    def fail_second_dup2(
        source: int,
        target: int,
        inheritable: bool = True,
    ) -> int:
        nonlocal dup2_calls
        dup2_calls += 1
        if dup2_calls == 2:
            raise setup_error
        return real_dup2(source, target, inheritable=inheritable)

    async def provider() -> object:
        nonlocal provider_called
        provider_called = True
        return object()

    monkeypatch.setattr(smoke.os, "dup2", fail_second_dup2)

    with pytest.raises(OSError) as captured_error:
        await smoke._run_with_discarded_process_output(provider)
    print("setup rollback stdout restored")
    print("setup rollback stderr restored", file=sys.stderr)
    captured = capfd.readouterr()

    assert captured_error.value is setup_error
    assert provider_called is False
    assert dup2_calls >= 3
    assert "DO_NOT_LEAK_SETUP_FAILURE" not in captured.out
    assert "DO_NOT_LEAK_SETUP_FAILURE" not in captured.err
    assert captured.out == "setup rollback stdout restored\n"
    assert captured.err == "setup rollback stderr restored\n"


@pytest.mark.asyncio
async def test_mineru_boundary_cleanup_faults_restore_and_preserve_primary(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    smoke = _load_parser_smoke()
    provider_error = RuntimeError("controlled primary provider failure")
    flush_error = OSError("DO_NOT_LEAK_FLUSH_FAILURE")
    restore_error = OSError("DO_NOT_LEAK_RESTORE_FAILURE")
    close_error = OSError("DO_NOT_LEAK_CLOSE_FAILURE")
    real_flush: Callable[[], None] = smoke._flush_standard_streams
    real_dup2 = os.dup2
    real_close = os.close
    flush_calls = 0
    dup2_calls = 0
    descriptor_close_calls = 0

    def flush_then_fail_on_cleanup() -> None:
        nonlocal flush_calls
        flush_calls += 1
        real_flush()
        if flush_calls == 2:
            raise flush_error

    def restore_then_fail_stdout(
        source: int,
        target: int,
        inheritable: bool = True,
    ) -> int:
        nonlocal dup2_calls
        dup2_calls += 1
        result = real_dup2(source, target, inheritable=inheritable)
        if dup2_calls == 3:
            raise restore_error
        return result

    def close_then_fail_first_descriptor(descriptor: int) -> None:
        nonlocal descriptor_close_calls
        descriptor_close_calls += 1
        real_close(descriptor)
        if descriptor_close_calls == 1:
            raise close_error

    async def failing_provider() -> object:
        os.write(1, b"DO_NOT_LEAK_CLEANUP_PROVIDER")
        os.write(2, b"DO_NOT_LEAK_CLEANUP_PROVIDER")
        raise provider_error

    monkeypatch.setattr(smoke, "_flush_standard_streams", flush_then_fail_on_cleanup)
    monkeypatch.setattr(smoke.os, "dup2", restore_then_fail_stdout)
    monkeypatch.setattr(smoke.os, "close", close_then_fail_first_descriptor)

    with pytest.raises(RuntimeError) as captured_error:
        await smoke._run_with_discarded_process_output(failing_provider)
    print("cleanup stdout restored")
    print("cleanup stderr restored", file=sys.stderr)
    captured = capfd.readouterr()

    assert captured_error.value is provider_error
    assert flush_calls == 2
    assert dup2_calls >= 4
    assert descriptor_close_calls >= 3
    assert any("stream flush: OSError" in note for note in provider_error.__notes__)
    assert any("stdout restore: OSError" in note for note in provider_error.__notes__)
    assert any(
        "saved stdout close: OSError" in note for note in provider_error.__notes__
    )
    for sentinel in (
        "DO_NOT_LEAK_CLEANUP_PROVIDER",
        "DO_NOT_LEAK_FLUSH_FAILURE",
        "DO_NOT_LEAK_RESTORE_FAILURE",
        "DO_NOT_LEAK_CLOSE_FAILURE",
    ):
        assert sentinel not in captured.out
        assert sentinel not in captured.err
    assert captured.out == "cleanup stdout restored\n"
    assert captured.err == "cleanup stderr restored\n"


def test_mineru_smoke_failure_is_fixed_and_credential_free(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    smoke = _load_parser_smoke()
    sentinel = "DO_NOT_LEAK_PROVIDER_FAILURE_DETAIL"

    async def failing_smoke() -> dict[str, object]:
        raise RuntimeError(sentinel)

    monkeypatch.setattr(smoke, "_run_smoke", failing_smoke)

    assert smoke.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "MinerU Stage 3 parser smoke failed\n"
    assert sentinel not in captured.err


@pytest.mark.parametrize(
    ("failure_factory", "expected_code"),
    [
        (lambda: SystemExit("DO_NOT_LEAK_SYSTEM_EXIT"), 1),
        (lambda: KeyboardInterrupt("DO_NOT_LEAK_KEYBOARD_INTERRUPT"), 130),
    ],
)
def test_mineru_smoke_base_exception_is_fixed_and_credential_free(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_factory: Callable[[], BaseException],
    expected_code: int,
) -> None:
    smoke = _load_parser_smoke()

    async def failing_smoke() -> dict[str, object]:
        raise failure_factory()

    monkeypatch.setattr(smoke, "_run_smoke", failing_smoke)

    assert smoke.main() == expected_code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "MinerU Stage 3 parser smoke failed\n"
    assert "DO_NOT_LEAK" not in captured.err


def test_real_mineru_integration_gate_cannot_be_replaced_by_a_fake_fixture() -> None:
    source = MINERU_INTEGRATION_TEST.read_text(encoding="utf-8")

    assert "pytest.mark.integration" in source
    assert "stage3_parser_smoke.py" in source
    assert "subprocess.run" in source
    assert "json.loads" in source
    assert "element_type_counts" in source
    assert '"tier"' not in source
    for forbidden in (
        "monkeypatch",
        "Fake",
        "fake_",
        "parser_outputs",
        "simple.json",
        "normalize_mineru_output",
    ):
        assert forbidden not in source
