#!/usr/bin/env bash
set -Eeuo pipefail

script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_directory}/.." && pwd)
cd "${repository_root}"

unset LANGSMITH_API_KEY LANGCHAIN_API_KEY LANGSMITH_ENDPOINT LANGCHAIN_ENDPOINT
export LANGSMITH_TRACING=false
export LANGSMITH_TRACING_V2=false
export LANGCHAIN_TRACING=false
export LANGCHAIN_TRACING_V2=false

mode=${1:-full}
if (($# > 1)) || [[ "${mode}" != "full" && "${mode}" != "--integration-only" ]]; then
    printf '%s\n' "usage: scripts/stage11_verify.sh [full|--integration-only]" >&2
    exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
    printf '%s\n' "Stage 11 integration requires docker" >&2
    exit 2
fi

stage11_python=${STAGE11_PYTHON:-${repository_root}/.venv/bin/python}
if [[ ! -x "${stage11_python}" ]]; then
    stage11_python=python3
fi

COMPOSE_FILE=${COMPOSE_FILE:-deploy/local/compose.yaml}
runtime_dir=$(mktemp -d "${TMPDIR:-/tmp}/atlasrag-stage11.XXXXXX")
meta_file=""
cleanup_armed=0
lock_pid=""

cleanup() {
    exit_code=$?
    trap - EXIT INT TERM
    if ((cleanup_armed)) && [[ -n "${meta_file}" && -f "${meta_file}" ]]; then
        cleanup_exit_code=0
        if ((exit_code != 0)); then
            "${stage11_python}" scripts/stage10_isolation.py cleanup --meta "${meta_file}" --show-logs || cleanup_exit_code=$?
        else
            "${stage11_python}" scripts/stage10_isolation.py cleanup --meta "${meta_file}" || cleanup_exit_code=$?
        fi
        if ((exit_code == 0 && cleanup_exit_code != 0)); then
            exit_code=${cleanup_exit_code}
        fi
    fi
    if [[ -n "${lock_pid}" ]]; then
        exec {LOCK_WRITE_FD}>&- || true
        wait "${lock_pid}" 2>/dev/null || true
    fi
    rm -rf "${runtime_dir}"
    exit "${exit_code}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# 1) Prepare isolated temporary configuration and reject conflicting shell overrides.
meta_file=$("${stage11_python}" scripts/stage10_isolation.py prepare \
    --runtime-dir "${runtime_dir}" \
    --repository-root "${repository_root}" \
    --project-name "${COMPOSE_PROJECT_NAME:-}" \
    --run-id "${STAGE11_RUN_ID:-}")

# Extract non-secret coordination fields cleanly via Python.
eval "$("${stage11_python}" -c '
import json, shlex, sys
m = json.load(open(sys.argv[1]))
for k in ("project_name", "run_id", "owner_token", "env_file", "override_file"):
    print(f"meta_{k}={shlex.quote(m[k])}")
' "${meta_file}")"

# 2) Acquire exclusive project + run locks before touching Docker.
lock_in_fifo="${runtime_dir}/lock.in"
lock_out_fifo="${runtime_dir}/lock.out"
mkfifo -m 600 "${lock_in_fifo}" "${lock_out_fifo}"
"${stage11_python}" scripts/stage10_isolation.py hold-locks --meta "${meta_file}" <"${lock_in_fifo}" >"${lock_out_fifo}" &
lock_pid=$!
exec {LOCK_WRITE_FD}>"${lock_in_fifo}"
exec {LOCK_READ_FD}<"${lock_out_fifo}"
lock_status=""
if ! IFS= read -r -u "${LOCK_READ_FD}" lock_status || [[ "${lock_status}" != "LOCKED" ]]; then
    exec {LOCK_READ_FD}<&- || true
    printf '%s\n' "Stage 11 failed to acquire exclusive project/run lock" >&2
    exit 2
fi
exec {LOCK_READ_FD}<&- || true

# 3) Pre-flight empty project check BEFORE arming cleanup (refuses pre-existing state without down -v).
COMPOSE_PROJECT_NAME="${meta_project_name}" \
    "${stage11_python}" scripts/stage10_isolation.py preflight \
    --meta "${meta_file}" \
    --compose-file "${COMPOSE_FILE}"

if [[ "${mode}" == "full" ]]; then
    make verify
    make test-stage11
fi

export COMPOSE_PROJECT_NAME="${meta_project_name}"
export ATLASRAG_ENV_FILE="${meta_env_file}"
compose=(
    docker compose
    -p "${meta_project_name}"
    --env-file "${meta_env_file}"
    -f "${COMPOSE_FILE}"
    -f "${meta_override_file}"
)

# Arm strictly run-owned cleanup ONLY after preflight succeeds.
cleanup_armed=1
"${compose[@]}" up -d --wait --wait-timeout 300 postgres redis etcd minio milvus

# 4) Verify Compose container identities, run_id labels, loopback bindings, and published ports.
containers_json=$("${stage11_python}" scripts/stage10_isolation.py verify-containers \
    --meta "${meta_file}" \
    --compose-file "${COMPOSE_FILE}")
eval "$("${stage11_python}" -c '
import json, shlex, sys
c = json.loads(sys.argv[1])
mapping = {
    "postgres": "cid_pg",
    "redis": "cid_redis",
    "milvus": "cid_milvus",
    "etcd": "cid_etcd",
    "minio": "cid_minio",
}
for svc, var in mapping.items():
    print(f"{var}={shlex.quote(c[svc])}")
' "${containers_json}")"

# 5) Wait for PostgreSQL then establish positive per-service ownership markers.
uv run --env-file "${meta_env_file}" --frozen \
    python scripts/wait_for_postgres.py
uv run --env-file "${meta_env_file}" --frozen \
    python scripts/stage10_isolation.py establish --meta "${meta_file}"

# 6) Execute real Stage 11 local application server E2E integration suite.
ATLASRAG_RUN_INTEGRATION=1 \
ATLASRAG_STAGE10B_ISOLATED=1 \
ATLASRAG_STAGE10B_RUN_ID="${meta_run_id}" \
ATLASRAG_STAGE10B_OWNER_TOKEN="${meta_owner_token}" \
ATLASRAG_STAGE10B_COMPOSE_PROJECT="${meta_project_name}" \
ATLASRAG_STAGE10B_PG_CONTAINER="${cid_pg}" \
ATLASRAG_STAGE10B_REDIS_CONTAINER="${cid_redis}" \
ATLASRAG_STAGE10B_MILVUS_CONTAINER="${cid_milvus}" \
ATLASRAG_STAGE10B_ETCD_CONTAINER="${cid_etcd}" \
ATLASRAG_STAGE10B_MINIO_CONTAINER="${cid_minio}" \
ATLASRAG_STAGE10B_ENV_FILE="${meta_env_file}" \
uv run \
    --env-file "${meta_env_file}" \
    --frozen \
    pytest \
    tests/integration/test_stage11_server_e2e.py \
    -m integration \
    -v
