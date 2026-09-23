.PHONY: setup format format-check lint typecheck test architecture-check verify

setup:
	uv sync

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

lint:
	uv run ruff check .

typecheck:
	uv run mypy src/atlasrag scripts tests

test:
	uv run pytest

architecture-check:
	uv run python scripts/check_import_boundaries.py
	uv run python scripts/check_forbidden_tracked_files.py

verify:
	$(MAKE) format-check
	$(MAKE) lint
	$(MAKE) typecheck
	$(MAKE) architecture-check
	$(MAKE) test
