# Changelog - v1.3

## New Features

### Council of Agents (`/council`)
- **Renamed from `/chat`**: The multi-agent discussion command is now `/council` — no backward-compatible alias
- **Democratic voting**: Each agent produces an independent answer; the final response is selected by majority vote
- **Cross-round history**: Council agents now carry conversation history across rounds for coherent multi-turn debates
- **Multi-`@agent` routing**: Route prompts to specific agents using `@agent` mentions inside the TUI

### HITL Policy Engine
- **Configurable approval policies**: Define policies (allow, deny, conditional) per tool or tool category
- **Approval notifications**: HITL interrupts surface as notification badges in the TUI status bar with pending approval count
- **Native approval modal**: Keyboard-friendly approve/dismiss flow with arrow-key navigation

### LangGraph Platform Deployment
- **`langgraph.json`**: Full LangGraph Platform deployment config with `source.kind=uv` for consistent dependency resolution
- **Docker support**: Docker Compose stack for self-hosted LangGraph server deployment
- **Async subagent server**: Remote LangGraph server endpoints for background documentation updates, code reviews, and test generation

### LLM Wiki
- **Persistent wiki system**: Scrapable, queryable wiki built from ingested documentation sources
- **`/ingest` command**: Pull external documentation into the wiki knowledge base
- **`/ask` command**: Query the wiki for context-backed answers

### Eval Harness
- **Automated evaluation**: Structured evaluation framework for benchmarking agent performance
- **LangSmith integration**: Trace and evaluate runs through LangSmith's evaluation pipeline

### Plugin System
- **Python entry-point plugins**: Plugins register slash commands, middleware at defined slots, and custom tools via `pyproject.toml` entry points
- **`/plugins` manager**: Native TUI screen to list, enable, and disable installed plugins
- **Repo reorganization**: Project structure reorganized to support the plugin discovery mechanism

### Ralph Integration
- **Ralph emit**: Inline Ralph expressions for quick calculations and data transformations within the chat

### `create` Command
- **`nova create <name>`**: Scaffold new projects with `uv init` and optional templates
### Per-Role Model Selection
- **A model per agent role**: Nova is no longer limited to a single model — the main agent, the in-process subagents, the remote async agents, and the agents discovered in the agent directories can each run on a different provider and model
- **Nothing saved changes nothing**: A role you have not set keeps its current behaviour — subagents and discovered agents inherit the main agent's model, and the remote async agents keep their own server default — so an existing `Nova.config.json` behaves exactly as it did before
- **One key per role in `Nova.config.json`**: Each role is stored under its own key — `model_role_subagent`, `model_role_async`, and `model_role_dynamic` — with the main agent keeping the existing `model` key, so setting one role never overwrites another role or your main model
- **Discovered agents can name their own model**: A `model:` value in an `agent.md` frontmatter wins over the `dynamic` role, which in turn follows the `subagent` role when it is unset; an `/eval` fan-out dispatches those same agents
- **Async agents are configured through the environment**: A stored async role is passed to a server Nova launches as `ASYNC_AGENT_PROVIDER` and `ASYNC_AGENT_MODEL` (plus `OPENAI_BASE_URL` when you saved an endpoint override), while a variable you set yourself still wins
- **Applies on the next use**: A new role setting takes effect on the next dispatch — or on the next server launch for the async role — instead of waiting for the subagent cache to expire

## Improvements

### Hermes Learning System
- **Memory rework**: Full rewrite of the memory consolidation pipeline — cleaner two-tier separation (prompt-injected markdown vs. key-value store)
- **Self-evolution**: Autonomous skill refinement cycle: review → extract lesson → create/update skill → re-evaluate
- **Skill schema validation**: Skills are validated against an extended schema (frontmatter + steps structure)
- **Skill debate**: Compares new skill against existing ones to prevent duplication before creation
- **Overhauled tracker**: Improved `skill_usage` tracking with outcome-based refinement triggers

