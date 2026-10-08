.DEFAULT_GOAL := help
IMAGE ?= ecommerce-scraper

.PHONY: help install hooks hooks-update format lint typecheck test test-live check run docker-build docker-run

help: ## Show available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-14s %s\n", $$1, $$2}'

install: ## Install the app and dev tools into .venv
	uv sync --locked

hooks: ## Install the git pre-commit hooks
	uv run pre-commit install

hooks-update: ## Bump pinned pre-commit hook versions (Dependabot doesn't cover them)
	uv run pre-commit autoupdate --freeze

format: ## Auto-format and auto-fix lint issues
	uv run ruff format .
	uv run ruff check --fix .

lint: ## Check formatting and lint rules
	uv run ruff format --check .
	uv run ruff check .

typecheck: ## Run mypy in strict mode
	uv run mypy

test: ## Run the offline test suite with coverage
	uv run pytest --cov

test-live: ## Live checks against the real site, including a headless browser
	uv sync --locked --group browser
	uv run --group browser playwright install chromium
	uv run --group browser pytest -m live

check: ## What CI runs: every pre-commit hook, then the tests
	uv run pre-commit run --all-files --show-diff-on-failure
	uv run pytest --cov

run: ## Scrape the site into report.json and validate it
	uv run ecommerce-scraper --output report.json
	uv run ecommerce-scraper-validate report.json

docker-build: ## Build the production image
	docker build -t $(IMAGE) .

docker-run: ## Run the production image
	docker run --rm $(IMAGE)
