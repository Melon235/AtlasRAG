#!/usr/bin/env bash
set -Eeuo pipefail
script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_directory}/.." && pwd)
cd "${repository_root}"

unset LANGSMITH_API_KEY LANGCHAIN_API_KEY LANGSMITH_ENDPOINT LANGCHAIN_ENDPOINT
export LANGSMITH_TRACING=false LANGSMITH_TRACING_V2=false
export LANGCHAIN_TRACING=false LANGCHAIN_TRACING_V2=false

mode=${1:-full}
if (($# > 1)) || [[ "${mode}" != "full" && "${mode}" != "--integration-only" ]]; then
    printf '%s\n' "usage: scripts/stage7_verify.sh [full|--integration-only]" >&2
    exit 2
fi

if [[ "${mode}" == "full" ]]; then
    make verify
    make test-stage7
fi

ATLASRAG_RUN_INTEGRATION=1 uv run --frozen pytest \
    tests/integration/test_stage7_transport.py \
    -m integration -q
