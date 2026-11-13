![NovaCode](assets/Nova.png)

# NovaCode

**An open-source coding assistant for your terminal, with persistent project knowledge and your choice of models.**

Nova reads code, investigates bugs, plans changes, edits files, runs tests, and delegates work to specialist agents. Use the interactive terminal, run scripted tasks, or connect remote conversations to your sessions.

[Install](#installation) · [Commands](#everyday-use) · [Documentation](#documentation) · [Releases](https://github.com/Babitdor/NovaCode/releases) · [Issues](https://github.com/Babitdor/NovaCode/issues)

![NovaCode terminal interface](assets/Preview-classic.png)

## Capabilities

| Area | What Nova provides |
| --- | --- |
| Coding | Repository search, file and notebook editing, shell commands, debugging, tests, and change review. |
| Planning | Read-only investigation, explicit plan approval, and Council discussions for comparing approaches. |
| Delegation | Specialist agents, background tasks, project tabs, and Git worktree tools. |
| Knowledge | Project onboarding, persistent memory, LLM-assisted recall, and a linked Markdown memory wiki. |
| Long sessions | Saved conversations, resumable sessions, context inspection, and recoverable context compression. |
| Models | Hosted and local providers, role-specific models, and a separate vision model for image interpretation. |
| Extensions | Skills, directory plugins, MCP servers, and Code Mode for composing eligible tools. |
| Interfaces | Terminal themes, optional voice, local web workspaces, remote chats, and headless automation. |

Nova is under active development; package metadata currently marks it **Alpha**. Agent teams and swarm scheduling are experimental, and the local computer feature is a preview.

## Installation

Requires **Python 3.11+**, a terminal on Windows, macOS, or Linux, and access to a supported model. Install with [uv](https://docs.astral.sh/uv/getting-started/installation/); uv can provision Python for the isolated tool environment.

```sh
uv tool install --python 3.11 novacode-cli
cd your-project
nova
```

First-run setup guides provider configuration and workspace approval. Use `/auth` to manage provider access and `/model` to select a model. Hosted models use your provider account; local Ollama use requires a running Ollama instance and an installed model.

If `nova` is not found after installation, run `uv tool update-shell` and restart your shell. Check the installed version with `nova --version`.

### Updates

Run `/update` inside Nova or upgrade from the shell:

```sh
uv tool upgrade novacode-cli
```

The in-app updater uses the current terminal and resumes launcher-managed sessions. Before upgrading from a version older than 1.2.15, exit Nova in other terminal windows. To replace a Git-based installation with the PyPI package:

```sh
uv tool install --python 3.11 --force novacode-cli
```

### Optional dependencies

Install only the extras you need. Keep all wanted extras in the requirement when reinstalling:

```sh
uv tool install --python 3.11 --force "novacode-cli[voice,voice-kokoro,bedrock]"
```

| Extra | Purpose / additional requirements |
| --- | --- |
| `voice` | Microphone input and spoken replies; speech models download on first use. |
| `voice-kokoro`, `voice-sano`, `voice-pocket`, `voice-orpheus` | Alternative speech output engines; use with `voice`. |
| `voice-parakeet`, `voice-whistle` | Alternative speech recognition engines; use with `voice`. |
| `bedrock` | Amazon Bedrock integration; requires AWS credentials or a configured role/profile. |
| `agents-server` | Local LangGraph server support for asynchronous agents. |
| `server` | Optional FastAPI/Uvicorn server dependencies. |
| `browser` | Legacy browser-use integrations; not required for Nova's Obscura MCP browser mode. |
| `computer` | Preview Linux VM integration; requires QEMU, a guest image, and 4 GiB guest RAM. |

Extras install Python dependencies. External runtimes, browser binaries, system audio support, and model downloads may require separate setup. Voice-engine compatibility varies by platform; installing every engine together is not a verified configuration.

## Everyday use

Describe the outcome you want, attach project files with `@`, and use **Shift+Enter** for multiline input. Press **Left Arrow** from an empty composer to open Command Center. `/help` lists available commands and shortcuts.

| Commands | Purpose |
| --- | --- |
| `/onboard` | Build a local project map, architecture guide, and guided tour. |
| `/model`, `/auth` | Select models, configure vision, and manage provider access. |
| `/plan`, `/council <task>` | Research a plan or compare proposals before implementation. |
| `/agents`, `/subagents`, `/agent-server` | Manage specialists and inspect background-agent setup. |
| `/tabs`, `/resume`, `/cc` | Switch projects, reopen conversations, and inspect session status. |
| `/context`, `/compact`, `/dream` | Inspect context, compact history, and maintain memory. |
| `/files`, `/images`, `/tests`, `/servers` | Inspect files and attachments, run checks, and manage development servers. |
| `/mcp`, `/plugin`, `/create` | Configure external tools, plugins, skills, and agents. |
| `/loop`, `/remote` | Schedule recurring session work or configure remote chats. |
| `/settings`, `/theme`, `/voice` | Adjust behavior, appearance, and speech preferences. |
| `/restore`, `/update` | Recover supported file snapshots or update Nova. |

Resume from the shell:

```sh
nova --continue              # Latest session
nova --continue SESSION_ID   # A specific session
nova --resume                # Interactive session picker
```

For sessions from another project, the picker offers the saved workspace or the current workspace. Workspace trust and tool approvals still apply.

## Models and configuration

Provider integrations include OpenAI, Anthropic, Google, OpenRouter, CommandCode, OpenCode, NVIDIA, Azure OpenAI, Amazon Bedrock, and Ollama, alongside additional providers in the model catalog. Authentication, tool calling, reasoning, vision, and cache reporting depend on the provider and model.

Use `/model` and `/auth` for configuration rather than editing credentials into project files. Nova supports independent model roles, including a vision companion for text-only models. Cache statistics reflect provider-reported usage; unavailable counts are not evidence of zero usage or guaranteed savings.

User preferences live in `~/.nova/Nova.config.json`. Project instructions can be supplied through supported `AGENTS.md`, `CLAUDE.md`, and `NOVA.md` files. Use `/settings` for interactive preferences and `nova config --help` for configuration commands.

## Project knowledge and long sessions

**Onboarding.** `/onboard` builds a local dependency map and guide in `.nova/onboard/`. Use `/onboard ask <question>`, `/onboard impact <symbol or path>`, and `/onboard refresh` to explore and update it. Static analysis respects ignore rules but cannot identify every dynamic dependency. [Onboarding guide](docs/project-onboarding.md).

**Memory and learning.** Nova retains reusable project knowledge in a linked Markdown wiki. LLM-assisted recall interprets what a request needs before searching authorized memory; bounded retrieval avoids loading the entire corpus into every prompt. Recall notices identify consulted sources. `/dream` provides memory maintenance. Memory is editable context, not proof that a remembered claim is correct.

**Context compression.** Enabled by default, model-controlled compression summarizes completed work while retaining searchable originals in the session archive. The agent can inspect, search, compress, and recover archived ranges; saved sessions retain those blocks. `/compact` remains available for manual compaction. This reduces active model context, not stored conversation size. [Compression guide](docs/context-compression.md).

**Response modes.** The composer cycles **Off → Caveman → Ponytail**. Caveman compresses prose and optional tool views; Ponytail emphasizes complete solutions with minimal new code. Both offer intensity controls and preserve tool permissions. They are separate from history compression. [Controls and validation](docs/response-modes.md).

## Integrations and automation

- **MCP:** discovery runs after the interface opens. Cached schemas are available early; MCP operations wait for discovery when needed. Failures appear as notifications. Select a server in `/mcp` and choose **Retry** to reconnect it. A cached schema does not establish a live connection.
- **Code Mode:** the composer toggle lets the agent combine eligible native and MCP tools in bounded JavaScript workflows. Nested calls retain approval checks. [Code Mode guide](docs/mcp-code-mode.md).
- **Skills and plugins:** reuse instructions and install namespaced skills, commands, agents, hooks, and MCP definitions through `/plugin`, `nova plugin`, or supported skill commands.
- **Remote and web:** use `/remote` for Telegram, Discord, or Rootapp; `/cowork` for a local visual workspace; `/council web` for multi-model discussions; and `/create` to edit skills and agents.
- **Artifacts:** create local previews and optionally share them through temporary Cloudflare URLs. Public sharing requires `cloudflared`; anyone with the link can access the preview. [Sharing guide](docs/temporary-sharing.md).
- **Recurring work:** `/loop 5m check CI status` schedules work in an open local session. `/loop list` and `/loop stop` inspect and cancel loops. [Schedules and lifecycle](docs/session-loops.md).

### Scripted tasks

Run a single task and exit, with optional structured output and limits:

```sh
nova -p "Summarize this repository"
nova -p "Review the current diff" --output-format json --max-turns 8 --timeout 180
```

`--print` supports `text`, `json`, and `stream-json` output. Persistent integrations use `nova --mode pipe` for JSONL communication; `--headless` is an alias for pipe mode, not single-prompt execution. Use `nova --help` for workspace trust, approval, model, and timeout options.

## Permissions and data

Nova applies workspace trust, tool policies, and approval controls. **Plan execution requires explicit user approval even when auto-approve is enabled.** Decision-model review and optional guardrails add checks; they do not make generated code correct or replace isolation for untrusted execution.

Requests may send prompts, selected source content, attachments, and retrieved memory to configured providers or integrations. Local models avoid hosted inference for those calls, but enabled search, remote, and external-tool features can still use network services. Review integrations and generated changes before using them with sensitive projects. [Permission and guardrail details](docs/security-guardrails.md).

## Development

Nova uses **Python, LangGraph, deepagents, and Textual**. The terminal, headless interface, and remote bridges share the agent loop in `novacode_cli/core/agent_loop.py`.

```sh
git clone https://github.com/Babitdor/NovaCode.git
cd NovaCode
uv sync --locked --group dev
uv run nova
uv run pytest tests/test_release_version.py -q
```

For optional features, include the required flags when syncing, for example `uv sync --group dev --extra voice`. Read [AGENTS.md](AGENTS.md), follow nearby code patterns, and run tests relevant to your change. Submit reproducible bug reports and focused pull requests through GitHub.

CI checks Linux and Windows, splits offline regressions into balanced shards, and verifies Windows wheel installation and reinstallation. PyPI publication requires successful CI for the exact release commit. [CI and release workflow](scripts/CI.md).

## Contributing / Community

- **Contribute:** see [CONTRIBUTING.md](CONTRIBUTING.md) for setup, focused checks, and PR expectations.
- **Issues and proposals:** search [existing issues](https://github.com/Babitdor/NovaCode/issues) first; use issue templates for bug reports and feature requests. Discuss large changes before implementation.
- **Starter issues:** maintainers will label/triage well-scoped tasks as `good first issue` or `help wanted` once the process is in place.
- **Code of Conduct:** community participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- **Security:** report vulnerabilities privately per [SECURITY.md](SECURITY.md) (**do not open public issues**). Product guardrails are documented in [docs/security-guardrails.md](docs/security-guardrails.md).
- **Repo hygiene (maintainer-led checks):** scan tracked files/history for secrets or sensitive data before opening the repo broadly, rotate any exposed credentials, and ensure `.gitignore`/docs do not leak internal-only data. History rewrites/credential rotation are high-risk and must be handled deliberately by maintainers (not by automated edits).

For development, run `uv sync --group dev` and tests relevant to your change. See [project instructions](AGENTS.md) for build and check commands.

## Documentation

| Topic | Reference |
| --- | --- |
| Setup and usage | [Documentation site](https://babitdor.github.io/NovaCode/docs/) |
| Project exploration and sessions | [Onboarding](docs/project-onboarding.md) · [Command Center](docs/command-center.md) |
| Context and responses | [Context compression](docs/context-compression.md) · [Response modes](docs/response-modes.md) |
| Tools and browser automation | [Code Mode](docs/mcp-code-mode.md) · [Essential tools](docs/essential-tools.md) · [Browser mode](docs/browser-mode.md) |
| Advanced workflows | [Prime](docs/prime-factory.md) · [Council](docs/COUNCIL-SETTINGS.md) · [Session loops](docs/session-loops.md) |
| Remote access and previews | [Discord](docs/discord-session-threads.md) · [Rootapp](docs/rootapp-remote.md) · [Sharing](docs/temporary-sharing.md) |
| Releases | [Release notes](https://github.com/Babitdor/NovaCode/releases) · [Changelog](changelog/) |

## License and acknowledgments

NovaCode is [MIT licensed](LICENSE). Its agent infrastructure builds on LangGraph and deepagents. Native context compression adapts the **billion-context / acp-kernel** design, with attribution and license details in the [compression guide](docs/context-compression.md). Ponytail's vendored rules retain their upstream attribution in the [response-mode guide](docs/response-modes.md).
