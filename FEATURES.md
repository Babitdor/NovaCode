# NovaCode feature inventory

<!-- product: novacode-cli; version: 1.0.99; scope: release; reviewed: 2026-10-25 -->

Landing-page and documentation reference for **v1.0.99**, including the explicitly marked
local computer preview. Reviewed against source, command registrations
and release notes on **25 October 2026**. An implemented capability is not a
benchmark or proof that every provider and platform has been exercised.

Use the short copy for feature cards and the detailed catalog for product pages,
screenshots and documentation links. Requirements and limits belong near the
relevant feature. This is a local content brief; creating it does not update or
publish the landing page.

## Product positioning

**Headline:** Your terminal. Your models. A coding workspace that remembers.

**Description:** NovaCode is an open-source terminal coding assistant that reads
your project, plans changes, edits files and runs checks. Work with specialist
agents, compare ideas in Council, continue through remote chats, and turn useful
outputs into temporary web previews.

**Supporting line:** Choose hosted or local models. Keep project context between
sessions. Control tool approvals, workspaces and optional integrations.

**Primary actions:** Get started · Explore features · Read the docs

## Suggested feature cards

| Feature | Landing-page copy | Suggested visual |
| --- | --- | --- |
| Specialist agents | Delegate coding, research, reviews and testing to specialists while you keep working. | Inline agent statuses and background tasks |
| Council | Gather independent perspectives, debate options and get a chairman's consolidated recommendation. | Advisor sidebar, ballots and consensus view |
| Persistent memory | Keep useful project knowledge in a linked memory wiki you can inspect and maintain. | Memory index and `/dream` controls |
| Model choice and vision | Choose cloud or local models, with independent roles and an on-demand vision companion for text-only models. | Model picker and Vision settings |
| Code Mode | Combine eligible native and MCP tools in bounded JavaScript workflows, with a toggle below the composer. | Composer toggle, a short script and its selected result |
| Voice conversation | Dictate requests and hear short spoken reply summaries, with lightweight CPU voice options. | Voice model picker and real download progress |
| Your terminal style | Choose from 14 interface styles, including Nova character art and optional animated homes. | Signal's Classic banner with Matrix3 code rain (unreleased) |
| Caveman mode | Choose terse answers, recoverable compact tool output and other independent token-efficiency features. | Composer toggle and intensity picker |
| Remote sessions | Continue through Telegram topics, Discord threads or an optional Rootapp companion. | Matching terminal session and remote conversation |
| Artifacts | Turn results into web previews and share the session's collection through a temporary Cloudflare URL. | Responsive artifact cards and a phone preview |
| Parallel workspaces | Run project tabs, exchange explicit messages and isolate changes in Git worktrees. | Session tabs and a worktree transition |
| Permissions and guardrails | Keep tool policy, workspace boundaries and prompt-injection handling in the agent workflow. | An approval request and scoped folder controls |

The local computer is a **preview in v1.0.99**. Use “Preview: a separate Linux
computer with a private live view” in a preview area. The working-tree WHPX
workaround has passed one Windows desktop boot; keep
that scope explicit rather than promising compatibility on every machine.
Do not use the Classic screenshot as evidence of new styles or VM operation.

## Recent additions for landing pages and docs

