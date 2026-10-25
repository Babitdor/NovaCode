# NovaCode setup and usage

<!-- product: novacode-cli; version: 1.0.99; scope: release; reviewed: 2026-10-25 -->

Detailed setup for NovaCode 1.0.99, including the local computer preview.
Start with the [README](README.md); browse the [feature catalog](FEATURES.md) for
capabilities, requirements and documentation links.

## Get started

Requires **Python 3.11+**, **Git** and **uv**. Configure a supported hosted provider or a running Ollama instance with a model installed.

```bash
git clone https://github.com/Babitdor/NovaCode.git
cd NovaCode
uv sync
uv run nova
```

First-run setup guides you through provider configuration and workspace approval. Provider API keys can be stored in the OS keychain. Hosted providers may charge for model usage.

When the selected chat model is text-only, it can inspect an attached image on demand with the `VISION` tool. Configure that independent vision model in `/model` → **Vision**; Nova does not switch the model answering your conversation. Multimodal chat models receive images directly.

To install a global `nova` command from this checkout:

```bash
uv tool install .
```

You can then run `nova` from your project's directory. After changing this checkout, reinstall the tool or use `uv run nova` to run the checkout directly.

## Messages between sessions

Open parallel project tabs with `/tabs`, then ask Nova to send a message to another
tab, for example: “Tell the backend session that the frontend now expects an `id`
field in the login response.”

To message a tab directly, type `@tab:` in the composer. Suggestions match the
session name, project or ID and show the project, session name and status.
Use Up/Down to choose, then Tab, Enter or a click to insert the address; type
your message and send it separately. For example:

```text
@tab:"API tests" Explain the login test failure
```

Names with spaces are quoted automatically. Duplicate names use stable IDs to
avoid sending to the wrong session. Existing `@tab:<session-id>` addresses work.
The main session is named Main; Telegram topics and Discord threads use readable
project/session labels, such as `backend · API tests`. Existing topic/thread
history stays attached when the label changes on the next connection or sync.

The built-in `SendMessage(recipient, content)` tool sends plain text to a session
ID or unique tab title found with `list_sessions`. Busy tabs queue messages until
their current turn finishes. The recipient processes the message in its own
project with its own permissions; its reply is linked in both tabs and available
through `read_session_messages`. Agents can also use `reply_session_message` for
an explicit reply. The existing `send_session_message` tool remains available.

Messaging connects tabs within one NovaCode TUI instance. Independently launched
terminal instances and standalone headless runs are not connected.

## Isolated worktrees

Ask Nova to “enter a worktree named login-fix” to use the essential
`EnterWorktreeTool(name="login-fix")`. It creates a fresh branch from `HEAD` and
moves the current session's file and shell tools into that worktree. Git and at
least one commit are required; uncommitted files stay in the original workspace.

`ExitWorktreeTool()` returns to the original workspace and keeps the worktree,
branch, commits and uncommitted changes. Nova reports the retained path for later
review or merging. Both tools run alone, follow normal approvals, and are blocked
in plan mode. They are always available alongside `SendMessage` without tool search.

Switching supports local agents. MCP servers, existing background jobs and remote
agents keep their own workspaces. Worktrees remain on disk after exit or restart;
the active workspace switch belongs to the running agent and is not restored by
resuming a saved conversation.

## Run from scripts

```bash
uv run nova -p "Summarize the changes in this repository"
uv run nova -p "Explain the project structure" --output-format json --max-turns 5 --timeout 120
```

One-shot mode exits after the task. It retains approval policies and reports when human input is required. Use `--auto-approve` only when unattended approvals are intended; `--deny-tools` rejects approval requests but does not disable every tool or guarantee read-only execution. Run `uv run nova --help` for all options.

## Optional capabilities

To install or update the global `nova` command with selected extras directly from GitHub:

```bash
uv tool install --upgrade "novacode-cli[voice,browser,agents-server] @ git+https://github.com/Babitdor/NovaCode.git"
```

To include all extras:

```bash
uv tool install --upgrade "novacode-cli[voice,voice-orpheus,voice-parakeet,voice-whistle,voice-pocket,voice-kokoro,voice-sano,browser,bedrock,server,agents-server] @ git+https://github.com/Babitdor/NovaCode.git" --index https://abetlen.github.io/llama-cpp-python/whl/cpu
```

The `--index` flag is needed whenever `voice-orpheus` is included: it fetches a prebuilt `llama-cpp-python` wheel instead of compiling it, which fails without a C++ toolchain (`CMAKE_C_COMPILER not set`).

From a local checkout, use `uv tool install --upgrade ".[voice,browser,agents-server]"` instead. After installing the tool, launch it with `nova` from any project directory. Keep your desired extras in the install command when updating; the extras list replaces the previous one, so omitted extras are uninstalled.

| Extra | Capability |
|---|---|
| `voice` | Local speech input and output |
| `voice-orpheus` | Optional Orpheus speech output |
| `voice-parakeet` | Optional Parakeet speech input |
| `voice-whistle` | Optional Whistle speech input (local Cactus runtime) |
| `voice-pocket` | Optional Pocket-TTS speech output |
| `voice-kokoro` | Kokoro 82M INT8 CPU speech (~92 MB weights + 28 MB voices) |
| `voice-sano` | Tiny sanoTTS CPU speech (~2–4 MB per English voice) |
| `browser` | Browser-use integration |
| `computer` | Local Linux VM preview; requires QEMU (see below) |
| `bedrock` | Amazon Bedrock provider |
| `server` | FastAPI and Uvicorn server dependencies |
| `agents-server` | On-demand background-agent server |

