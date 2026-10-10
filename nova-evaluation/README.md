# Nova Code Evaluation with Harbor & Terminal-Bench 2.0

Runs the Nova Code CLI agent on [Terminal-Bench 2.0](https://github.com/laude-institute/terminal-bench-2) using [Harbor](https://github.com/laude-institute/harbor) as the evaluation harness, with optional [LangSmith](https://smith.langchain.com) tracing.

---

## Prerequisites

- **Python 3.12+** and **[uv](https://docs.astral.sh/uv/)**
- **Docker Desktop** running (required for `--env docker`)
- API keys for your chosen model and (optionally) LangSmith

---

## Setup

```bash
cd nova-evaluation
uv sync          # installs harbor plus the NovaCode checkout in .. (editable)
```

Model keys are read from `.env` here or in the repo root (`OPENCODE_API_KEY`,
`NVIDIA_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, ...).

Runs are isolated from your own Nova setup: the wrapper points the process home at
`nova-evaluation/.eval-home/`, so your `~/.nova` MCP servers, personal skills, prompt
overrides and memory store are not loaded, and each trial gets a fresh in-memory
store. What is measured is NovaCode as shipped. Set `NOVA_EVAL_HOME` to use a
different directory.

---

## Running the Evaluation

```bash
uv run python scripts/run.py <benchmark> -m <provider/model> [harbor run flags]
```

| Benchmark | Harbor Hub dataset | Tasks |
|---|---|---|
| `nova` | local `nova-tasks/` sanity set | 6 |
| `tb2` | `terminal-bench/terminal-bench-2` (Terminal-Bench 2.0) | 89 |
| `tb2.1` | `terminal-bench/terminal-bench-2-1` | 89 |
| `tb3` | `terminal-bench/terminal-bench@1` (Terminal-Bench 3.0) | 74 |
| `tb-latest` | `terminal-bench/terminal-bench@latest` | changes |

```bash
# Sanity check: 6 small local tasks
uv run python scripts/run.py nova -m opencode/deepseek-v4.1-flash -n 2

# One Terminal-Bench 2.0 task (task names carry the org prefix)
uv run python scripts/run.py tb2 -m opencode/deepseek-v4.1-flash -i terminal-bench/fix-git

# Full Terminal-Bench 2.0, 3 trials at a time, local Docker
uv run python scripts/run.py tb2 -m opencode/deepseek-v4.1-flash -n 3

# Terminal-Bench 3.0 on Modal (tasks need up to 32 GB RAM, 4 need a GPU, 1-5 h timeouts)
uv run python scripts/run.py tb3 -m openai/gpt-5.5 -e modal -n 8
```

The model is `provider/model`, where provider is a NovaCode provider id (`opencode`,
`nvidia`, `openai`, `anthropic`, `google`, `openrouter`, `ollama`). Useful harbor
flags: `-i` one task, `-l` first N tasks, `-n` concurrency, `-k` attempts per task,
`-e docker|modal|daytona`, `--dry-run`. Results land in `jobs/<benchmark>/<timestamp>/`.

Check the harness itself without spending tokens (runs each task's reference solution):

```bash
uv run harbor run -p nova-tasks -a oracle -e docker
```

The six `nova-tasks/` are standard Harbor task directories (`instruction.md`,
`task.toml`, `environment/`, `tests/`, `solution/`): multi-bug debugging, refactoring,
git bisect, shell debugging, a data off-by-one, and an SQL injection fix.

### DeepAgents baseline agent

```bash
make run-terminal-bench-docker     # 1 task, Docker
make run-terminal-bench-daytona    # 40 tasks, Daytona
make run-terminal-bench-modal      # 4 tasks, Modal
```

---

## Analyzing Results

```bash
# Summarize a completed job run
uv run python scripts/analyze.py jobs/Novacode/<timestamp>

# Example
uv run python scripts/analyze.py jobs/Novacode/2026-03-28__23-52-59
```

Output includes: trial status, reward scores, step counts, tool usage, and exception details.

---

## LangSmith Integration

LangSmith provides per-call tracing across all trials. The workflow:

```
Run evaluation  →  Add reward scores  →  Analyze in LangSmith UI
```

### 1. Create a dataset (one-time)

```bash
uv run python scripts/harbor_langsmith.py create-dataset terminal-bench --version 2.0
```

### 2. Create an experiment session

```bash
uv run python scripts/harbor_langsmith.py create-experiment terminal-bench \
  --name Novacode-baseline-v1
```

This prints a session ID and a direct link to the LangSmith comparison view.

### 3. Run with tracing enabled

```bash
# Set the experiment name so traces are grouped
export LANGSMITH_EXPERIMENT="Novacode-baseline-v1"

uv run python scripts/run.py tb2 -m <provider/model> -n 3
```

### 4. Push reward scores to traces

After the run completes, attach Harbor's `harbor_reward` scores (0.0–1.0) to each trace:

```bash
uv run python scripts/harbor_langsmith.py add-feedback \
  jobs/Novacode/2026-03-28__23-52-59 \
  --project-name Novacode-baseline-v1

# Dry-run first to preview what would be updated
uv run python scripts/harbor_langsmith.py add-feedback \
  jobs/Novacode/2026-03-28__23-52-59 \
  --project-name Novacode-baseline-v1 \
  --dry-run
```

---

## Project Structure

```
nova-evaluation/
├── deepagents_harbor/
│   ├── backend.py             # HarborSandbox — wraps Docker/Daytona/Modal APIs
│   ├── deepagents_wrapper.py  # DeepAgents baseline wrapper
│   ├── novacode_wrapper.py    # Nova Code CLI wrapper (primary)
│   └── tracing.py             # LangSmith helpers
├── scripts/
│   ├── run.py                 # Launcher: benchmark name -> harbor run
│   ├── analyze.py             # Summarize job results locally
│   └── harbor_langsmith.py    # Dataset / experiment / feedback CLI
├── nova-tasks/                # Local 6-task sanity set
├── jobs/                      # Output from evaluation runs
├── Makefile                   # Shortcuts for scripts/run.py
└── pyproject.toml             # Dependencies (uv)
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | For Claude models | `sk-ant-...` |
| `OPENAI_API_KEY` | For GPT models | `sk-...` |
| `LANGSMITH_API_KEY` | For tracing | `lsv2_...` |
| `LANGSMITH_TRACING_V2` | For tracing | `true` |
| `LANGSMITH_EXPERIMENT` | Optional | Groups traces by experiment name |
| `LANGSMITH_PROJECT` | Optional | Simpler project-level grouping |
| `DAYTONA_API_KEY` | For `--env daytona` | Daytona cloud API key |

---

## Available Environments

| Flag | Description | Best for |
|---|---|---|
| `--env docker` | Local Docker containers | Quick single-task tests |
| `--env daytona` | Daytona cloud sandboxes | Scaled parallel runs |
| `--env modal` | Modal cloud compute | Medium-scale runs |
| `--env runloop` | Runloop sandboxes | Alternative cloud |

---

## Makefile Targets

```
make nova | tb2 | tb2.1 | tb3     NovaCode on a benchmark (MODEL=provider/model ARGS="-n 2")
make run-terminal-bench-docker    DeepAgents baseline, 1 task (Docker)
make test | lint | format
```