| Capability | Status | Setup / documentation | Claim boundary |
| --- | --- | --- | --- |
| Native + MCP Code Mode | Released in v1.0.97 | Composer toggle; [Code Mode](docs/mcp-code-mode.md) | Eligible tools only; interactive and graph-control calls remain direct. |
| Whistle STT | Released in v1.0.97 | `voice` + `voice-whistle`; [voice setup](SETUP.md#optional-capabilities) | Local Cactus runtime; microphone quality and transcription accuracy are not benchmarked. |
| Kokoro and sanoTTS | Released in v1.0.98 | `voice` + the selected provider extra; [voice setup](SETUP.md#optional-capabilities) | CPU options; download size is not peak RAM or a speed guarantee. |
| Download progress and earlier speech playback | Released in v1.0.98 | `/voice download`, `/voice test`, `/settings` | Percentages need a backend-reported total; no universal latency claim. |
| Eight additional interface styles and creative homes | Included in v1.0.99 | `/theme`; [interface guide](SETUP.md#interface-and-vision) | Animations are optional; new screenshots still needed. |
| Local Linux computer and live preview | Preview in v1.0.99 | `computer` extra + QEMU; [computer guide](COMPUTER.md) | Offline tests plus one real Windows desktop boot with the working-tree WHPX workaround. |

## 1. Everyday coding tools

**Benefit:** Investigate, change and verify a project from the same conversation.

- Read, create and edit files, search project content, execute commands and run
  checks through Nova's agent tools.
- Keep both native-shell and Bash execution paths for their different interpreter
  needs. Both remain subject to the applicable approval policy.
- Edit existing Jupyter notebooks with **`NotebookEditTool`**: insert, replace or
  delete cells without starting a kernel. Preserve unrelated cells and notebook
  metadata; changed code cells lose stale outputs and execution counts.
- Use native **bfs** traversal and **ugrep** search when installed, with ripgrep
  and Python fallbacks. Imported definitions can use **GlobTool/GrepTool** aliases
  for the existing `glob` and `grep` tools.
- Open attached files and images in the conversation; inspect the session's
  file activity with `/files` and manage image attachments with `/images`.
- Run project checks with `/tests`; manage development servers with `/servers`.

Notebook editing is local-workspace scoped, follows approval rules and is blocked
in plan mode. Native search engines are optional executables, not auto-installed
dependencies. [Tool details](docs/essential-tools.md).

## 2. Specialist agents and background work

**Benefit:** Give independent work to specialists without replacing the main conversation.

- Use in-process delegation for results needed during the current task and async
  agents for independent background work.
- Create file-defined dynamic agents with project or global scope. Project
  definitions override matching global names.
- Invoke recognized agents with `@name`; dual-mode agents normally use the
  in-process path, while background-only definitions route to async execution.
- Use `/agents` and `/subagents` to inspect or manage definitions. Changes refresh
  at supported turn boundaries without clearing the conversation.
- Built-in specialists cover code exploration, bug investigation and fixes,
  refactoring, review, security, dependencies, performance, documentation,
  test generation, test execution, browser automation and plan scouting.
- Give specialists selective skill libraries rather than loading the entire
  library into every agent.
- See local agents as plain inline running/completed/failed status lines.
  Async work retains its background monitoring and task views.
- Receive bounded retry progress and model-call deadlines rather than stacked
  SDK retries. The normal bounded async policy permits at most two Nova retries
  within a four-minute **model-call** budget; long-running tools have their own
  lifecycle.
- Keep watching long-running async jobs, including connection-loss and recovery
  notices. A monitoring warning is not treated as completed research.

Async agents need the optional `agents-server` installation and a usable agent
server. `/agent-server` reports availability and setup requirements. Agent mentions
must resolve to known definitions; arbitrary `@names` are not universal dispatch
commands. [Agent definitions](novacode_cli/agents/default_subagents/builtin/).

## 3. Experimental agent teams

**Benefit:** Coordinate persistent teammates for larger tasks.

- Enable teams in `/settings` → Experimental; they are **off by default**.
- Use `spawn_agent` for persistent teammates with scoped inboxes and task claims.
- Use supported in-process or separate-process/worktree worker paths.
- Forward teammate approval requests through the parent policy flow and track
  worker ownership and lifecycle separately from the lead's current turn.
- Enable the separately gated swarm scheduler when teams are enabled.
- Keep the selected teammate model identity visible in runtime records.

Teams, swarm scheduling and the configured container adapter are experimental.
Do not describe them as independently certified production orchestration or
promise durable reconnect/resume for every worker path.

## 4. Web Council: decisions through discussion

**Benefit:** Compare ideas before committing to a direction.

Open `/council` or `/council web` for the local browser workspace.

- Run specialist analyses in parallel, collect peer ballots, then stream a
  dedicated chairman's synthesis.
- Enter a decision brief with explicit criteria and up to eight options.
- Let advisors ask clarification questions with suggested choices or free text.
- Collect recommendations, risks, tradeoffs and next steps from different
  perspectives.
- Vote on actual options. Invalid ballots abstain; ties remain unresolved.
- Give the chairman visible analyses, ballot reasons, tallies, clarifications
  and member failures to reconcile disagreements and retain minority concerns.
- Approve, revise or reject the result yourself. Votes are advisory.
- Switch between **Synthesized consensus** and **Full debate** views.
- Apply **Code Audit**, **Architecture** and **Creative Debate** presets without
  overwriting saved model assignments or Council settings.
- Edit advisor identities, personality templates, system instructions and
  provider/model overrides in the web settings. Save commits the draft;
  Cancel discards it. The judge configuration supplies the web chairman.
- Follow real advisor states, ballots, elapsed time and the three-stage pipeline.
  No invented confidence percentages or hardware telemetry.
- Use the advisor sidebar and persistent composer on desktop, with a sidebar
  drawer and keyboard focus handling on smaller screens.
- Stop a discussion; use follow-ups, clear the displayed view, or start a new
  discussion to reset its context.
- Keep SSE streams maintained with heartbeats, bounded buffering, disconnect
  cancellation, worker cleanup and explicit terminal events.

This is distinct from **Planning Council**. `/council <task>` and its planning
approval commands keep their existing planning workflow. The web consensus
redesign does not silently replace it. The chairman uses visible analyses and
vote reasons, not hidden model chain-of-thought. Consensus and factual correctness
are not guaranteed.

Sources: [Council consensus](novacode_cli/council_consensus.py),
[workspace presets](novacode_cli/council_workspace.py),
[settings reference](docs/COUNCIL-SETTINGS.md).

## 5. Cowork workspace

**Benefit:** Work with Nova in a visual, folder-aware local workspace.

- Launch `/cowork` with an optional initial task.
- Use a left sidebar for workspace permissions, tasks and activity, alongside
  the conversation on the right.
- Grant and revoke folder access through the workspace controls.
- Answer questions, follow activity, stop work and manage the current session.
- Keep section headings and tabs visible while long task, activity and folder
  lists scroll independently.
- Use responsive layouts and a mobile sidebar drawer.

Cowork retains its local application/server behavior. It is not exposed by
`/share`, and folder permissions are not a universal OS sandbox.
Sources: [Cowork](novacode_cli/cowork/), [web interface](novacode_cli/ui/cowork_web.py).

## 6. Create: skills and agent editor

**Benefit:** Maintain reusable skills and agent definitions through a local web UI.

- Open `/create` to browse and preview skills and agents.
- Create, edit or delete supported definitions in project and global scopes.
- Keep reusable instructions in their native files instead of only in a chat.
- Use `/create stop` to stop the local UI server.

Create is a skills-and-agents editor, not a general website builder. Public
sharing through `/share create` is not supported.
Source: [Create handler](novacode_cli/commands/create_handler.py).

## 7. Artifacts and temporary Cloudflare previews

**Benefit:** Turn a useful result into a page you can open outside the terminal.

- Create HTML, Markdown or dashboard artifacts and update the same artifact
  in place, retaining its ID and recording new versions.
- Browse a responsive collection with cards, type accents, timestamps, versions
  and visible IDs that distinguish repeated titles.
- Search by title, type or ID and change the sort order.
- Access multiple artifacts through the session's shared collection, with
  separate previews for individual items.
- Automatically attempt temporary hosting when an artifact is created, using
  **Cloudflare Quick Tunnels**. `/share artifacts` and `/share all` use the same
  artifact-hosting path.
- Restore artifact content when resuming a saved session; a fresh tunnel can
  expose the restored collection again.
- List active shares with `/share list` and revoke them with `/share stop`.
- Clearing the session removes its active artifact hosting. The public preview
  also ends when Nova or the local viewer stops.
- Keep artifact content inside the preview sandbox. Authoring guidance and
  overflow guards support mobile layouts, while fixed-width content can still
  need its own responsive redesign.

Install `cloudflared` for a public URL. If it is unavailable, local artifacts
remain usable and Nova reports the hosting problem. Quick Tunnel URLs are
temporary, change on restart, and are accessible to anyone with the link. This
is not permanent deployment, authentication or an uptime guarantee. There is
no silent ngrok fallback for artifacts.

Council, Cowork and Create keep local servers; their former public-share
commands and the combined sharing dashboard were removed.
[Sharing reference](docs/temporary-sharing.md).

## 8. Remote sessions: Telegram, Discord and Rootapp

**Benefit:** Keep following a terminal session through your preferred remote chat.

### Telegram

- Route Nova sessions to separate named topics, with their own conversation and
  session identity.
- Stream terminal prompts, tool activity, progress and editable replies into
  the corresponding topic, including supported background turns.
- Accept authorized remote prompts, photos and image documents with captions.
- Use readable status sections, task checkboxes, stacked table records and
  expandable long diff previews designed for phone screens.
- Run supported commands and skills through chat, using numbered controls for
  interactive panels.
- Authorize or revoke users through `/remote` controls or its subcommands.
- Reconnect to existing topics, recover confirmed-deleted topics and share a
  local polling connection across Nova windows rather than competing for updates.
- Launch or reopen approved project tabs through supported remote pickers.

### Discord

- Create a dedicated thread or forum post for each Nova session.
- Restore session-to-thread routing on reconnect/resume and archive threads when
  their sessions close.
- Mirror terminal prompts, live tool activity and streamed replies into the
  matching thread, including supported child-session turns.
- Report inaccessible parent channels and missing permissions clearly.

### Rootapp

- Use the optional Rootapp companion for an authorized community channel.
- Save bridge configuration, route sessions and update responses through Root's
  local DevHost integration.
- Follow the companion build/setup flow, Bot ID and sender-authorization setup.

Use `/remote` for setup and connection management. Remote services need their
own accounts, bot configuration, channel permissions and authorized senders.
They do not silently broaden Nova's workspace permissions. Rootapp requires
its companion setup; do not present it as the same installation flow as Telegram.

Sources: [Discord threads](docs/discord-session-threads.md),
[Rootapp setup](docs/rootapp-remote.md), [remote bridges](novacode_cli/remote/).

## 9. Model choice, roles, routing and vision

**Benefit:** Choose models for the work rather than tying every capability to one provider.

- Configure providers and credentials with `/auth`; select models through
  `/model` or its `/models` alias.
- Supported provider IDs cover OpenAI, Anthropic, Google, Ollama, NVIDIA,
  OpenRouter, Command Code, OpenCode Go, OpenCode Zen, Azure OpenAI, Amazon
  Bedrock, Mistral, Groq, Cerebras, xAI, Hugging Face, Kimi For Coding and MiniMax.
- Assign main, in-process, async and dynamic-agent model roles independently.
- Configure per-turn `/router` choices with natural-language criteria, a decision
  model and a fallback route.
- Select reasoning effort with `/effort`, including supported per-route effort
  settings. Controls are mapped to the provider/model's supported interface.
- Let multimodal main models inspect attachments directly.
- Give a text-only main model an independent **`VISION`** side-call configured in
  `/model` → Vision. Inspection happens on demand; it does not switch the model
  answering the conversation.
- Bound vision calls and show timeout/provider failure reasons so a failed image
  inspection remains diagnosable.
- Use supported provider sign-in flows, including OpenAI ChatGPT sign-in and
  Google setup, alongside API-key configuration.

Provider access, model capability, region, subscriptions and billing still apply.
Bedrock is an optional installation extra. Do not promise every account or model
supports vision, strict tool sampling, OAuth or identical effort settings.
[Reasoning support](docs/reasoning-support.md), [vision routing](novacode_cli/bootstrap/vision_router.py).

## 10. Caveman mode and token visibility

**Benefit:** Choose how to reduce verbosity and unnecessary model context.

The composer toggle, Options panel and `/caveman` expose six independent features.
The master switch remains opt-in; the individual switches start selected.

| Feature | Current behavior |
| --- | --- |
| Say less | Skill-aligned prose instructions, exact code/identifiers and clarity exceptions for warnings and ambiguity. Full is the default intensity. |
| Read less | Compact eligible logs, JSON and CSV in the model's view while retaining exact recoverable sources. |
| Auto | Use configured router choices and per-route effort without changing the saved session model. |
| Blocks | Find and recover exact scripts from prior tool calls, then rerun through the normal execution tool and current permissions. |
| Waste fixes | Reduce duplicate tool results and report local estimates plus provider-reported input, output, total, cache and reasoning usage. |
| Browse | Compact eligible HTML and Markdown tool results, retaining references for exact recovery. |

- Select `lite`, `full`, `ultra`, `wenyan-lite`, `wenyan-full` or `wenyan-ultra`
  through Options or `/caveman <level>`. Preserve the requested reply language;
  Wenyan styles Chinese replies without translating other languages into Chinese.
- Replace Nova's exact bundled response-style block rather than adding a second
  competing directive. Preserve custom instructions and security rules.
- Apply the selected prose style to Nova-managed subagents. Local/background
  style-only integration retains their configured models and tool behavior.
- Recover source text and saved script arguments within the current checkpoint
  thread, including after old messages have been removed.
- Use `/caveman audit`, `/caveman blocks` and the read-only recovery utility.
- Keep provider cache observations distinct from estimated text-token counts.

Do not advertise “75% fewer tokens,” “54% cheaper,” identical model quality or
another project's benchmark as Nova's result. Shorter visible answers can still
consume more total tokens on reasoning models. Savings and style compliance
vary; no paid idle cache-warming requests are sent. The six-part system extends
the Caveman skill, which itself defines prose style.
[Configuration, audit and live-check limits](docs/caveman-mode.md).

## 11. MCP discovery, Code Mode and tool search

**Benefit:** Connect external capabilities without loading every schema or intermediate payload.

- Manage MCP servers with `/mcp` and discover tools dynamically.
- Use **`MCPTool`** to list tools, inspect their JSON schemas and call exact
  server/tool names. Select structured result paths and bound returned output.
- Toggle Code Mode below the prompt composer; the preference persists.
- Use **`MCPCode`** with `tools.list`, `tools.describe` and `tools.call` for
  eligible registered native tools, even without MCP servers. Existing
  `mcp.list`, `mcp.describe` and `mcp.call` scripts remain supported.
- Combine discovery and calls in an isolated JavaScript async-function body:
  loops, branches, dependent calls, bounded `Promise.all` and local filtering
  are supported.
- Return only selected results to the model while keeping intermediate data in
  the script runtime. Normal traces may still retain nested call data.
- Preserve the configured inventory, argument validation, downstream tool
  middleware and approval decisions for nested native and MCP calls.
- Bound runtime memory, code size, execution time, call count, concurrency and
  returned results. Cancel pending work when a script is interrupted.
- Keep existing `eval`/subagent workflows separate from MCP Code Mode.
- Search tool schemas with the existing hybrid `search_tools`, a regex variant
  or an independent BM25 natural-language ranking variant.
- Load less-used schemas when needed rather than keeping the full catalog in
  every model request.

Code Mode needs Nova's async agent loop. Native workflows do not need an MCP
server; `mcp.*` calls need configured servers. JavaScript has no general host
imports or filesystem/network API; authorized tools retain their capabilities.
Delegation, user questions, graph-state changes, workspace/mode transitions and
computer image/action results keep their direct paths. Calls needing interactive
approval must be made directly rather than replaying the script. External side
effects cannot be rolled back. Dynamic agents run through the separate `eval`
workflow; Code Mode does not replace their execution engine.
The local regex/BM25 implementations are not Anthropic server-side search tools.
[MCP Code Mode](docs/mcp-code-mode.md), [search and MCP reference](docs/essential-tools.md).

## 12. Strict tool arguments

**Benefit:** Catch malformed tool calls before they execute.

- Use default `strict_tools="auto"` to request native constrained tool schemas
  where Nova has a supported route and compatible schema.
- Validate raw arguments without coercion in the enabled strict path; a string
  `"2"` is not silently treated as integer `2`.
- Apply the middleware to main agents and supported subagent paths.
- Use `on` to require/request a supported native strict mode, or `off` to retain
  the previous provider behavior.

Native grammar-constrained sampling is provider/model/schema dependent.
Validation cannot establish authorization, task correctness or successful tool
execution. [Provider matrix](docs/essential-tools.md#strict-tool-use).

## 13. Sessions, branches and project tabs

**Benefit:** Resume work without flattening every task into one conversation.

- Launch, switch, reopen and close project sessions with `/tabs`.
- Resume saved work through a searchable browser with task, project, model,
  status, blockers and next-step details.
- Preserve session-specific model choices, drafts, scroll position and supported
  approval state across tab interactions.
- Browse branching history with `/tree`, bookmark entries and continue from
  earlier points while retaining the old branch.
- Import Nova, Claude Code and Codex histories as structured context with
  `/sessions` and `/import`; compare supported histories with `/compare`.
- Use `/rewind` for supported conversation/code rollback and `/restore` for
  recoverable file snapshots.
- Export a self-contained HTML session tree with `/export [file]`.
- With confirmation, bare `/share` uploads a session export as a secret GitHub
  gist. This is separate from temporary artifact hosting.

A secret gist is unlisted, not authenticated private storage; it can remain
available independently of Nova until deleted. Export redaction is a protection,
not a guarantee that arbitrary private project content is safe to publish.

## 14. Messages between sessions

**Benefit:** Coordinate separate projects without sharing their entire context.

- Use the essential **`SendMessage(recipient, content)`** tool to send plain text
  to a live session ID or unique tab title.
- Discover recipients with `list_sessions`; inspect and explicitly reply with
  the session-message tools.
- Queue delivery while the recipient is busy and keep replies linked to the
  originating request.
- Retain each recipient's workspace, model, history and approval settings.
- Address a tab with `@tab:` autocomplete: search session names, projects or IDs,
  then accept with Tab, Enter or a click. Suggestions show project/name/status;
  names with spaces are quoted and ambiguous names use stable IDs.
- Identify the main session as Main and label Telegram topics and Discord
  threads with project/session names while retaining their existing routing.

Messaging connects live tabs in one NovaCode TUI instance. It does not connect
arbitrary independently launched terminals or standalone headless sessions.
[Messaging usage](SETUP.md#messages-between-sessions).

## 15. Isolated Git worktrees

**Benefit:** Separate feature work from the original checkout.

- Use essential **`EnterWorktreeTool(name)`** to create a fresh branch/worktree
  from `HEAD` and move the current local agent's file and shell tools into it.
- Use **`ExitWorktreeTool()`** to return while retaining the worktree, branch,
  commits and uncommitted changes for later review or merging.
- Keep the original checkout's uncommitted files in their original location.
- Apply normal approvals, plan-mode restrictions and transition guards.
- Use the separate `/worktree` command for its worktree-plus-session workflow.

Git and an existing commit are required. Existing jobs, external MCP servers
and remote agents keep their own workspaces. The live workspace switch is not
restored merely by resuming a conversation.
[Worktree usage](SETUP.md#isolated-worktrees).

## 16. Memory, learning and skills

**Benefit:** Keep useful knowledge inspectable and reusable across tasks.

- Maintain a linked Markdown memory wiki with indexes, topic maps and related
  pages, including project-scoped notes and stable habits.
- Use `/dream` for consolidation and its automatic-maintenance controls.
- Retain backups and recoverable memory overflow; check broken links and avoid
  treating a remembered path as permission to write there.
- Keep the main agent's coding instructions packaged with Nova. User memory
  does not replace the baked-in base system prompt.
- Review learning and proposal history through `/learning`, `/evolution` and
  related controls. Generated proposals still follow their approval lifecycle.
- Load skills progressively with `$skill-name`, `/skill-name` or Pin to prompt.
- Read supporting resources on demand with `skills_load` and
  `skills_read_resource`, instead of preloading every reference.
- Activate declared skill tools in the applicable run and scope.
- Let custom agents restrict their skill library or use the full enabled set.
- Install and manage plugins, marketplaces and hooks through their controls.

Learning does not certify that a model has learned a correct general rule.
Memory is contextual evidence, not authorization. [Prompt audit](docs/prompt-audit.md),
[prompt templates](novacode_cli/prompts/), [skills implementation](novacode_cli/skills/).

## 17. Planning, reviews, research and goals

**Benefit:** Choose a deliberate workflow for complex work.

- Use `/plan` for read-only investigation and an explicit implementation handoff.
- Run `/claude-plan` or `/codex-plan` for supported external planning, then let
  Nova implement the approved plan.
- Run `/claude-review` or `/codex-review` for external read-only review and let
  Nova verify and report findings.
- Save approved plans in supported project/global stores and recall them with
  `recent_plans`. Imported execution todos retain the plan's required items.
- Use `/research` for a multi-agent research workflow.
- Keep a persistent goal with pause/resume and turn-budget controls through
  `/goal`; retain its objective and lifecycle in saved sessions.
- Use `/btw` for a separate quick question without changing the main conversation.
- Work with the `/trello` browser task board and its optional dedicated Telegram
  intake topic.

External Claude Code/Codex workflows require those tools and their own access.
The task board remains in memory while it runs; a goal or todo is not proof of
successful execution. [Command registry](novacode_cli/tui/app.py).

## 18. Background terminals, daemons and scheduled tasks

**Benefit:** Keep the conversation responsive while commands and repeatable work run.

- Send supported running commands or agent turns to the background with Ctrl+B.
- Inspect background tasks, bounded log tails and completion/cancellation state
  through `/tasks` and the relevant task tools.
- Use bounded `wait_for_task` checks and inspect active waiters. New user input
  can interrupt waiting without cancelling the underlying task.
- Launch explicitly authorized detached processes with the **`daemon`** tool;
  inspect status, read logs and stop tracked daemons.
- Validate process identity before daemon termination and clean up supported
  process trees on Windows and POSIX systems.
- Manage scheduled tasks with `/cron`, **`CronCreateTool`**,
  **`CronDeleteTool`** and **`CronListTool`**, backed by persistent job storage.
- Optionally expose **`TerminalCaptureTool`** for retained Nova-managed panel
  text, and **`CtxInspectTool`** for context diagnostics, through their feature gates.

Ordinary background jobs belong to the session. Detached daemons can survive
the CLI process but are not a reboot-persistent system service. Cron tasks need
an active interactive Nova consumer; this is not operating-system cron.
Terminal capture is not an OS screenshot or access to arbitrary terminal apps.
Inspection tools are disabled by default. [Tool reference](docs/essential-tools.md).

## 19. Tracing, usage and context control

**Benefit:** See what the agent is doing and where tokens are going.

- Configure LangSmith with `/trace setup`; enable/disable tracing, inspect
  status and use the supported project/trace commands.
- Persist non-secret tracing settings and store the API key in Nova's credential
  store. Restore tracing configuration on startup.
- Apply setup changes to running agent streams and clear stale SDK client/env
  caches without requiring a restart.
- Attach optional workspace, agent ID and environment metadata; the configured
  LangSmith project remains the trace destination.
- Inspect context categories and assembled tool-schema costs with `/context`.
- Compact history with `/compact`, preserve supported recoverable sources and
  keep context budgets consistent with the UI.
- View token/API/plan usage with `/usage`, `/tokens` and `/cost`.
- Show provider-reported prompt-cache reads/writes when available, without
  fabricating cache hits for providers that do not report them.

Tracing needs a working LangSmith account, destination and credentials. Provider
usage observations, local estimates and pricing calculations are different kinds
of evidence. [Tracing implementation](novacode_cli/tracking/),
[context snapshots](novacode_cli/context/snapshot.py).

## 20. Permissions and prompt-injection guardrails

**Benefit:** Keep approvals and project boundaries visible as work is delegated.

- Use workspace approval, scoped path grants, tool policies and explicit denies.
- Choose session auto-approval with `/auto-approve`; the composer indicates when
  it is active. Child sessions retain their own approval state.
- Optionally use a configured decision model for eligible auto-approval reviews;
  uncertain/unavailable verdicts fail closed and explicit denies remain enforced.
- Automatically approve ordinary delegated tool calls while retaining denies,
  workspace grants and parent approval for ungranted outside-project file writes.
- Reject supported traversal, Windows drive/UNC/device/alternate-stream escapes
  and canonical-path denials in virtual filesystem paths.
- Treat tool results, web content, repository text, images/OCR, memories and peer
  messages as lower-trust evidence rather than permission or system instructions.
- Redact supported credential-like values in model-visible tool copies and block
  tool arguments that expose secret environment values.
- Keep API-key storage separate from ordinary settings and preserve sender
  authorization on remote bridges.

These protections are not an OS sandbox, formal security proof or guarantee
against every prompt injection. Shells, plugins and external servers retain
their own execution capabilities. Do not market auto-approval as unrestricted
access or redaction as permission to publish private data.
[Threat model and limits](docs/security-guardrails.md).

## 21. Configurable terminal experience

**Benefit:** Adapt the workspace without leaving the terminal.

- Choose **14 interface styles**: Dashboard, Classic, Centered, Banner, Minimal,
  Terminal, Workbench, Notebook, Outline, Focus, Studio, Ledger, Blueprint and Signal.
  Signal is unreleased: Classic's full-height Nova banner and logo meet Matrix3 rain,
  with an orange-accented `nova-signal` theme. Explicit color choices are preserved.
- Use distinct home compositions with keyboard-accessible Explain / Refactor /
  Build / Debug starters that fill an editable prompt before sending.
- See Nova character art in Workbench, Studio and Signal, with optional animated home
  scenes in Terminal, Studio, Blueprint and Signal. Low-resource mode pauses them;
  short terminals prioritize the composer over large artwork.
- Carry the selected style into conversation, tool and dialog presentation,
  independently of the color theme.
- Change themes, including light and dark palettes, with `/theme` or `/settings`.
- Customize the status line with a live preview through `/statusline`.
- Use a composer that grows with multiline input and soft wraps; keep drafts
  while changing supported views.
- Queue messages during work, steer at supported model-step boundaries or
  interrupt and redirect through `/redirect`.
- Use input history, jump-to-latest controls and session-specific scrolling.
- Keep long command output responsive through batching, bounded live buffers
  and retained-log rendering.
- Show an animated terminal-title spinner while the active session works.
- Optionally display Nova's reactive character-cell avatar with `/nova`.
- Choose animation frame rate and opt into Matrix Rain instead of requiring
  continuous decorative animation.
- Use searchable, categorized `/help` for built-ins, aliases, skills and shortcuts.

## 22. Integrations, voice and headless use

**Benefit:** Bring Nova into more of the development workflow.

- Connect generic MCP servers and use presets for Figma, Figma Desktop, Vercel
  and Higgsfield through `/mcp`, with supported browser authentication.
- Use optional browser automation and semantic code-search capabilities.
- Dictate requests with local speech input and hear short summaries of Nova's
  final prose. Automatic speech excludes tool calls/results, reasoning and
  subagent payloads.
- Select speech providers and voices through `/model` (or `/models`), `/settings`
  or `/voice settings`. Faster-Whisper and Piper remain the defaults.
- Add **Whistle STT** with `voice-whistle` using the local Cactus runtime;
  Parakeet is another optional speech-input provider.
- Choose CPU speech output with **Kokoro 82M INT8** (six English voices,
  roughly 120 MB total weights/voice download) or **sanoTTS** (four English
  voices, roughly 2–4 MB per selected voice). Pocket and Orpheus remain optional
  alternatives; ElevenLabs requires service credentials and may incur charges.
- See separate STT/TTS download rows above the composer: transferred bytes and
  percentages when totals are known, otherwise indeterminate progress.
  Cached models remain quiet. Use `/voice download` to prepare models.
- Begin Piper playback sentence by sentence instead of buffering the whole
  reply. The extra summary-model call has a two-second budget with a filtered
  short-prose fallback. Cancellation prevents late synthesis from starting
  playback; model instances and downloaded weights are reused where supported.
- Run one-shot tasks with `nova -p`, including structured output and turn/time
  limits.
- Build remote-app adapters around persistent `--mode pipe` / `--headless`
  JSONL sessions with request IDs, readiness, approvals, cancellation and queues.
- Keep headless approval policy explicit rather than treating print mode as
  automatic workspace trust.

Optional extras, external tools, browser setup, account eligibility and service
subscriptions may be required. Figma remote access has client restrictions;
Higgsfield is not a free included subscription. [Setup and extras](SETUP.md#optional-capabilities).

Voice requires the `voice` extra plus any selected provider extra, model downloads
and working audio devices. Kokoro is Apache-2.0; sanoTTS is GPL-3.0. Download
footprints are not total RAM. Physical microphone/speaker behavior, peak memory
and universal low-resource latency have not been benchmarked. See
[installation and voice commands](SETUP.md#optional-capabilities).

## 23. Commit and pull request attribution

**Benefit:** Identify Nova-assisted contributions using standard Git metadata.

- Enable commit and pull request attribution by default with independent
  `attribution.commits` and `attribution.pullRequests` settings.
- Add a NovaCode `Co-Authored-By` trailer while preserving the human author and
  existing co-authors/sign-offs.
- Include the supplied NovaCode logo in a compact pull-request footer.
- Avoid duplicate supported trailers and identical footers.
- Apply live preferences to main/subagent instructions and supported structured
  Git/GitHub creation-tool payloads.

```json
{
  "attribution": {
    "commits": false,
    "pullRequests": false
  }
}
```

Merge that block into `~/.nova/Nova.config.json` to opt out. Git commit messages
are plain text; their trailers cannot embed the logo or force a GitHub avatar.
The default co-author is an unlinked product identity; an optional configured
name/email can credit an existing GitHub account with an associated email.
Attribution credits a commit, not exact AI-written lines. Arbitrary shell commands follow agent
instructions rather than being rewritten. [Attribution reference](docs/attribution.md).

## 24. Updates and cross-platform reliability

**Benefit:** Keep the installed tool current while retaining your work.

- Use `nova update` and `/update` with stage progress, release notes and a
  completion screen.
- Use the supported update/restart flow to reopen the main session and continue
  an interrupted turn; failed updates retain a path back to the current version.
- Handle Windows launcher-in-use updates without asking the running executable
  to overwrite itself directly.
- Verify matching package, lockfile and changelog versions in CI.
- Run maintained regression jobs on Windows and Linux, plus a Windows wheel
  install/reinstall and updater job.
- Exercise native search, portable path behavior, daemon operations and focused
  security/tool boundaries in regression checks.

Do not infer that every historical test, OS configuration or provider has been
certified from a focused or maintained CI suite. Advertise the actual matrix
and tested capabilities rather than “all platforms, all tests, guaranteed.”

## 25. Nova's own computer — preview

**Benefit:** Let Nova operate a separate Linux desktop while you watch its activity.

- Start a session-owned **local QEMU VM**, explicitly selecting Linux with
  2 CPUs and 4 GiB RAM; no cloud account or hosting fees are required.
- Use `/computer start` to launch it and open a private loopback browser preview.
  The view refreshes up to once per second while its tab is visible.
- Pause/resume agent input or stop/delete the guest from the preview or TUI.
  User pause remains effective with auto-approve enabled.
- Let the local root agent observe screenshots and click, drag, type, send key
  chords or scroll. Actions require the latest observation ID and return a
  new screenshot; they follow tool approval and plan-mode restrictions.
- Keep guest ownership separate across sessions. Delegated agents do not inherit
  desktop tools. No host project files, clipboard, browser profiles or
  credentials are mounted or copied by this feature.

Requires the optional `computer` extra (`cua==0.9.0`), installed QEMU, disk space
for the first guest-image download, and enough RAM for the guest and host apps.
Select a vision-capable model for visual interaction. Normal shell/file tools
keep their existing workspace; starting a guest does not move them into it.
The preview is local and token-protected, not publicly shared. Ordinary guest
networking can reach the Internet and local services. Stop deletes guest files;
normal exit attempts cleanup, while a killed process can leave a guest behind.

**Verification status:** offline lifecycle, tools, approval, preview and TUI tests
pass. The working-tree Windows WHPX workaround also passed a real Linux desktop
boot, screenshot, pause/resume and stop on the development PC, reaching readiness
in about 40 seconds with the image already cached. This is one host check, not
a universal startup benchmark or proof of complete host/network isolation.
[Setup and boundaries](COMPUTER.md).

## Launch/demo command shortlist

| Command or action | What to demonstrate |
| --- | --- |
| `/model` → Vision | Separate main-model and vision choices |
| `/router` | Task criteria and per-route reasoning effort |
| `@<known-agent-name>` | Resolve a registered specialist |
| `/council` | Parallel discussion, ballots and chairman synthesis |
| `/cowork` | Chat with folder grants, tasks and activity |
| `/create` | Local skill/agent editing |
| `/artifacts` | Searchable multi-artifact collection |
| `/share artifacts` | Temporary Cloudflare artifact URL |
| `/caveman full` | Enable the selected intensity |
| `/caveman settings` | Six switches and saved intensity |
| `/caveman audit` | Estimates versus reported token usage |
| `/tabs`, `/resume`, `/tree` | Parallel work, saved sessions and branches |
| Ask Nova to use `SendMessage` | Explicit coordination between live tabs |
| Ask Nova to enter/exit a worktree | Move the current local session in and back |
| `/mcp` | External tool discovery and integrations |
| Composer Code Mode toggle | Eligible native and MCP workflows |
| `/voice settings`, `/voice test` | Selected input/output providers and short spoken prose |
| `/voice download` | Real STT/TTS download progress, with an uncached model |
| `/theme` | Distinct homes, character art and optional animations |
| `/computer start` | Preview; demonstrate only after verifying a real local guest |
| Ask Nova to discover `MCPCode` | Filter or combine MCP results in JavaScript |
| `/trace setup` | Persistent tracing configuration |
| `/dream`, `/learning` | Memory maintenance and reviewed learning |
| `/tasks`, `/cron` | Background work and scheduled tasks |
| `/remote` | Authorized Telegram, Discord or Rootapp setup |

## Editorial rules for the landing-page update

1. **Lead with shipped user benefits.** Keep tool/class names for expanded
   details or documentation links.
2. **Label experimental, preview and optional capabilities.** The local VM is
   a preview; the Windows WHPX boot check covers one development PC. Teams/swarm are not on by
   default; vision, remotes, browser, voice and hosted integrations have setup
   and service requirements.
3. **Keep public artifact hosting distinct from local web apps.** Do not restore
   `/share council`, `/share cowork`, `/share create`, the removed combined
   dashboard, permanent tunnel promises or a silent artifact ngrok fallback.
4. **Use measured claims carefully.** No borrowed Caveman percentages, fabricated
   confidence meters, universal model-quality claims or unsupported cost savings.
5. **Keep security language specific.** Guardrails and validation are layers of
   protection; schema correctness, approval and task correctness are different.
6. **Avoid listing removed native tools.** GitHub trending, Hacker News, LinkedIn
   jobs, Reddit posts and `package_info` were removed from the built-in inventory.
   External MCP/browser workflows are separate capabilities.
7. **Do not imply permanent or private hosting.** Temporary artifact links are
   public to link holders and require the local Nova process/viewer to run.
8. **Do not claim line-level AI provenance or a GitHub bot avatar.** Git trailers
   and a PR logo footer provide the actual implemented attribution.

## Tool names for technical feature lists

Keep these exact names in developer-facing references; use benefit-led names in
the main landing-page cards.

| Tool or alias | Capability |
| --- | --- |
| `SendMessage` | Explicit messages between connected live tabs |
| `EnterWorktreeTool`, `ExitWorktreeTool` | Move the current local agent into a Git worktree and back |
| `NotebookEditTool` | Insert, replace and delete notebook cells without executing them |
| `GlobTool`, `GrepTool` | Imported-definition aliases for `glob` and `grep` |
| `MCPTool` | Generic server-scoped discovery, schema inspection and execution |
| `MCPCode` | Bounded JavaScript workflows over eligible native and MCP tools |
| `computer_status`, `computer_screenshot`, `computer_action` | Preview session-owned Linux desktop observation and input; user starts the guest |
| `tool_search_tool_regex_20251119` | Local regex-based schema discovery |
| `tool_search_tool_bm25_20251119` | Local BM25 natural-language schema discovery |
| `CronCreateTool`, `CronDeleteTool`, `CronListTool` | Granular scheduled-task management |
| `CtxInspectTool` | Feature-gated context diagnostics |
| `TerminalCaptureTool` | Feature-gated capture of retained Nova task-panel text |
| `VISION` | Independent on-demand image inspection for text-only chat models |
| `daemon` | Tracked detached processes, status, log tails and termination |
| `caveman` | Read-only source recovery, saved script lookup and usage audit |

## Existing brand assets

- [Nova banner](assets/Nova.png): existing wide brand artwork.
- [NovaCode attribution logo](assets/nova-attribution.png): the supplied square
  character image used in the PR attribution footer.
- [Classic terminal screenshot](assets/Preview-classic.png): an existing product
  screenshot; capture fresh screenshots for the new Caveman controls and other
  recently changed interfaces.

Keep the NovaCode identity. Use real UI captures for demos rather than invented
agent results, confidence meters, hardware counters or token-saving figures.

## Release/source map for maintainers

| Capability group | Main release notes |
| --- | --- |
| Routing, settings, cache visibility, sign-in and progressive skills | v1.0.0–v1.0.21 |
| Remote topics, command panels, sessions, context and background work | v1.0.18–v1.0.40 |
| UI styles, plan execution, updates/reviews, memory and integrations | v1.0.41–v1.0.50 |
| Providers, history tree, experimental teams and dynamic definitions | v1.0.51–v1.0.53 |
| Essential messaging/worktrees and async compatibility | v1.0.54–v1.0.55 |
| MCPTool, cron, inspection, search variants, strict tools and tool trimming | v1.0.56 |
| Telegram formatting, CI, notebooks and native file search | v1.0.57–v1.0.58 |
| Artifact restore/sharing, then Cloudflare-only artifact hosting | v1.0.59–v1.0.66; later releases supersede earlier sharing behavior |
| Council/Cowork redesign and web consensus pipeline | v1.0.67–v1.0.72 |
| Team fixes, bounded retries, inline agent statuses and workspace safety | v1.0.74–v1.0.80 |
| Packaged prompt consistency and persistent LangSmith tracing | v1.0.79–v1.0.84 |
| Generalized vision, VISION side-calls, remotes and portability | v1.0.85–v1.0.89 |
| Prompt-injection protections | v1.0.90 |
| Caveman features, skill alignment and token accounting | v1.0.91, v1.0.94 |
| MCP Code Mode | v1.0.92 |
| Default-on commit/PR attribution and logo | v1.0.93 |
| Configurable GitHub-linked attribution identity | v1.0.95 |
| Reply-language preservation at Wenyan intensities | v1.0.96 |
| Native Code Mode, composer toggle, Whistle and voice filtering | v1.0.97 |
| Voice download progress, earlier speech, Kokoro and sanoTTS | v1.0.98 |
| Additional interface styles, character art and animated homes | v1.0.99 |
| Local QEMU computer and private live preview | Preview in v1.0.99; [computer guide](COMPUTER.md) |

Full historical records are in [changelog/](changelog/). When an older note
conflicts with the current implementation or a later release, use the later
behavior. Review this file again when preparing a future landing-page release.
