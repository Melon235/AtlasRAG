#!/usr/bin/env bash
set -Eeuo pipefail

script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_directory}/.." && pwd)
cd "${repository_root}"

ATLASRAG_ENV_FILE=${ATLASRAG_ENV_FILE:-deploy/local/.env}
COMPOSE_FILE=${COMPOSE_FILE:-deploy/local/compose.yaml}
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-atlasrag-stage3-local}
export ATLASRAG_ENV_FILE COMPOSE_PROJECT_NAME

mode=${1:-full}
if [[ "${mode}" != "full" && "${mode}" != "--integration-only" ]]; then
    printf '%s\n' "usage: scripts/stage3_verify.sh [--integration-only]" >&2
    exit 2
fi
if (($# > 1)); then
    printf '%s\n' "usage: scripts/stage3_verify.sh [--integration-only]" >&2
    exit 2
fi

compose=(
    docker compose
    --env-file "${ATLASRAG_ENV_FILE}"
    -f "${COMPOSE_FILE}"
)
postgres_started=0

cleanup() {
    exit_code=$?
    trap - EXIT INT TERM
    if ((postgres_started)); then
        if ((exit_code != 0)); then
            "${compose[@]}" logs --no-color postgres || true
        fi
        cleanup_exit_code=0
        "${compose[@]}" down --remove-orphans || cleanup_exit_code=$?
        if ((exit_code == 0 && cleanup_exit_code != 0)); then
            exit_code=${cleanup_exit_code}
        fi
    fi
    exit "${exit_code}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

run_integration() {
    postgres_started=1
    "${compose[@]}" up -d postgres
    uv run --env-file "${ATLASRAG_ENV_FILE}" --frozen \
        --group parsing-docling python scripts/wait_for_postgres.py
    ATLASRAG_RUN_INTEGRATION=1 uv run \
        --env-file "${ATLASRAG_ENV_FILE}" \
        --frozen \
        --group parsing-docling \
        pytest \
        tests/integration/test_stage3_postgres.py \
        tests/integration/test_stage3_docling.py \
        -m integration \
        -q
}

if [[ "${mode}" == "full" ]]; then
    make verify
    make test-stage3
fi
run_integration
