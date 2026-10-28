![NovaCode banner](assets/Nova.png)

# NovaCode

An open-source terminal coding assistant that reads your project, plans changes,
edits files and runs checks. Choose cloud or local models and keep useful project
knowledge between sessions.

Built with Python, LangGraph, deepagents and Textual. [MIT licensed](LICENSE).

![NovaCode terminal in Classic style](assets/Preview-classic.png)

## Get started

Requires **Python 3.11+**, **Git** and **uv**, plus a supported model provider or
a running Ollama instance with a model installed.

```bash
git clone https://github.com/Babitdor/NovaCode.git
cd NovaCode
uv sync
uv run nova
```

First-run setup guides provider configuration and workspace approval. Hosted
models may charge for usage. To install a global `nova` command from this checkout,
run `uv tool install .`, then launch `nova` from your project directory.

## What Nova can do

- **Code and verify:** investigate projects, edit files, run commands and checks,
  and review changes with workspace and tool approvals.
- **Plan and delegate:** compare Council proposals, use specialist agents and
  coordinate project tabs with messages and Git worktrees.
- **Remember:** inspect and maintain a linked Markdown memory wiki, resume
  sessions and recover supported file snapshots.
- **Choose and extend:** select hosted or local models, configure an independent
  vision model, and add skills, plugins and MCP servers.
- **Compose tools in Code Mode:** toggle it below the composer to combine
  eligible native and MCP tools in bounded JavaScript workflows.
- **Speak and listen:** optional speech input and short spoken reply summaries,
  including lightweight CPU voice choices and model download progress.
- **Make it yours:** choose from 13 interface styles, including character art
  and optional animated home scenes.
- **Work beyond the terminal:** use remote chats, local web workspaces, temporary
  artifact previews and headless automation.

See [FEATURES.md](FEATURES.md) for the full catalog, setup requirements and
landing-page copy.

## Everyday use

| Command | Purpose |
| --- | --- |
| `/help` | Commands and keyboard shortcuts |
| `/model`, `/auth` | Models, vision and provider access |
| `/plan`, `/council <task>` | Investigate changes and compare plans |
| `/tabs`, `/resume` | Parallel projects and saved sessions |
| `/dream`, `/context` | Memory maintenance and context inspection |
| `/settings`, `/theme` | Voice, interface styles and preferences |
| `/mcp`, `/remote` | External tools and remote chat setup |
| `/restore` | Recoverable file snapshots |

Attach files with `@`; use **Shift+Enter** for a new line. For a one-shot task:

```bash
uv run nova -p "Summarize the changes in this repository"
```

## Optional features

Install selected extras from this checkout, for example:

```bash
uv sync --extra voice --extra voice-kokoro
```

Keep all desired `--extra` flags when syncing. Speech models download on first
use. See [setup and usage](SETUP.md) for global installation, voice providers,
browser automation and background-agent requirements.

**Local computer preview:** the `computer` extra lets Nova operate a
separate Linux QEMU VM with a private live browser preview and pause/resume/stop
controls. Requires QEMU, a guest-image download and 4 GiB RAM for the guest.
Start with `/computer start`; see [computer setup and limits](COMPUTER.md).
The Windows WHPX compatibility fix in this checkout has passed a real desktop boot.

## Documentation and contributing

[Setup and usage](SETUP.md) · [Feature catalog](FEATURES.md) ·
[Code Mode](docs/mcp-code-mode.md) · [Permissions](docs/security-guardrails.md) ·
[Sharing](docs/temporary-sharing.md) · [Changelog](changelog/)

For development, run `uv sync --group dev` and tests relevant to your change.
See [project instructions](AGENTS.md) for build and check commands. Report bugs
or propose changes through repository issues and pull requests.


### Compression preferences

The composer selector cycles **Off → Caveman → Ponytail → Off**. Options saves
mode, independent intensities and Caveman feature switches together; Escape discards
edits. Preferences persist across restarts. Existing Caveman settings migrate automatically.
Mode changes use temporary notifications and never submit your draft.

- `/caveman on|off|lite|full|ultra|wenyan-lite|wenyan-full|wenyan-ultra`
- `/ponytail` activates Ponytail without resetting its selected intensity.
- `/ponytail lite|full|ultra|off|settings|status`
- `/ponytail-review [scope]`, `/ponytail-audit [scope]`, `/ponytail-debt [scope]`,
  `/ponytail-gain`, `/ponytail-help` run the corresponding one-shot helper without
  changing the selected mode. Review and audit request reports, not edits.
- A standalone `stop ponytail` or `normal mode` disables Ponytail. Those phrases
  inside an ordinary task do not change preferences.

Caveman compresses prose and optionally tool views. Ponytail applies the upstream
coding rules: solve the complete problem with the least new code, preserve necessary
checks, and name skipped work and risks. Ponytail leaves tool results intact and
also applies to delegated model requests. Neither mode changes tool permissions.
Savings depend on the task and model; shorter output is not guaranteed.

Ponytail's MIT-licensed skill bodies and JavaScript instruction builder are vendored
from [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) at
`09c434ab73ca43761c5d8b0c5d341926c2fba710`. Nova's Python builder is checked
against golden hashes from that JavaScript builder. Nova owns the persistent selector;
upstream Claude-specific band, board, install hooks and default-mode files are not used.
There is no Node runtime requirement.

To validate every compression intensity against your configured LLM (20 paid/provider
calls, concurrency two, 60-second timeout each), run:

```sh
uv run python -m scripts.verify_compression_live
```

This opt-in check preserves exact failure facts/commands and validates generated
Python on boundary and invalid-bound cases. It writes synthetic outputs and reported
usage (or `unknown`) to `test-results/compression-live.json`, without changing your
preferences. Offline tests never call an LLM. Golden parity proves prompt equivalence;
it cannot guarantee identical stochastic model responses.
