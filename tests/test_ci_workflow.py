"""Offline regression checks for the GitHub Actions workflow."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
INTEGRATION_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "integration.yml"
STAGE2_VERIFY_SCRIPT = REPOSITORY_ROOT / "scripts" / "stage2_verify.sh"
SETUP_UV_REFERENCE = (
    "uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0"
)


def test_setup_uv_uses_resolvable_immutable_release() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    normalized_lines = [line.strip() for line in workflow.splitlines()]

    assert normalized_lines.count(SETUP_UV_REFERENCE) == 1
    assert "astral-sh/setup-uv@v10" not in workflow


def test_regular_ci_remains_secret_free_cpu_only_and_service_free() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "make verify" in workflow
    assert "make stage2-verify" not in workflow
    assert "docker compose" not in workflow
    assert "services:" not in workflow
    assert "${{ secrets." not in workflow
    assert "gpu" not in workflow.casefold()


def test_integration_workflow_is_isolated_and_always_destroys_volumes() -> None:
    workflow = INTEGRATION_WORKFLOW.read_text(encoding="utf-8")
    verify_script = STAGE2_VERIFY_SCRIPT.read_text(encoding="utf-8")

    assert "uv sync --frozen" in workflow
    assert "make stage2-verify" in workflow
    assert "ATLASRAG_ENV_FILE: deploy/local/.env" in workflow
    assert (
        "COMPOSE_PROJECT_NAME: atlasrag-ci-${{ github.run_id }}-"
        "${{ github.run_attempt }}"
    ) in workflow
    assert "cp deploy/local/.env.example deploy/local/.env" in workflow
    assert "GITHUB_RUN_ID" in workflow
    assert "${{ secrets." not in workflow
    assert (
        "m.daocloud.io/quay.io/minio/minio@sha256:"
        "1dce27c494a16bae114774f1cec295493f3613142713130c2d22dd5696be6ad3"
    ) in workflow
    assert "docker tag" in workflow

    failure_index = workflow.index("if: failure()")
    logs_index = workflow.index("logs --no-color")
    always_index = workflow.index("if: always()")
    cleanup_index = workflow.index("down -v --remove-orphans")
    assert failure_index < logs_index < always_index < cleanup_index
    assert verify_script.index("make infra-logs") < verify_script.index(
        "make infra-down"
    )
