# Convenience targets for POSIX shells. Windows users: scripts/bootstrap.ps1
# does the same thing in one command.

VENV := .venv
PY   := $(VENV)/bin/python

.PHONY: setup test lint typecheck check run run-http clean

setup: ## Create the venv, install everything, arm the credential hook
	python3 -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -e ".[dev]"
	git config core.hooksPath .githooks
	@test -f .env || cp .env.example .env
	@mkdir -p .secrets
	@echo "Setup complete. Next: docs/SETUP_GOOGLE_CLOUD.md"

test: ## Run the full suite
	$(PY) -m pytest -q

test-live: ## Run the tests that talk to the real Google APIs
	$(PY) -m pytest -m integration -v

test-failures: ## Run only the failure scenarios
	$(PY) -m pytest tests/failure -v

lint:
	$(PY) -m ruff check src tests

typecheck:
	$(PY) -m mypy

check: lint test ## Everything CI would run

run: ## Serve over stdio (Claude Desktop)
	$(PY) -m mcp_cloudtube --transport stdio

run-http: ## Serve over HTTP (ChatGPT)
	$(PY) -m mcp_cloudtube --transport http

extension: ## Build the Claude Desktop extension (.mcpb)
	$(PY) scripts/build_extension.py

doctor: ## Report configuration and credential status
	$(PY) -m mcp_cloudtube --check

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache .pytest-tmp htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
