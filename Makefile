.PHONY: help install test lint type format ci clean docker

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install locked dependencies (dev + test + lint)
	uv sync --frozen --all-extras

test: ## Run tests with coverage
	uv run --frozen pytest --cov=main --cov-report=term-missing --cov-fail-under=95 tests/

lint: ## Run ruff linter
	uv run --frozen ruff check .

type: ## Run mypy type checker
	uv run --frozen mypy main.py

format: ## Auto-format code
	uv run --frozen ruff format .
	uv run --frozen ruff check --fix .

format-check: ## Check formatting without changes
	uv run --frozen ruff format --check .

lock: ## Upgrade dependencies and regenerate hashed container requirements
	uv lock --upgrade
	uv export --frozen --no-dev --no-emit-project --output-file requirements.txt

ci: lint format-check type test ## Run all CI checks locally

clean: ## Remove build artifacts
	rm -rf .pytest_cache __pycache__ .coverage htmlcov dist build *.egg-info .mypy_cache .ruff_cache

docker: ## Build Docker image
	docker build -t olivetin-mcp .
