"""Offline integration tests for the reusable stage finalizer."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FINALIZER = REPOSITORY_ROOT / "scripts" / "finalize_stage.sh"
FORBIDDEN_CHECKER = REPOSITORY_ROOT / "scripts" / "check_forbidden_tracked_files.py"
EXPECTED_BRANCH = "stage/00-repository-foundation"
REPORT_PATH = "docs/stages/stage-00-repository-foundation.md"
COMMIT_MESSAGE = "stage(00): establish repository foundation"


def run_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run Git in a temporary repository and require success."""
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


def git_output(repository: Path, *arguments: str) -> str:
    """Return stripped stdout from a successful Git command."""
    return run_git(repository, *arguments).stdout.strip()


def initialize_stage_repository(
    root: Path,
    *,
    branch: str = EXPECTED_BRANCH,
    add_origin: bool = True,
    include_report: bool = True,
    verify_succeeds: bool = True,
) -> tuple[Path, Path]:
    """Create a small repository and optional local bare origin."""
    repository = root / "work"
    remote = root / "origin.git"
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch", branch, str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    run_git(repository, "config", "user.name", "AtlasRAG Tests")
    run_git(repository, "config", "user.email", "atlasrag-tests@example.invalid")

    scripts = repository / "scripts"
    scripts.mkdir()
    copied_checker = scripts / FORBIDDEN_CHECKER.name
    shutil.copy2(FORBIDDEN_CHECKER, copied_checker)
    copied_checker.chmod(0o755)
    (repository / "README.md").write_text("fixture repository\n", encoding="utf-8")
    verification_recipe = (
        "\t@printf 'verified\\n'\n"
        if verify_succeeds
        else "\t@printf 'verification failed\\n' >&2\n\t@exit 7\n"
    )
    (repository / "Makefile").write_text(
        ".PHONY: verify\nverify:\n" + verification_recipe,
        encoding="utf-8",
    )
    if include_report:
        report = repository / REPORT_PATH
        report.parent.mkdir(parents=True)
        report.write_text("# Stage 00 report\n", encoding="utf-8")

    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "initial")

    if add_origin:
        subprocess.run(
            ["git", "init", "--bare", "--quiet", str(remote)],
            check=True,
            capture_output=True,
            text=True,
        )
        run_git(repository, "remote", "add", "origin", str(remote))
    return repository, remote


def run_finalizer(repository: Path) -> subprocess.CompletedProcess[str]:
    """Execute the real finalizer with the Stage 0 interface."""
    return subprocess.run(
        [
            "bash",
            str(FINALIZER),
            "00",
            "repository-foundation",
            COMMIT_MESSAGE,
        ],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )


def combined_output(result: subprocess.CompletedProcess[str]) -> str:
    """Combine captured streams for concise diagnostic assertions."""
    return result.stdout + result.stderr


def test_wrong_branch_aborts_before_commit_or_push(tmp_path: Path) -> None:
    repository, remote = initialize_stage_repository(
        tmp_path, branch="stage/00-wrong-slug"
    )
    before = git_output(repository, "rev-parse", "HEAD")

    result = run_finalizer(repository)

    assert result.returncode != 0
    assert "expected branch 'stage/00-repository-foundation'" in combined_output(result)
    assert git_output(repository, "rev-parse", "HEAD") == before
    assert git_output(remote, "for-each-ref", "--format=%(refname)") == ""


def test_missing_origin_emits_required_token(tmp_path: Path) -> None:
    repository, _ = initialize_stage_repository(tmp_path, add_origin=False)
    before = git_output(repository, "rev-parse", "HEAD")

    result = run_finalizer(repository)

    assert result.returncode != 0
    assert "GITHUB_REMOTE_MISSING" in combined_output(result)
    assert git_output(repository, "rev-parse", "HEAD") == before


def test_missing_report_aborts_without_commit(tmp_path: Path) -> None:
    repository, remote = initialize_stage_repository(tmp_path, include_report=False)
    before = git_output(repository, "rev-parse", "HEAD")

    result = run_finalizer(repository)

    assert result.returncode != 0
    assert f"missing stage report: {REPORT_PATH}" in combined_output(result)
    assert git_output(repository, "rev-parse", "HEAD") == before
    assert git_output(remote, "for-each-ref", "--format=%(refname)") == ""


def test_verification_failure_aborts_before_commit(tmp_path: Path) -> None:
    repository, remote = initialize_stage_repository(tmp_path, verify_succeeds=False)
    (repository / "change.txt").write_text("pending\n", encoding="utf-8")
    before = git_output(repository, "rev-parse", "HEAD")

    result = run_finalizer(repository)

    assert result.returncode != 0
    assert "verification failed" in combined_output(result)
    assert git_output(repository, "rev-parse", "HEAD") == before
    assert git_output(remote, "for-each-ref", "--format=%(refname)") == ""


def test_force_tracked_forbidden_file_aborts(tmp_path: Path) -> None:
    repository, remote = initialize_stage_repository(tmp_path)
    (repository / ".env").write_text("SECRET=not-a-real-secret\n", encoding="utf-8")
    run_git(repository, "add", "--force", ".env")
    run_git(repository, "commit", "--quiet", "-m", "add forbidden fixture")
    before = git_output(repository, "rev-parse", "HEAD")

    result = run_finalizer(repository)

    assert result.returncode != 0
    assert ".env: forbidden tracked artifact" in combined_output(result)
    assert git_output(repository, "rev-parse", "HEAD") == before
    assert git_output(remote, "for-each-ref", "--format=%(refname)") == ""


def test_no_staged_changes_aborts_without_empty_commit(tmp_path: Path) -> None:
    repository, remote = initialize_stage_repository(tmp_path)
    before = git_output(repository, "rev-parse", "HEAD")

    result = run_finalizer(repository)

    assert result.returncode != 0
    assert "no staged changes to commit" in combined_output(result)
    assert git_output(repository, "rev-parse", "HEAD") == before
    assert git_output(remote, "for-each-ref", "--format=%(refname)") == ""


def test_success_creates_requested_commit_and_pushes_without_force(
    tmp_path: Path,
) -> None:
    repository, remote = initialize_stage_repository(tmp_path)
    (repository / "change.txt").write_text("ready\n", encoding="utf-8")

    result = run_finalizer(repository)

    assert result.returncode == 0, combined_output(result)
    assert git_output(repository, "log", "-1", "--format=%s") == COMMIT_MESSAGE
    assert git_output(repository, "rev-parse", "@{upstream}") == git_output(
        repository, "rev-parse", "HEAD"
    )
    assert git_output(remote, "rev-parse", EXPECTED_BRANCH) == git_output(
        repository, "rev-parse", "HEAD"
    )


def test_remote_rejection_uses_generic_push_failure_not_auth_token(
    tmp_path: Path,
) -> None:
    repository, remote = initialize_stage_repository(tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\necho 'policy rejection' >&2\nexit 1\n", encoding="utf-8"
    )
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    (repository / "change.txt").write_text("ready\n", encoding="utf-8")

    result = run_finalizer(repository)

    assert result.returncode != 0
    assert "GITHUB_PUSH_FAILED" in combined_output(result)
    assert "GITHUB_AUTH_FAILED" not in combined_output(result)
    assert "policy rejection" in combined_output(result)
