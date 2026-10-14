"""Offline regression checks for the GitHub Actions workflow."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
INTEGRATION_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "integration.yml"
MINIO_CI_DOCKERFILE = REPOSITORY_ROOT / "deploy" / "local" / "minio-ci.Dockerfile"
STAGE2_VERIFY_SCRIPT = REPOSITORY_ROOT / "scripts" / "stage2_verify.sh"
SETUP_UV_REFERENCE = (
    "uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0"
)
CHECKOUT_REFERENCE = (
    "uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0"
)
SETUP_PYTHON_REFERENCE = (
    "uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0"
)
SETUP_GO_REFERENCE = (
    "uses: actions/setup-go@44694675825211faa026b3c33043df3e48a5fa00 # v6.0.0"
)


def _job(workflow: str, name: str, next_name: str | None = None) -> str:
    start = workflow.index(f"  {name}:")
    end = len(workflow) if next_name is None else workflow.index(f"  {next_name}:")
    return workflow[start:end]


def test_setup_uv_uses_resolvable_immutable_release() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    normalized_lines = [line.strip() for line in workflow.splitlines()]

    assert normalized_lines.count(SETUP_UV_REFERENCE) == 1
    assert "astral-sh/setup-uv@v10" not in workflow


def test_regular_ci_pins_every_action_to_an_immutable_release() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert workflow.count(CHECKOUT_REFERENCE) == 1
    assert workflow.count(SETUP_PYTHON_REFERENCE) == 1
    assert workflow.count(SETUP_UV_REFERENCE) == 1
    for floating_reference in (
        "actions/checkout@v",
        "actions/setup-python@v",
        "astral-sh/setup-uv@v",
    ):
        assert floating_reference not in workflow


def test_regular_ci_remains_secret_free_cpu_only_and_service_free() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "make verify" in workflow
    assert "make stage2-verify" not in workflow
    assert "docker compose" not in workflow
    assert "services:" not in workflow
    assert "${{ secrets." not in workflow
    assert "gpu" not in workflow.casefold()
    assert "parsing-docling" not in workflow
    assert "parsing-mineru" not in workflow
    assert "stage3-parser-smoke" not in workflow
    assert "MinerU" not in workflow


def test_integration_workflow_is_isolated_and_always_destroys_volumes() -> None:
    workflow = INTEGRATION_WORKFLOW.read_text(encoding="utf-8")
    minio_dockerfile = MINIO_CI_DOCKERFILE.read_text(encoding="utf-8")
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
    assert SETUP_GO_REFERENCE in workflow
    assert 'go-version: "1.23.4"' in workflow
    assert "https://github.com/minio/minio.git" in workflow
    assert "MINIO_COMMIT: 16f8cf1c52f0a77eeb8f7565aaf7f7df12454583" in workflow
    assert "MINIO_RELEASE_TIMESTAMP: 2024-12-18T13-15-44Z" in workflow
    assert "MINIO_RELEASE=RELEASE go run buildscripts/gen-ldflags.go" in workflow
    assert "CGO_ENABLED=0 go build" in workflow
    assert "--file deploy/local/minio-ci.Dockerfile" in workflow
    assert "minio/minio:RELEASE.2024-12-18T13-15-44Z" in workflow
    assert (
        "minio version RELEASE.2024-12-18T13-15-44Z "
        "(commit-id=16f8cf1c52f0a77eeb8f7565aaf7f7df12454583)" in workflow
    )
    assert "m.daocloud.io" not in workflow
    assert (
        "FROM curlimages/curl:8.10.1@sha256:"
        "d9b4541e214bcd85196d6e92e2753ac6d0ea699f0af5741f8c6cccbfcf00ef4b"
        in minio_dockerfile
    )
    assert ":latest" not in minio_dockerfile.casefold()
    assert "COPY --chmod=0755 minio /usr/bin/minio" in minio_dockerfile
    assert 'ENTRYPOINT ["/usr/bin/minio"]' in minio_dockerfile

    failure_index = workflow.index("if: failure()")
    logs_index = workflow.index("logs --no-color")
    always_index = workflow.index("if: always()")
    cleanup_index = workflow.index("down -v --remove-orphans")
    assert failure_index < logs_index < always_index < cleanup_index
    assert verify_script.index("make infra-logs") < verify_script.index(
        "make infra-down"
    )


def test_integration_workflow_keeps_stage2_and_adds_isolated_stage3_job() -> None:
    workflow = INTEGRATION_WORKFLOW.read_text(encoding="utf-8")
    stage2 = _job(workflow, "stage2-integration", "stage3-integration")
    stage3 = _job(workflow, "stage3-integration")

    assert "make stage2-verify" in stage2
    assert "Set up Go 1.23.4" in stage2
    assert "Build pinned MinIO release from official source" in stage2
    assert "--group parsing-docling" not in stage2

    assert CHECKOUT_REFERENCE in stage3
    assert SETUP_PYTHON_REFERENCE in stage3
    assert SETUP_UV_REFERENCE in stage3
    assert "uv sync --frozen --group parsing-docling" in stage3
    assert "make test-stage3-integration" in stage3
    assert "COMPOSE_PROJECT_NAME: atlasrag-stage3-ci-" in stage3
    assert "cp deploy/local/.env.example deploy/local/.env" in stage3
    assert "if: always()" in stage3
    assert "down -v --remove-orphans" in stage3
    assert "logs --no-color postgres" in stage3
    assert "parsing-mineru" not in stage3
    assert "MinerU" not in stage3
    assert "make stage2-verify" not in stage3
    assert "setup-go" not in stage3

    for floating_reference in (
        "actions/checkout@v",
        "actions/setup-python@v",
        "actions/setup-go@v",
        "astral-sh/setup-uv@v",
    ):
        assert floating_reference not in workflow
