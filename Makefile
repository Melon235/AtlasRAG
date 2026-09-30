ATLASRAG_ENV_FILE ?= deploy/local/.env
COMPOSE_FILE := deploy/local/compose.yaml

.PHONY: setup format format-check lint typecheck test architecture-check verify \
	infra-up infra-wait infra-status infra-down migrate migration-check \
	test-integration stage2-verify

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

infra-down:
	docker compose --env-file "$(ATLASRAG_ENV_FILE)" -f "$(COMPOSE_FILE)" down

migrate:
	uv run --env-file "$(ATLASRAG_ENV_FILE)" --frozen alembic upgrade head

migration-check:
	uv run --env-file "$(ATLASRAG_ENV_FILE)" --frozen python scripts/check_migration.py

test-integration:
	ATLASRAG_RUN_INTEGRATION=1 uv run --env-file "$(ATLASRAG_ENV_FILE)" --frozen pytest -m integration

stage2-verify:
	ATLASRAG_ENV_FILE="$(ATLASRAG_ENV_FILE)" scripts/stage2_verify.sh
