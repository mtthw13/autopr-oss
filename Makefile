.DEFAULT_GOAL := check

# ── Installation ─────────────────────────────────────────────────────────────

.PHONY: install
install:  ## Install the package with dev extras
	python -m pip install -e '.[dev]'

.PHONY: install-hooks
install-hooks: install  ## Install pre-commit hooks
	pre-commit install

# ── Quality checks ───────────────────────────────────────────────────────────

.PHONY: check-fix
check-fix:  ## Auto-fix lint and formatting issues
	ruff check --fix autopr tests
	ruff format autopr tests

.PHONY: check
check:  ## Lint and format check (no auto-fix)
	ruff check autopr tests
	ruff format --check autopr tests
	mypy autopr

# ── Tests ────────────────────────────────────────────────────────────────────

.PHONY: test
test:  ## Run the full test suite
	python -m pytest tests/ -q

.PHONY: test-changed
test-changed:  ## Run tests related to uncommitted changes
	python -m pytest tests/ -q --picked

.PHONY: coverage
coverage:  ## Run tests with coverage report
	python -m pytest tests/ --cov=autopr --cov-report=term-missing
