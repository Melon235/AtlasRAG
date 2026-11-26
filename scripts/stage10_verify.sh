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
    printf '%s\n' "usage: scripts/stage10_verify.sh [full|--integration-only]" >&2
    exit 2
fi

ATLASRAG_ENV_FILE=${ATLASRAG_ENV_FILE:-deploy/local/.env}
COMPOSE_FILE=${COMPOSE_FILE:-deploy/local/compose.yaml}
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-atlasrag-stage10b-local}
export ATLASRAG_ENV_FILE COMPOSE_PROJECT_NAME

if [[ ! -f "${ATLASRAG_ENV_FILE}" ]]; then
    printf '%s\n' "Stage 10-B integration requires env file: ${ATLASRAG_ENV_FILE}" >&2
    exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
    printf '%s\n' "Stage 10-B integration requires docker" >&2
    exit 2
fi

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
            "${compose[@]}" logs --no-color postgres redis etcd minio milvus || true
        fi
        cleanup_exit_code=0
        "${compose[@]}" down -v --remove-orphans || cleanup_exit_code=$?
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
    make test-stage10
fi

services_started=1
"${compose[@]}" up -d --wait --wait-timeout 300 postgres redis etcd minio milvus
uv run --env-file "${ATLASRAG_ENV_FILE}" --frozen \
    python scripts/wait_for_postgres.py
uv run --env-file "${ATLASRAG_ENV_FILE}" --frozen python -c '
import asyncio, os
from redis.asyncio import Redis
from pymilvus import MilvusClient

async def wait_services() -> None:
    client = Redis.from_url(os.environ["ATLASRAG_REDIS_URL"], decode_responses=False, socket_timeout=3.0)
    try:
        assert await client.ping() is True
    finally:
        await client.aclose()
    mc = MilvusClient(uri=os.environ["ATLASRAG_MILVUS_URI"], timeout=10)
    try:
        mc.list_collections()
    finally:
        mc.close()

asyncio.run(wait_services())
'
ATLASRAG_RUN_INTEGRATION=1 uv run \
    --env-file "${ATLASRAG_ENV_FILE}" \
    --frozen \
    pytest \
    tests/integration/test_stage10_runtime_lifecycle.py \
    tests/integration/test_stage10_cache_and_recovery.py \
    tests/integration/test_stage10_transport_settlement.py \
    -m integration \
    -v
