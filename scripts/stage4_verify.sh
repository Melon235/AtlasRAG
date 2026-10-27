#!/usr/bin/env bash
set -Eeuo pipefail

script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_directory}/.." && pwd)
cd "${repository_root}"

mode=${1-full}
if (($# > 1)) || [[ "${mode}" != "full" && "${mode}" != "--integration-only" ]]; then
    printf '%s\n' "usage: scripts/stage4_verify.sh [full|--integration-only]" >&2
    exit 2
fi

ATLASRAG_ENV_FILE=${ATLASRAG_ENV_FILE:-deploy/local/.env}
COMPOSE_FILE=${COMPOSE_FILE:-deploy/local/compose.yaml}
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-atlasrag-stage4-local}
export ATLASRAG_ENV_FILE COMPOSE_PROJECT_NAME

compose=(
    docker compose
    --env-file "${ATLASRAG_ENV_FILE}"
    -f "${COMPOSE_FILE}"
)
services_started=0

cleanup() {
    exit_code=$?
    trap - EXIT INT TERM
    if ((services_started)); then
        if ((exit_code != 0)); then
            "${compose[@]}" logs --no-color postgres etcd minio milvus || true
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

if [[ "${mode}" == "full" ]]; then
    make verify
    make test-stage4
fi

# Mark ownership before startup so partial startup/readiness failures also clean up.
services_started=1
"${compose[@]}" up -d --wait --wait-timeout 300 postgres etcd minio milvus
uv run --env-file "${ATLASRAG_ENV_FILE}" --frozen \
    python scripts/wait_for_postgres.py
ATLASRAG_RUN_INTEGRATION=1 uv run \
    --env-file "${ATLASRAG_ENV_FILE}" \
    --frozen \
    pytest \
    tests/integration/test_stage4_index.py \
    tests/integration/test_stage4_publish.py \
    tests/integration/test_stage4_locks.py \
    tests/integration/test_stage4_build.py \
    -m integration \
    -q
