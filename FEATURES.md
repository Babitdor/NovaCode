# NovaCode features

Product messaging reference for the landing page, verified against the source at
v1.0.52. The features below are distinctive parts of Nova's workflow; this is not
a claim that no other product offers them. Suggested copy describes capabilities,
not benchmark results or guaranteed outcomes.

## Positioning

**A terminal coding assistant with memory you can explore and workflows you can control.**

Nova combines code editing, linked persistent memory, reusable specialist agents,
multi-agent planning and parallel project sessions. Choose cloud or local models,
approve changes, and return to saved work without leaving the terminal.

## Lead with these features

### 1. Memory you can explore

**Suggested copy:** Keep project lessons in a wiki your agent can follow.

Nova stores preferences and topic notes in readable Markdown. A compact home
index routes to topic maps and related pages, with project-specific lessons kept
scoped. The agent can search for relevant notes and open their full sources.

`/dream` prepares backups and navigation, then consolidates overlapping notes.
Maintenance checks local links and preserves topic bodies. In the TUI,
`/dream status` shows consolidation gates and `/dream auto on|off` controls
automatic maintenance. Automatic runs require the time and session gates
(currently 24 hours and five sessions) and an available ownership lock. A
restricted background agent works under the memory route.

A new install starts with its memory scaffold; the wiki develops around
accumulated notes. Consolidation can use the configured model and incur usage.
Navigation maintenance does not establish whether a remembered claim is true.

**Show:** Home index → topic map → lesson → related page; then `/dream` results.

**Sources:** [Wiki engine](novacode_cli/memory/wiki.py),
[dream workflow](novacode_cli/commands/dream_handler.py),
[automatic gates](novacode_cli/memory/dream_gates.py),
[background dream agent](novacode_cli/agents/dream_agent.py),
[memory guidance](novacode_cli/prompts/longterm_memory.jinja).

### 2. Learning with a review trail

**Suggested copy:** Turn useful experience into knowledge you can review.

Hermes can review completed work and prepare lessons, habits, skills and prompt
proposals. Generated knowledge has a versioned local ledger with scope, evidence
and status. Approval and activation are separate steps; generated proposals are
not automatically treated as user instructions. `/learning` exposes controls,
review and optional automatic approval.

**Availability:** Configurable learning system. Reviews and other auxiliary model
work can incur provider usage. Do not describe this as guaranteed improvement or
unrestricted self-modification.

**Show:** A proposed lesson with its evidence, followed by its review decision.

**Sources:** [Learning controls](novacode_cli/commands/learning_handler.py),
[versioned ledger](novacode_cli/hermes/ledger.py),
[activation](novacode_cli/hermes/activation.py).

### 3. A council before the code changes

**Suggested copy:** Compare plans before you commit to an approach.

`/council <task>` runs independent proposals through critique, anonymous ranked
voting and a judging stage. It presents up to three plans for approval. Council
participants have no bound tools, so they cannot directly edit the repository.
Implementation handoff requires a separate approval step.

`/council settings` edits each member's name, system prompt and provider/model,
plus the planning judge. Choose a personality template or write your own.
Members inherit the session model unless overridden; saved settings apply to
future planning councils and web debates. [Council settings guide](docs/COUNCIL-SETTINGS.md).

**Availability:** Uses multiple model calls. Diverse personas are not necessarily
different models; voting does not prove that the winning plan is correct.

**Show:** Proposals → anonymous critique → ranked choices → approved handoff.

**Source:** [Planning council](novacode_cli/council_planner.py).

### 4. Project agents that can talk to each other

**Suggested copy:** Keep projects separate. Share the context that matters.

Run parallel session tabs and address another live tab with
`@tab:<session-id> <message>`. Requests queue while the recipient is busy. Linked
replies appear in both transcripts, with controls to open the other tab or attach
the reply to your draft. Attachment does not submit the draft.

**Availability:** Messaging currently connects tabs within one Nova window.
Each recipient retains its project and approval boundaries. Only the explicit
message is shared; pending messages are held in memory.

**Show:** An API-project agent answering a question from a frontend-project tab.

**Sources:** [Session messaging](novacode_cli/sessions/messaging.py),
[tab interaction](novacode_cli/sessions/tui_messaging.py).

### 5. Your model choices, across the workflow

**Suggested copy:** Choose the model for the job, including delegated work.

Nova supports hosted providers and local Ollama models. `/model` configures the
main agent and delegated model roles. Optional `/router` profiles select among
configured routes per turn, using a decision model. Routing falls back to the
default route when its decision path fails.

**Availability:** Routing is off by default and requires configuration. Model
capabilities, credentials and provider charges still apply. Local inference is
available; selecting it does not make every external integration offline.

**Show:** Model-role settings and an optional routing profile.

**Sources:** [Provider construction](novacode_cli/config/model_create.py),
[role and route settings](novacode_cli/config/nova_config.py),
[model router](novacode_cli/agents/model_router.py).

### 6. Plans you can find again

