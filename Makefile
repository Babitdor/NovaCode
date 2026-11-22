.PHONY: all lint format test help run test_integration test_watch clean rust rust-test rust-frames

# Default target executed when no arguments are given to make.
all: help

######################
# TESTING AND COVERAGE
######################

# Define variables for test paths
TEST_FILE ?= tests/unit_tests
INTEGRATION_FILES ?= tests/integration_tests

test:
	uv run pytest $(TEST_FILE) \
	  --ignore=tests/unit_tests/test_shell.py \
	  --ignore=tests/unit_tests/test_process_manager.py \
	  --ignore=tests/unit_tests/test_end_to_end.py

test_integration:
	uv run pytest $(INTEGRATION_FILES)

test_all:
	uv run pytest tests/

test_watch:
	uv run ptw . -- $(TEST_FILE)

test_cov:
	uv run pytest --cov=novacode_cli --cov-report=term-missing $(TEST_FILE)

######################
# RUNNING
######################

run:
	uv run nova

# Sync dependencies and run (use this when pyproject.toml changes)
sync:
	uv sync

# Lock dependencies (update uv.lock without installing)
lock:
	uv lock

# Show dependency tree
tree:
	uv tree

# Show outdated packages
outdated:
	uv tree --outdated

# Add a dependency
add:
	uv add $(PACKAGE)

# Add a dev dependency
add-dev:
	uv add --dev $(PACKAGE)

# Remove a dependency
remove:
	uv remove $(PACKAGE)

# Full reinstall of both packages into .venv — use when sync isn't enough
reinstall:
	uv sync --reinstall-package novacode-cli --reinstall-package deepagents

run_reinstall:
	uv sync --reinstall-package novacode-cli --reinstall-package deepagents && uv run nova

######################
# RUST CLIENT (rust/nova-tui)
######################

# Build the ratatui client. Development builds are enough to run with
# NOVA_TUI_BIN=rust/target/debug/nova-tui (or nova-tui.exe on Windows).
rust:
	cd rust && cargo build -p nova-tui

# Format, lint and test exactly what CI runs. -D warnings makes clippy a gate,
# not a suggestion.
rust-test:
	cd rust && cargo fmt --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace

# Regenerate every parity corpus (deterministic: stub agent, no API spend) and
# the rendered-frame snapshot the replay test compares against. One scenario per
# behaviour family; the replay tests read whichever corpora exist, so adding a
# scenario is a data change, not a code change.
rust-frames:
	uv run python scripts/record_tui_session.py --scenario core_chat
	uv run python scripts/record_tui_session.py --scenario screens
	uv run python scripts/record_tui_session.py --scenario commands
	cd rust && BLESS=1 cargo test -p nova-tui --test frames_replay

######################
# LINTING AND FORMATTING
######################

# Define Python files to lint/format
PYTHON_FILES = novacode_cli/ tests/

lint:
	@echo "Running ruff format check..."
	uv run ruff format $(PYTHON_FILES) --check
	@echo "Running ruff linter..."
	uv run ruff check $(PYTHON_FILES)

format:
	@echo "Formatting code..."
	uv run ruff format $(PYTHON_FILES)
	@echo "Fixing lint issues..."
	uv run ruff check --fix $(PYTHON_FILES)

######################
# CLEANUP
######################

clean:
	@echo "Cleaning up..."
	-rm -rf .pytest_cache
	-rm -rf __pycache__
	-rm -rf novacode_cli/__pycache__
	-rm -rf tests/__pycache__
	-rm -rf .ruff_cache
	-rm -rf *.egg-info
	-rm -rf dist
	-rm -rf build
	@echo "Done."

######################
# HELP
######################

help:
	@echo '===================='
	@echo 'Nami-Code Makefile'
	@echo '===================='
	@echo ''
	@echo '-- RUNNING --'
	@echo 'run                          - run Nova CLI (uv run nova)'
	@echo 'sync                         - sync .venv dependencies (use after pyproject.toml changes)'
	@echo 'lock                         - update uv.lock without installing'
	@echo 'tree                         - show dependency tree'
	@echo 'outdated                     - show outdated packages'
	@echo 'add PACKAGE=xxx              - add a dependency'
	@echo 'add-dev PACKAGE=xxx          - add a dev dependency'
	@echo 'remove PACKAGE=xxx           - remove a dependency'
	@echo 'reinstall                    - force reinstall novacode-cli + deepagents into .venv'
	@echo 'run_reinstall                - reinstall then run Nova CLI'
	@echo ''
	@echo '-- TESTING --'
	@echo 'test                         - run unit tests (excludes subprocess-heavy tests)'
	@echo 'test TEST_FILE=<path>        - run specific test file or directory'
	@echo 'test_integration             - run integration tests'
	@echo 'test_all                     - run all tests (including subprocess-heavy)'
	@echo 'test_watch                   - run tests in watch mode'
	@echo 'test_cov                     - run tests with coverage report'
	@echo ''
	@echo '-- LINTING --'
	@echo 'format                       - run code formatters'
	@echo 'lint                         - run linters'
	@echo ''
	@echo '-- CLEANUP --'
	@echo 'clean                        - remove build artifacts and caches'
	@echo ''
