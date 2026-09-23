.PHONY: setup format format-check lint typecheck test architecture-check verify

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
