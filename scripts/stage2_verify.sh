#!/usr/bin/env bash
set -Eeuo pipefail

script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_directory}/.." && pwd)
cd "${repository_root}"

ATLASRAG_ENV_FILE=${ATLASRAG_ENV_FILE:-deploy/local/.env}
export ATLASRAG_ENV_FILE

cleanup() {
    exit_code=$?
    trap - EXIT
    make infra-down ATLASRAG_ENV_FILE="${ATLASRAG_ENV_FILE}" || true
    exit "${exit_code}"
}
trap cleanup EXIT

make verify
make infra-up ATLASRAG_ENV_FILE="${ATLASRAG_ENV_FILE}"
make infra-wait ATLASRAG_ENV_FILE="${ATLASRAG_ENV_FILE}"
make migrate ATLASRAG_ENV_FILE="${ATLASRAG_ENV_FILE}"
make migration-check ATLASRAG_ENV_FILE="${ATLASRAG_ENV_FILE}"
make test-integration ATLASRAG_ENV_FILE="${ATLASRAG_ENV_FILE}"