**Suggested copy:** Keep approved plans close to their projects.

Read-only planning separates investigation from implementation. User-approved
plans are saved in `<project>/.nova/plans/` and mirrored to
`~/.nova/plans/<project-slug>/`. Automatically approved plans are stored only in
the global archive. The agent's `recent_plans` tool searches both locations for
the current project and reads the saved Markdown.

**Also available:** `/claude-plan` and `/codex-plan` let an installed, authenticated
Claude Code or Codex CLI prepare a plan for Nova to implement after approval.
These integrations have their own installation and account requirements.

**Show:** Approve a plan, then ask Nova to find the recent plan in a later session.

**Sources:** [Plan persistence](novacode_cli/tools/plan_mode_tools.py),
[plan discovery](novacode_cli/tools/plan_archive_tools.py),
[Claude planner](novacode_cli/commands/claude_plan.py),
[Codex planner](novacode_cli/commands/codex_plan.py).

### 7. Specialists you can keep and edit

**Suggested copy:** Give recurring work a specialist of its own.

Define an agent in readable Markdown with its instructions, description and
optional tool selection. Store it in `<project>/.nova/agents/<name>.md` for one
project or `~/.nova/agents/<name>.md` for use across projects. Project definitions
override global namesakes. Nova ships 14 editable specialist definitions, seeded
once without replacing existing user definitions.

In the main TUI, `@reviewer review this diff` asks Nova to delegate to that agent;
multiple mentions let the main agent coordinate specialists. An ordinary agent
runs synchronously, `async: true` offers both synchronous and background forms,
and `async: only` selects background execution. Root TUI sessions and session
workers reload changed definitions before the next turn while preserving the
model, checkpoint, workspace and policy settings.

**Availability:** Background execution needs the optional agent server for the
current workspace. External servers manage their own graph registry. Explicit
mention preprocessing currently belongs to the main TUI. Creating a saved agent
definition is separate from spawning a running teammate.

**Show:** Create `reviewer.md`, invoke `@reviewer`, then demonstrate a project
override without changing the global definition.

**Sources:** [Invocation guide](docs/AGENT-INVOCATION.md),
[definition format and tool selection](novacode_cli/agents/agent_file.py),
[shipped specialists](novacode_cli/agents/default_subagents/builtin/).

### 8. Context you can inspect and recover

**Suggested copy:** See what fills the context window—and recover the detail.

`/context` shows message categories, tool-schema contributions and the current
compaction strategy. Accounting reflects the selected tool schemas, including
deferred tools and MCP attribution, rather than treating every registered tool
as sent on every request. Estimated shares remain separate from provider usage.

Before automatic model compaction, Nova can snip stale or redundant observations
and collapse older completed tool loops. Larger collapsed results are saved
under the recoverable `/cleared/` route. These are request-local views: saved
history and the current turn remain intact.

**Availability:** Context reduction does not prove better reasoning or perfect
recall. Do not attach a token-savings percentage or speed claim without a measured
corpus and stated boundaries.

**Show:** Inspect `/context`, open an offloaded result, then browse the original
session history.

**Sources:** [Context strategies](novacode_cli/context/strategies.py),
[context accounting](novacode_cli/context/_analysis.py),
[compaction middleware](novacode_cli/agents/context_compaction.py).

## Experimental: coordinated agent teams

**Suggested copy:** Give a team a shared task board and separate workspaces.

Opt-in teams add persistent teammates, scoped inboxes and a durable task board.
The lead can choose in-process execution, a fresh Python process, an isolated
Git worktree or a configured Nova container worker. Task claims are atomic and
versioned; workers report results and evidence for lead review. Tool approvals
are checked against the current parent policy and the worker's capability
snapshot. Separately gated swarm scheduling assigns ready tasks to isolated
workers without model polling.

**Availability:** `agent_teams_enabled` and `swarm_enabled` default to false.
Container execution needs a trusted, digest-pinned worker image and remains
unverified against a live daemon/provider. Restart supports cleanup reconciliation,
not resumable execution. Shared file-read caching is deferred; integration and
merges remain the lead's responsibility. Scripted tests establish context and
coordination contracts, not real-model task quality.

**Show:** An explicitly enabled team with a task dependency, a worktree worker,
a peer message and evidence awaiting lead review.

**Sources:** [Team guide](docs/AGENT-TEAMS.md),
[verification and limits](docs/AGENT-TEAMS-EVIDENCE.md),
[coordinator](novacode_cli/teams/coordinator.py).

## Supporting capabilities

