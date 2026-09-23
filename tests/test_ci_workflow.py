"""Offline regression checks for the GitHub Actions workflow."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
SETUP_UV_REFERENCE = (
    "uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0"
)


def test_setup_uv_uses_resolvable_immutable_release() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    normalized_lines = [line.strip() for line in workflow.splitlines()]

    assert normalized_lines.count(SETUP_UV_REFERENCE) == 1
    assert "astral-sh/setup-uv@v10" not in workflow
