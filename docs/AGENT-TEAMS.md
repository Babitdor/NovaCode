# Agent teams

Agent teams are an experimental, opt-in addition to Nova's existing `task()`
delegation. They add persistent teammates, a shared task board and scoped inboxes.
The existing delegation tools continue to work.

## Enable

Merge these keys into your existing `~/.nova/Nova.config.json` and restart Nova:

```json
{
  "agent_teams_enabled": true,
  "swarm_enabled": false
}
```

Do not replace your other settings. Both flags default to false. Disabled teams
add no team tools or automatic model calls. Sandboxed or externally customized
root builds currently leave teams disabled rather than assume a compatible
execution policy.

Ask Nova, for example:

> Create a team to investigate this failure. Spawn a reviewer in default mode
> and an implementer in worktree mode. Give both the constraints and required
> checks. Review their evidence before integrating any changes.

The lead uses `team_create`, `spawn_agent`, `list_agents`, `get_agent` and
`cancel_agent`. `send_message`, `read_messages` and `ack_message` coordinate
members. `task_create`, `task_list`, `task_get`, `task_claim` and `task_update`
manage the board. `team_delete` stops owned workers and archives the team.
These are model tools, not new slash commands. Sender identity is host-bound.

## Choose an execution mode

| Mode | Conversation | Files and execution |
| --- | --- | --- |
| `default` | Private copy of the completed parent conversation | Async teammate in the same process and workspace |
| `fork` | Fresh conversation with explicit task handoff | New Python process in the same workspace, including on Windows |
| `worktree` | Fresh conversation with explicit task handoff | New Python process in a separate committed Git worktree |
| `remote` | Fresh conversation with explicit task handoff | Nova worker in an owned container with an isolated Git worktree |

Default mode preserves corrections and complete tool/result groups, excludes an
unfinished parent turn and refuses protected-context overflow. It has a private
checkpoint; changes to its messages do not mutate the parent's conversation.
Shared-workspace modes do not prevent file conflicts. Use isolated modes for
concurrent edits. No cross-process file-read cache is enabled.

Worktree and remote modes require Git and a committed repository. Remote also
requires the workspace to be the repository root. Unavailable isolation produces
an error; it never falls back to editing shared files. Dirty worktrees and
worktrees with commits ahead of the source repository are preserved. The lead
reviews and integrates changes; there is no automatic merge or push.

## Tasks, messages and swarm

The lead creates tasks with constraints, expected evidence and dependencies.
Claims are atomic and versioned. Only the current owner with a live lease can
complete a task. Stale or uncertain execution is blocked for lead review rather
than replayed. Completion requires a nonempty result and evidence.

Messages use stable request IDs for deduplication. Peer text is untrusted data,
delivered at safe model boundaries without racing a running tool. Read receipt
and task completion are separate operations. Idle teammates wake on new work;
the model does not need to poll them repeatedly.

For swarm scheduling, also set `swarm_enabled` to true and ask the lead to create
a team with `swarm=true`. Swarm accepts only `worktree` and `remote` workers.
The host assigns ready tasks and renews their leases. Waiting workers make no
startup model call. A worker's final report records completion evidence with
`lead_review_required`; this is not independent verification that its code works.
Ordinary teams do not claim tasks automatically.

## Permissions and lifecycle

Workers retain their configured role model and a snapshot of their tool
capabilities. Every ordinary tool call is checked against that snapshot and the
current parent policy. Explicit denies survive auto-approve. Explicit asks,
including reads, require a parent decision bound to the exact worker, tool,
arguments and call ID; one approval cannot authorize a second execution.
Unsupported approval delivery fails closed. Workers ask the lead to delegate
further work; nested legacy `task`, `eval` and `async_task` are refused.

Cancellation targets the selected agent and its owned descendants. Closing a
view does not grant ownership of another root's workers. Team shutdown retains
uncertain workers and capacity reservations until termination can be verified.
An old PID alone is insufficient proof of ownership: recovery checks process
birth time and a launch nonce, or container ownership labels.

Reopening a saved root currently provides **cleanup-only reconciliation**.
Old execution is marked disconnected; the implementation does not reconnect a
model stream, restore in-flight approvals or resume a lost task automatically.
Inspect the task, preserved files and evidence before creating replacement work.

## Storage and limits

Coordination records live under
`~/.nova/sessions/<sha256(project_id + NUL + root_id)[:32]>/teams.sqlite3`.
Records are scoped to project, root and team. Existing histories need no migration.
Large final text has a bounded preview and an immutable local artifact; `get_agent`
returns its path, SHA-256 and byte count. Final-text artifacts have a 16 MiB limit
and a 64 MiB per-root budget. Retention failure is reported instead of silently
claiming the full output was saved. Container temporary tool artifacts are not
yet fully exported; persist deliverables in the mounted worktree.

Current host limits include four external workers shared with session tabs,
eight live team agents per root, 256 retained tasks and messages per team,
32 unread messages per recipient, 16 KiB per message and 256 turns per agent.
Acknowledging messages frees unread capacity, not the retained-message ceiling.
Start a fresh session when retained records reach their bound. Unread work is
not silently evicted. Usage follows the existing UsageTree accounting; unknown
prices remain unknown, and cached-token subsets are not added to input twice.

## Container adapter

This adapter has offline protocol and control tests, but no live Docker startup
has been verified. Supply your own trusted image containing this compatible
Nova version, Python and Git, pinned by its actual SHA-256 digest. Preload it in
Docker's default local context; Nova does not pull or build images automatically.
Merge a `team_container` object into the same configuration:

```json
{
  "team_container": {
    "image": "your-worker-image@sha256:<actual-64-character-lowercase-digest>",
    "network": "none",
    "user": "1000:1000",
    "memory": "1g",
    "cpus": 1,
    "readonly": false
  }
}
```

The placeholder digest must be replaced. Network may be `none` or `bridge`;
provider access generally requires `bridge` and an available selected-provider
credential. Local ChatGPT OAuth is unsupported in this transport. The image's
non-root user must be able to access the bound workspace.

The worker receives its worktree and independent Git metadata, selected provider
credentials and host-validated configuration. Host profiles, coordination DBs
and the Docker socket are not mounted. The container uses a read-only root,
private temporary filesystem, dropped capabilities and explicit resource bounds.
Failure to verify a stopped container retains its reservation and reports an
uncertain state. This transport supports local Docker, not arbitrary remote
Docker contexts or Claude Code Remote.

See [the architecture](AGENT-TEAMS-ARCHITECTURE.md) and
[verification evidence](AGENT-TEAMS-EVIDENCE.md) for implementation coverage and
the remaining rollout gates.
