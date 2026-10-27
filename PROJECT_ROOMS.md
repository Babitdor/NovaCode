# Project rooms

Open **Rooms**, `/rooms`, or **Ctrl+Shift+R** in Nova's TUI. Existing session tabs
remain available through **Sessions** or Escape. Leaving rooms pauses their work;
closing Nova stops its foreground workers. Opening a room never invokes a model.

The sidebar starts with `#general` and separate Nova, Claude and Codex DMs. Enter
a lowercase name in **+ channel name** and press Enter to create a channel.
**Channels** collapses the sidebar; narrow terminals collapse it automatically.
Each room retains its draft and unread position. Scroll freely; Ctrl+End jumps
to the latest message.

In channels, `@claude`, `@codex` and `@nova` select recipients. Unmentioned messages
go to Nova. DMs always address their named participant. Mention suggestions label
agents and files separately; `@app.py` remains a file reference. CLI participants
read files using their native tools. Nova has project-relative file tools.

Claude and Codex run the installed `claude` and `codex` commands with their own
logins. Nova does not install them or silently fall back to API billing. Run
`claude` or `codex login` in a terminal to finish setup. Each participant keeps a
separate resumable conversation per channel/DM and task; review conversations
are separate again. Other participants receive shared room messages, published
notes and explicit handoffs, never the CLI's private reasoning/tool history.

## Approving work

Discussion has read-only tools. To implement:

1. Select the channel or DM, then open **Shared board**.
2. Enter the goal and comma-separated participants; choose **Review task scope**.
3. Review the task card's participants, read/edit/test operations, base revision,
   worktree, warnings and turn allowance. Choose **Start** to authorize execution.

Tasks require a Git repository with an initial commit. The worktree branches from
HEAD; uncommitted project files are never copied or stashed. Discussion and notes
also work outside Git. Each task retains its own worktree through handoffs,
cancellation, restart and review. Turns in one task serialize access to that tree.
At most two turns run across the canonical project, including other Nova sessions.
The initial task allowance is six turns; handoffs and reviews consume it. An
exhausted handoff stays paused with its evidence saved. **Extend +6** requires an
owner decision and keeps the same scope. Broader scope needs a new task card.

Native CLI controls remain in force. Codex uses its read-only or workspace-write
sandbox with approval escalation disabled in the headless stream. Claude keeps
its default permission mode and existing native write/test grants; Nova does not
pre-approve writes or shell commands. A new native permission request blocks the
turn. Review the blocker, configure the permission through the CLI, then explicitly
resume. Nova task tools allow project-relative edits and Python pytest checks;
other execution capabilities are outside this first room implementation.
Worktrees isolate files, **not security boundaries**. No permission bypass flags
are enabled. Agents cannot approve tasks, expand membership, extend allowances,
or approve integration through the room bridge.

**View diff** includes tracked and new files. **Claude review** runs a read-only
review when Claude is an approved participant. **Integrate** asks for a separate
owner confirmation, commits the task worktree and fast-forwards the canonical
checkout. Integration refuses a dirty checkout, a changed base, or pending task
workers. Diverged bases require manual reconciliation. No worktree is removed.

## Shared memory and recovery

Canonical projects store room history, authorship, tasks, native session IDs,
drafts, read positions and notes in `.nova/workspace/rooms.sqlite3`. Linked Git
worktrees use that same database. Nova adds `/.nova/workspace/` to the project's
local Git exclude file; it does not change tracked project files for storage.
Nova's private checkpoints and immutable text artifacts also stay in that ignored
directory. Keep native CLI session storage to retain resumability.

The local MCP bridge exposes the running participant's room transcript and the
published project board, plus findings/decisions/artifacts and structured task
handoffs. Other DMs stay private even when the same agent reads from a channel. Authorship,
room and task access come from a per-execution capability, not model arguments.
Handoffs record the goal, completed work, files, checks, open questions and next
owner, within the approved participant list. Artifacts are content-addressed and
verified when read. Shared context is bounded and does not confer authority.

The owner can inspect expandable turn details and provider-reported usage on the
board. Missing usage remains **unknown**; turn allowances are enforced, while a
universal dollar cap is not provided. Select individual channel notes and a new
destination path to **Export Markdown**. Existing files are never overwritten;
DM notes are excluded from export.

Interrupted or cancelled jobs remain paused. Restart recovery pauses jobs from
dead Nova processes without replaying them or disturbing another live session.
Inspect the preserved worktree before **Resume**: uncertain actions might already
have completed. Resuming consumes another task turn.

## Verification

Offline acceptance checks use fake CLI streams, a fake Nova model through the
shared loop, temporary Git repositories, TCP bridge requests and Textual pilots:

```powershell
uv run pytest tests/test_project_rooms.py tests/test_tui_app.py::test_tui_headless_run tests/test_tui_app.py::test_tui_saves_on_quit tests/test_tui_app.py::test_tui_sessions_screen tests/test_claude_plan.py tests/test_codex_plan.py tests/test_team_worktree.py tests/test_agent_mentions.py tests/test_file_mentions.py tests/test_tab_mentions.py tests/test_deepagents_compat.py -q
uv run ruff check novacode_cli/rooms novacode_cli/tui/rooms.py tests/test_project_rooms.py
uv run ruff format novacode_cli/rooms novacode_cli/tui/rooms.py tests/test_project_rooms.py --check
uv run mypy novacode_cli/rooms novacode_cli/tui/rooms.py --follow-imports=silent
```

Opt-in live smoke test, using a disposable Git repository and existing logins:

1. Run Nova there; open rooms. Confirm navigation alone starts no CLI.
2. Ask `@claude summarize this project without editing`, then follow up. Restart
   Nova and follow up again; confirm the native conversation resumes.
3. Repeat with Codex and in each DM; verify private DM content is absent elsewhere.
4. Create a tiny task with Codex and Claude. Review its base/worktree, Start, then
   ask for a structured handoff and Claude review. Verify the same worktree and
   remaining allowance. Exercise Cancel, restart, Resume and a denied permission.
5. Inspect View diff, then Integrate separately. Verify dirty/base refusals first.
   Confirm original working files and the task worktree remain intact.

Live smoke tests incur provider usage and are not part of offline tests.
