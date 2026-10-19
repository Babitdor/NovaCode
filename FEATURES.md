# NovaCode features

Product messaging reference for the landing page, verified against the source at
v1.0.47. The features below are distinctive parts of Nova's workflow; this is not
a claim that no other product offers them. Suggested copy describes capabilities,
not benchmark results or guaranteed outcomes.

## Positioning

**A terminal coding assistant with memory you can explore and workflows you can control.**

Nova combines code editing, linked persistent memory, multi-agent planning and
parallel project sessions. Choose cloud or local models, approve changes, and
return to saved work without moving your workflow out of the terminal.

## Lead with these features

### 1. Memory you can explore

**Suggested copy:** Keep project lessons in a wiki your agent can follow.

Nova stores preferences and topic notes in readable Markdown. A compact home
index routes to topic maps and related pages, with project-specific lessons kept
scoped. The agent can search for relevant notes and open their full sources.

`/dream` prepares backups and navigation, then asks the agent to consolidate the
notes. Maintenance checks local links and preserves topic bodies. A new install
starts with its memory scaffold; the wiki develops around accumulated notes.
Navigation maintenance does not establish whether a remembered claim is true.

**Show:** Home index → topic map → lesson → related page; then `/dream` results.

**Sources:** [Wiki engine](novacode_cli/memory/wiki.py),
[dream workflow](novacode_cli/commands/dream_handler.py),
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

## Supporting capabilities

| Capability                     | User-facing value                                                                                          | Availability and limits                                                                                                         | Source                                                                                         |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Recoverable context            | Inspect context, compact conversations and browse recovery archives                                        | Archives use checksummed manifests; compaction is not a guarantee of perfect model recall                                       | [Context recovery](novacode_cli/context/archives.py), [compaction](novacode_cli/compaction.py) |
| Resume saved work              | Search saved sessions and inspect their project, model and continuation details                            | Restoration depends on the saved record and available provider configuration                                                    | [Resume browser](novacode_cli/tui/resume_picker.py)                                            |
| Continue from other assistants | Import supported local Claude, Codex or Nova conversations and use them as context                         | Requires accessible local session records; imported history does not recreate another assistant's running tools or hidden state | [Import browser](novacode_cli/tui/session_import.py), [CLI options](novacode_cli/cli_args.py)  |
| Bounded goal mode              | Continue toward an objective across agent turns, with pause, resume and a turn budget                      | Requires an explicit completion declaration with evidence; a turn budget is not a spend limit or independent proof of success   | [Goal loop](novacode_cli/core/autonomous_loop.py)                                              |
| Background specialists         | Delegate focused jobs while continuing local work                                                          | Async server requires the `agents-server` extra or a compatible configured server; in-process delegation is also available      | [Server lifecycle](novacode_cli/agents/server_launcher.py)                                     |
| Adaptable terminal interface   | Choose Dashboard, Classic, Centered, Banner or Minimal layouts, plus color palettes                        | Composer and component presentation follow the selected design; terminal size affects layout                                    | [Home layouts](novacode_cli/tui/home.py), [TUI](novacode_cli/tui/app.py)                       |
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
4. **Parallel work:** an explicit exchange between two project tabs.
5. **Model choice:** hosted/local options and delegated roles.
6. **Trust and control:** reviewable learning, approval boundaries and recovery.
7. **Capability strip:** voice, images, skills, MCP and automation, with optional labels.

Keep source references and operational detail in this document rather than in the
landing-page copy. Avoid unmeasured speed, accuracy or token-savings claims,
blanket "fully offline" promises, and claims that snapshots cover every shell
operation. These are implementation-backed capabilities, not a comparative
market survey or a live-model evaluation.