### TUI Enhancements
- **Native `/dream`**: Dream consolidation runs as a native TUI action with live status updates
- **`/clear` reset**: Full transcript clear resets the turn state and input buffer properly
- **Ralph emit**: Ralph results displayed inline in the TUI transcript
- **Multi-`@agent` routing**: Route prompts to specific subagents from the input line
- **`@`-mention autocomplete fix**: Corrected autocomplete popup behavior for file/agent mentions
- **Keyboard-friendly text selection**: `ctrl+c` copies selected text (if any), else quits
- **Bash alias mode**: `!command` prefix detected and styled with magenta accent in the input bar
- **Paste tracking**: Large paste detection with placeholder preview before submission
- **Matrix rain pause**: Animation pauses when terminal loses OS focus or is scrolled out of view
- **Roles in `/model`**: The picker now opens with a **Roles** section — one row per role, each showing what that role would actually run, so you can see which model a delegation will use before you change anything
- **Pick the role, then the model**: The row marked `target` is where the next model pick goes, and choosing a role row retargets the picker instead of closing it, so pointing the subagents, the async agents or the dynamic agents at a different model takes one pass through the same screen
- **The main agent switches now, every other role later**: A pick for the main agent still hot-swaps the live agent on the spot; a pick for any other role is saved and takes effect when that role is next used — on the next dispatch for the subagents and the dynamic agents, on the next server launch for the async agents — and the confirmation line says which
- **The subagents panel stops assuming the session model**: The MODEL column reports each task's real model instead of repeating the session model, and an async row names the async role or the server's own default
- **Roles documented**: The README gained a table of what each role covers and when a change takes effect, and `.env.example` records that the `ASYNC_AGENT_*` pair is the async role and that the container route needs the service recreated to pick up a change

### Steering & Prompts
- **Mid-run steering**: Steering instructions now reach the agent while it's actively running (not just on next turn)
- **Prompt cleanup**: Removed outdated prompt fragments, tightened core system prompt
- **`/init` crash fixes**: Fixed crashes during project graph rebuild with certain project structures

### Sandbox & Safety
- **Pattern A sandbox**: New sandbox execution pattern for isolated agent runs
- **LangSmith sandbox**: New sandbox provider integration with LangSmith
- **Sandbox lifecycle hardening**: Proper cleanup of orphaned/stale sandbox containers on session end
- **Plugins security**: Plugin sandboxing with restricted tool access

### Session Management
- **Project memory always loaded**: `NOVA.md` / `CLAUDE.md` loaded into `<project_memory>` context even on session resume
- **Session picker honors `/clear`**: The resume session picker properly shows cleared sessions as fresh
### Async Subagent Models
- **Model-agnostic async agents**: The remote LangGraph graphs (`async-agents/*.py`) no longer hardwire `ChatOllama` — all six build their model through one factory, so background code reviews, documentation updates, and test runs can use any configured provider instead of Ollama only
- **Environment-driven selection**: `ASYNC_AGENT_PROVIDER` chooses the provider (defaults to `ollama`) and `ASYNC_AGENT_MODEL` the model id; `DOC_AGENT_MODEL` is still honored, and `PLAN_SCOUT_MODEL` still overrides for that graph alone
- **Defaults unchanged**: With neither variable set the async agents keep running on `gemma4:31b-cloud`, so an existing deployment is unaffected
- **Failures name the fix**: An unknown provider, a provider with no model set, or a missing API key now fails at startup with a message naming the variable to set — instead of quietly running background work on the wrong model

## Bug Fixes

- **Async subagent initialization**: Fixed race conditions in async subagent startup
- **Shell/bash tool**: Fixed shell execution hanging on multi-line commands; improved error propagation
- **Vision handling**: Fixed initial bug where vision/image inputs weren't being passed to the model correctly
- **Vision captioning rework**: Image captioning now uses the vision model directly instead of a separate captioning call
- **`edit_file` UX**: Fixed edit failure not showing a clear error message to the user
- **Response streaming truncation**: Fixed edge case where streaming responses were cut off at the end
- **Hermes memory leak**: Fixed cross-session memory accumulation that inflated context windows
- **Steering prompt truncation**: Fixed steering instructions being silently dropped when combined with long system prompts
- **`/init` crashes**: Fixed crashes on projects without `pyproject.toml` or with circular dependencies
- **Subagents panel doubled a label's kind prefix**: Rows read `researcher: research: docling-0` because the panel prefixed the subagent type onto labels that already opened with a kind of their own; the type is now added only to a label with no prefix of its own

## Technical Changes

- **Repository reorganization**: Moved modules into clearer directory structure to support the plugin system
- **`pyproject.toml`**: Added `[project.entry-points."nova.plugins"]` for plugin discovery
- **Dependency updates**: Bumped `deepagents` framework dependency; added `langgraph` SDK for platform deployment
- **Test suite expansion**: Added new test coverage for Hermes review cycles, HITL policy engine, and sandbox lifecycle
- **Per-role model plumbing**: Added `config/role_models.py` as the single place that answers what each role runs on, with one config key per role so two Nova processes cannot clobber each other's settings

---

## v1.2 - Previous Release

See [`CHANGELOG-v1.2.md`](CHANGELOG-v1.2.md) for the documentation update agent and async subagent configuration changes.