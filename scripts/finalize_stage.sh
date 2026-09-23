#!/usr/bin/env bash
# Verify, commit, and publish one Stage branch without merging or force-pushing.

set -Eeuo pipefail

if [[ $# -ne 3 || -z "${1:-}" || -z "${2:-}" || -z "${3:-}" ]]; then
    echo 'usage: ./scripts/finalize_stage.sh <stage> <slug> <commit-message>' >&2
    exit 2
fi

stage="$1"
slug="$2"
commit_message="$3"
expected_branch="stage/$stage-$slug"
report_path="docs/stages/stage-$stage-$slug.md"

if ! repository_root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    echo 'not inside a Git repository' >&2
    exit 2
fi
cd "$repository_root"

current_branch="$(git branch --show-current)"
if [[ "$current_branch" != "$expected_branch" ]]; then
    echo "branch mismatch: expected branch '$expected_branch', found '$current_branch'" >&2
    exit 1
fi

if ! git remote get-url origin >/dev/null 2>&1; then
    echo 'GITHUB_REMOTE_MISSING: required Git remote origin is not configured' >&2
    exit 1
fi

./scripts/check_forbidden_tracked_files.py
make verify

if [[ ! -f "$report_path" ]]; then
    echo "missing stage report: $report_path" >&2
    exit 1
fi

git add -A
git diff --cached --check
./scripts/check_forbidden_tracked_files.py --staged

if git diff --cached --quiet; then
    echo 'no staged changes to commit' >&2
    exit 1
fi

git commit -m "$commit_message"

push_stderr="$(mktemp)"
cleanup() {
    if [[ -n "$push_stderr" ]]; then
        rm -f -- "$push_stderr"
    fi
}
trap cleanup EXIT

print_sanitized_push_stderr() {
    python3 - "$push_stderr" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
print(
    re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://\S+", "<redacted-url>", text),
    end="",
    file=sys.stderr,
)
PY
}

if GIT_TERMINAL_PROMPT=0 git push -u origin HEAD 2>"$push_stderr"; then
    print_sanitized_push_stderr
    exit 0
fi

if grep -Eiq \
    'authentication failed|authorization failed|could not read (username|password)|permission denied|access denied|write access.*not granted|http[^0-9]*(401|403)|returned error: 40(1|3)' \
    "$push_stderr"; then
    echo 'GITHUB_AUTH_FAILED: GitHub authentication or push permission failed' >&2
else
    echo 'GITHUB_PUSH_FAILED: git push failed' >&2
fi
print_sanitized_push_stderr
exit 1
