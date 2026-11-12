ATLASRAG_ENV_FILE ?= deploy/local/.env
COMPOSE_FILE := deploy/local/compose.yaml

.PHONY: setup format format-check lint typecheck test architecture-check verify \
	infra-up infra-wait infra-status infra-logs infra-down migrate migration-check \
	test-integration stage2-verify test-stage3 test-stage3-integration \
	stage3-parser-smoke stage3-verify test-stage4 test-stage4-integration \
	stage4-model-smoke stage4-verify stage5-verify test-stage5 test-stage5-integration 	stage6-verify test-stage6 test-stage6-integration stage6-model-smoke

setup:
	uv sync

format:
	uv run --frozen ruff format .

format-check:
	uv run --frozen ruff format --check .

lint:
	uv run --frozen ruff check .

typecheck:
	uv run --frozen mypy src/atlasrag scripts tests

test:
	uv run --frozen pytest

architecture-check:
	uv run --frozen python scripts/check_import_boundaries.py
	uv run --frozen python scripts/check_forbidden_tracked_files.py

verify:
	$(MAKE) format-check
	$(MAKE) lint
	$(MAKE) typecheck
	$(MAKE) architecture-check
	$(MAKE) test

infra-up:
	docker compose --env-file "$(ATLASRAG_ENV_FILE)" -f "$(COMPOSE_FILE)" up -d

infra-wait:
	uv run --env-file "$(ATLASRAG_ENV_FILE)" --frozen python scripts/wait_for_infrastructure.py

infra-status:
	docker compose --env-file "$(ATLASRAG_ENV_FILE)" -f "$(COMPOSE_FILE)" ps

infra-logs:
	docker compose --env-file "$(ATLASRAG_ENV_FILE)" -f "$(COMPOSE_FILE)" logs --no-color

infra-down:
	docker compose --env-file "$(ATLASRAG_ENV_FILE)" -f "$(COMPOSE_FILE)" down

migrate:
	uv run --env-file "$(ATLASRAG_ENV_FILE)" --frozen alembic upgrade head

migration-check:
	uv run --env-file "$(ATLASRAG_ENV_FILE)" --frozen python scripts/check_migration.py

test-integration:
	ATLASRAG_RUN_INTEGRATION=1 uv run --env-file "$(ATLASRAG_ENV_FILE)" --frozen pytest \
		tests/integration/test_compose.py \
		tests/integration/test_milvus.py \
		tests/integration/test_postgres.py \
		tests/integration/test_redis.py \
		-m integration

stage2-verify:
	ATLASRAG_ENV_FILE="$(ATLASRAG_ENV_FILE)" scripts/stage2_verify.sh

test-stage3:
	uv run --frozen pytest tests/stage3 -q

test-stage3-integration:
	ATLASRAG_ENV_FILE="$(ATLASRAG_ENV_FILE)" scripts/stage3_verify.sh --integration-only

stage3-parser-smoke:
	ATLASRAG_RUN_INTEGRATION=1 uv run --frozen --group parsing-mineru pytest tests/integration/test_stage3_mineru_smoke.py -m integration -q -s

stage3-verify:
	ATLASRAG_ENV_FILE="$(ATLASRAG_ENV_FILE)" scripts/stage3_verify.sh

test-stage4:
	uv run --frozen pytest tests/stage4 -q

test-stage4-integration:
	ATLASRAG_ENV_FILE="$(ATLASRAG_ENV_FILE)" scripts/stage4_verify.sh --integration-only

stage4-model-smoke:
	ATLASRAG_RUN_INTEGRATION=1 uv run --frozen --group indexing-embedding pytest tests/integration/test_stage4_model_smoke.py -m integration -q -s

stage4-verify:
	ATLASRAG_ENV_FILE="$(ATLASRAG_ENV_FILE)" scripts/stage4_verify.sh

test-stage5:
	uv run --frozen pytest tests/stage5 -q

test-stage6:
	uv run --frozen pytest tests/stage6 -q

test-stage6-integration:
	ATLASRAG_ENV_FILE="$(ATLASRAG_ENV_FILE)" scripts/stage6_verify.sh --integration-only

stage6-verify:
	ATLASRAG_ENV_FILE="$(ATLASRAG_ENV_FILE)" scripts/stage6_verify.sh

stage6-model-smoke:
	ATLASRAG_RUN_INTEGRATION=1 uv run --frozen --group indexing-embedding pytest tests/integration/test_stage6_model_smoke.py -m integration -q -s

test-stage7:
	uv run --frozen pytest tests/stage7 -q

test-stage7-integration:
	ATLASRAG_RUN_INTEGRATION=1 uv run --frozen pytest tests/integration/test_stage7_transport.py -m integration -q

stage7-verify:
	scripts/stage7_verify.sh

stage7-provider-smoke:
	ATLASRAG_RUN_INTEGRATION=1 uv run --frozen pytest tests/integration/test_stage7_provider_smoke.py -m integration -q -s

test-stage5-integration:
	ATLASRAG_ENV_FILE="$(ATLASRAG_ENV_FILE)" scripts/stage5_verify.sh --integration-only

stage5-verify:
	ATLASRAG_ENV_FILE="$(ATLASRAG_ENV_FILE)" scripts/stage5_verify.sh
