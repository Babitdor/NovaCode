.PHONY: all lint format test help run-hello-world run-terminal-bench-modal run-terminal-bench-docker nova tb2 tb2.1 tb3 tb-latest

# Default target executed when no arguments are given to make.
all: help

######################
# TESTING AND COVERAGE
######################

# Define a variable for the test file path.
TEST_FILE ?= tests/unit_tests
INTEGRATION_FILES ?= tests/integration_tests

test:
	uv run pytest --disable-socket --allow-unix-socket $(TEST_FILE)

test_integration:
	uv run pytest $(INTEGRATION_FILES)

test_watch:
	uv run ptw . -- $(TEST_FILE)


######################
# RUN DEEPAGENTS WRAPPER
######################

run-hello-world:
	@mkdir -p jobs/hello-world
	harbor run -a deepagents_harbor:DeepAgentsWrapper --dataset hello-world --task-name hello-world -n 1 --jobs-dir tmp/hello-world --env docker

run-terminal-bench-modal:
	@mkdir -p jobs/terminal-bench
	harbor run -a deepagents_harbor:DeepAgentsWrapper -d terminal-bench/terminal-bench-2 -n 4 --jobs-dir jobs/terminal-bench --env modal

run-terminal-bench-daytona:
	@mkdir -p jobs/terminal-bench
	harbor run -a deepagents_harbor:DeepAgentsWrapper -d terminal-bench/terminal-bench-2 -n 40 --jobs-dir jobs/terminal-bench --env daytona

run-terminal-bench-docker:
	@mkdir -p jobs/terminal-bench
	harbor run -a deepagents_harbor:DeepAgentsWrapper -d terminal-bench/terminal-bench-2 -n 1 --jobs-dir jobs/terminal-bench --env docker

run-terminal-bench-runloop:
	@mkdir -p jobs/terminal-bench
	harbor run -a deepagents_harbor:DeepAgentsWrapper -d terminal-bench/terminal-bench-2 -n 10 --jobs-dir jobs/terminal-bench --env runloop

######################
# RUN NOVA CODE (scripts/run.py wraps `harbor run`)
######################

# Harbor-style provider/model. Override: make tb2 MODEL=openai/gpt-5.5
MODEL ?= opencode/deepseek-v4.1-flash
# Extra `harbor run` flags, e.g. ARGS="-n 4 -e modal" or ARGS="-i terminal-bench/fix-git"
ARGS ?=

# nova = local 6-task sanity set; tb2 = Terminal-Bench 2.0; tb3 = Terminal-Bench 3.0
nova tb2 tb2.1 tb3 tb-latest:
	uv run python scripts/run.py $@ -m $(MODEL) $(ARGS)

######################
# LINTING AND FORMATTING
######################

# Define a variable for Python and notebook files.
lint format: PYTHON_FILES=deepagents_harbor/ tests/
lint_diff format_diff: PYTHON_FILES=$(shell git diff --relative=. --name-only --diff-filter=d master | grep -E '\.py$$|\.ipynb$$')

lint lint_diff:
	[ "$(PYTHON_FILES)" = "" ] ||	uv run ruff format $(PYTHON_FILES) --diff
	@if [ "$(LINT)" != "minimal" ]; then \
		if [ "$(PYTHON_FILES)" != "" ]; then \
			uv run ruff check $(PYTHON_FILES) --diff; \
		fi; \
	fi
	# [ "$(PYTHON_FILES)" = "" ] || uv run mypy $(PYTHON_FILES)

format format_diff:
	[ "$(PYTHON_FILES)" = "" ] || uv run ruff format $(PYTHON_FILES)
	[ "$(PYTHON_FILES)" = "" ] || uv run ruff check --fix $(PYTHON_FILES)

format_unsafe:
	[ "$(PYTHON_FILES)" = "" ] || uv run ruff format --unsafe-fixes $(PYTHON_FILES)
	

######################
# HELP
######################

help:
	@echo '===================='
	@echo '-- LINTING --'
	@echo 'format                       - run code formatters'
	@echo 'lint                         - run linters'
	@echo '-- TESTS --'
	@echo 'test                         - run unit tests'
	@echo 'test TEST_FILE=<test_file>   - run all tests in file'
	@echo '-- DEEPAGENTS EVALUATION --'
	@echo 'run-terminal-bench-docker    - run 1 task with DeepAgents (Docker)'
	@echo 'run-terminal-bench-daytona   - run 40 tasks with DeepAgents (Daytona)'
	@echo 'run-terminal-bench-modal     - run 4 tasks with DeepAgents (Modal)'
	@echo '-- NOVA CODE EVALUATION --'
	@echo 'nova | tb2 | tb2.1 | tb3     - run NovaCode on a benchmark (MODEL=provider/model ARGS="-n 2")'
	@echo '-- DOCUMENTATION tasks are from the top-level Makefile --'