Install extras from the checkout as needed:

```bash
uv sync --extra voice          # Local speech input and output
uv sync --extra agents-server  # On-demand background-agent server
uv sync --extra browser        # Browser-use integration
```

For Whistle speech input from this checkout:

```bash
uv sync --extra voice --extra voice-whistle
uv run nova
```

In Nova, run `/voice settings stt whistle`, then `/voice download` to prepare
models. Whistle uses the local Cactus runtime. `/voice test` checks speech output;
verify input with a microphone request.

For lightweight English TTS, install `voice` plus `voice-kokoro` or `voice-sano`:

```bash
uv sync --extra voice --extra voice-kokoro --extra voice-sano
```

Select with `/voice settings tts kokoro --voice af_heart` or
`/voice settings tts sano --voice amy-1p1m`, then `/voice test`.
Both run on CPU, load only on first use, reuse cached weights, and report model
download progress above the composer. `/model` and `/settings` also list their
voices. Piper remains the default. File sizes describe downloads, not total RAM.
Kokoro is Apache-2.0; sanoTTS is GPL-3.0. Voice weights download from their
upstream publishers on first use.

Automatic speech reads short summaries of Nova's final prose, excluding tool
calls/results, reasoning and subagent output. STT and TTS downloads have separate
progress rows above the composer; a percentage appears only when the provider
reports a total. Model choice is also available in `/model` (alias `/models`)
and `/settings`. Physical audio quality and low-resource latency depend on the
machine and have not been benchmarked across configurations.

Combine `--extra` flags to keep multiple extras installed. Voice models may need an initial download; browser integrations may need additional browser setup. Without the agent-server extra, in-process delegation remains available.

### Nova's own computer (preview)

Included as a preview in v1.0.99. The working-tree Windows WHPX compatibility
fix has passed a real Linux desktop boot, screenshot, pause/resume and stop on
the development PC. See [computer setup](COMPUTER.md) for the tested scope.

Install the `computer` extra and QEMU, then run `/computer start`. Nova creates
a separate Linux VM (2 CPUs, 4 GiB RAM) and opens its live desktop preview in
your browser. Ask Nova to use its computer to work in desktop apps or websites.
`/computer pause` blocks input; `/computer resume` restores it;
`/computer preview` reopens the view; `/computer stop` deletes the temporary VM.

```bash
uv tool install --upgrade ".[computer]"
```

Keep any other desired extras in that install command. The first VM startup
downloads the Linux guest image. Windows needs QEMU installed separately;
see [computer setup and limits](COMPUTER.md) for platform prerequisites,
preview controls, and the VM's file and network boundaries.

## Design and deployment integrations

Open `/mcp` to install **Figma**, **Vercel** or **Higgsfield**, then select **Authenticate** to sign in. No API keys or local server packages are needed for these hosted presets. You can also configure them with `nova mcp install figma`, `nova mcp install vercel` or `nova mcp install higgsfield`.

[Figma remote access](https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/) is limited to approved clients. If Nova cannot connect, use the **Figma Desktop** preset with its desktop MCP server enabled and an eligible seat. [Vercel](https://vercel.com/docs/agent-resources/vercel-mcp) uses your account permissions; [Higgsfield](https://higgsfield.ai/creator-hub/help-center/integrations/how-do-i-connect-higgsfield-to-ai-agent) requires a paid subscription.

## Remote sessions and sharing

Set up Telegram, Discord or Rootapp with `/remote`. See
[Discord session threads](docs/discord-session-threads.md) and
[Rootapp setup](docs/rootapp-remote.md) for service-specific requirements.

`/share artifacts` and `/share all` expose the artifact collection through a
Cloudflare Quick Tunnel and require `cloudflared`. Anyone with the link can
access it while Nova and its viewer run. Use `/share stop` to revoke the tunnel.
See [temporary sharing](docs/temporary-sharing.md) before publishing content.

## Code Mode

Use the toggle below the composer to enable Code Mode. Nova can combine eligible
native tools and MCP calls in bounded JavaScript, even without MCP servers.
Interactive approvals, delegation and graph/workspace controls keep their direct
paths. See [Code Mode](docs/mcp-code-mode.md) for exact namespaces and limits.

## Interface and vision

Use `/theme` to choose interface style, colors and animation settings. Dashboard,
Classic, Centered, Banner and Minimal are joined by Terminal, Workbench, Notebook,
Outline, Focus, Studio, Ledger, Blueprint and Signal. Signal pairs Classic's
full-height Nova banner and logo with Matrix3 code rain and a neutral, orange-accented
`nova-signal` theme. Choose Signal under Interface style; an explicitly saved
color theme stays selected. Workbench, Studio and Signal use Nova's
character art; Terminal, Studio, Blueprint and Signal have optional animated home scenes.
Signal's Home animations toggle controls its rain, including its fading trails
and bright heads. Signal uses the Japanese characters `生涯最愛の人` ("love of
my life"); departed chains leave no ghost text. Its composer spans the terminal.
Classic rain uses the separate Matrix Rain toggle.
Low-resource mode pauses those animations.

Configure an independent vision model in `/model` → Vision when the main model
is text-only. The `VISION` tool inspects images on demand without switching the
model answering the conversation. Multimodal main models receive images directly.