| Capability                     | User-facing value                                                                                          | Availability and limits                                                                                                         | Source                                                                                         |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Recoverable context            | Inspect context, compact conversations and browse recovery archives                                        | Archives use checksummed manifests; compaction is not a guarantee of perfect model recall                                       | [Context recovery](novacode_cli/context/archives.py), [compaction](novacode_cli/compaction.py) |
| Resume saved work              | Search saved sessions and inspect their project, model and continuation details                            | Restoration depends on the saved record and available provider configuration                                                    | [Resume browser](novacode_cli/tui/resume_picker.py)                                            |
| Branchable session history     | Browse the conversation tree, bookmark entries and continue from an earlier point with `/tree` or `/rewind` | Conversation branches do not automatically undo filesystem changes | [History tree](novacode_cli/session/history_tree.py), [tree browser](novacode_cli/tui/history_tree_screen.py) |
| Export and share               | Export a self-contained HTML session with `/export`; optionally share through `/share` | Sharing requires confirmation and GitHub authentication; recognized token patterns are masked, but review content before publication | [Session export](novacode_cli/session/history_export.py) |
| Continue from other assistants | Import supported local Claude, Codex or Nova conversations and use them as context                         | Requires accessible local session records; imported history does not recreate another assistant's running tools or hidden state | [Import browser](novacode_cli/tui/session_import.py), [CLI options](novacode_cli/cli_args.py)  |
| Bounded goal mode              | Continue toward an objective across agent turns, with pause, resume and a turn budget                      | Requires an explicit completion declaration with evidence; a turn budget is not a spend limit or independent proof of success   | [Goal loop](novacode_cli/core/autonomous_loop.py)                                              |
| Background specialists         | Delegate focused jobs while continuing local work                                                          | Async server requires the `agents-server` extra or a compatible configured server; in-process delegation is also available      | [Server lifecycle](novacode_cli/agents/server_launcher.py)                                     |
| Adaptable terminal interface   | Choose Dashboard, Classic, Centered, Banner or Minimal layouts, plus color palettes                        | Composer and component presentation follow the selected design; terminal size affects layout                                    | [Home layouts](novacode_cli/tui/home.py), [TUI](novacode_cli/tui/app.py)                       |
| A status line you choose       | Use `/statusline` to select model, effort, branch, context, permissions, background activity and other fields | Available fields depend on current session state; reasoning controls remain provider-dependent | [Status-line fields](novacode_cli/tui/statusline.py) |
| Visible approval mode          | Toggle `/auto-approve` with composer styling that makes the active mode visible | Broader approval is an explicit choice; configured deny rules remain in force. OS confinement requires a configured sandbox | [TUI permission controls](novacode_cli/tui/app.py) |
| Animated terminal companion    | Toggle `/nova` for a small avatar that reacts to agent activity | Character-cell rendering quality depends on terminal glyph support | [Avatar](novacode_cli/tui/avatar.py) |
| Local voice                    | Dictate prompts and hear spoken replies                                                                    | Optional `voice` extra, audio hardware and model downloads; cloud speech providers are separate choices                         | [Voice package](novacode_cli/audio/)                                                           |
| Images in the conversation     | Use screenshots and pasted images as task context                                                          | A capable main model can view images directly; text-only configurations can use an auxiliary vision model                       | [Vision routing](novacode_cli/bootstrap/vision_router.py)                                      |
| Skills, MCP and plugins        | Add reusable workflows, external tools and Python extensions                                               | Some integrations require extra runtimes, services or credentials                                                               | [Skills](novacode_cli/skills/), [MCP](novacode_cli/mcp/), [plugins](plugins/README.md)         |
| File recovery                  | Browse and restore snapshots from supported file operations                                                | Coverage depends on the operation; large files can be skipped. It does not replace version control or backups                   | [Recovery](novacode_cli/recovery.py)                                                           |
| Headless automation            | Run one-shot tasks with text, JSON or streamed JSON output; integrate through a persistent JSON-lines pipe | Approval policies remain active. Unattended approval is explicit; `--deny-tools` is not a read-only sandbox                     | [One-shot runner](novacode_cli/headless/runner.py), [pipe mode](novacode_cli/headless/pipe.py) |
| Remote and scheduled work      | Use Discord/Telegram bridges, cron tasks or signed webhook events                                          | Requires integration configuration; scheduling depends on the Nova process/service being available                              | [Remote integrations](novacode_cli/remote/)                                                    |

## Suggested landing-page order

1. **Hero:** positioning line, terminal preview and an installation call to action.
2. **Memory:** linked navigation and `/dream`, with a concrete remembered lesson.
3. **Planning:** council choices, user approval and the saved plan.
4. **Specialists and parallel work:** an editable agent definition and an explicit
   exchange between two project tabs.
5. **Model choice:** hosted/local options and delegated roles.
6. **Trust and control:** inspectable context, reviewable learning, approval
   boundaries and recovery.
7. **Capability strip:** session trees, voice, images, skills, MCP and automation,
   with optional labels.
8. **Experimental section:** opt-in teams and swarm, visibly separated from
   default capabilities and accompanied by their current limits.

Keep source references and operational detail in this document rather than in the
landing-page copy. Avoid unmeasured speed, accuracy or token-savings claims,
blanket "fully offline" promises, and claims that snapshots cover every shell
operation. These are implementation-backed capabilities, not a comparative
market survey or a live-model evaluation.
