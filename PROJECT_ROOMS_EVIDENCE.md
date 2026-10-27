# Project rooms verification

Implementation contract: [PROJECT_ROOMS_SPEC.md](PROJECT_ROOMS_SPEC.md).
Usage, architecture, permissions and reproducible check commands:
[PROJECT_ROOMS.md](PROJECT_ROOMS.md).

Base commit: `b667761567b6bccf9c4341c55311ee3e054267c9`.
Existing unrelated edits/deletions were preserved. No dependency or login
changes were needed. The subsequent owner-requested release bump is 1.1.1.

## Release 1.1.1 checks

- `uv run pytest tests/test_release_version.py tests/test_project_rooms.py -q --tb=short`:
  **33 passed**, one existing deprecation warning, **21.10 seconds**. This covers
  all **30 room cases** and the three release/version entrypoint checks.
- The release's 30 room cases also passed in randomized order with seed
  `27102026` (**19.30 seconds**); `uv run nova --version` reports **1.1.1**.
- Release validation reproduced stale autocomplete selecting an agent for newer
  file input. Suggestions now publish only for their matching input and room;
  stale selections are rejected. A new regression test reproduced the wrong
  replacement before the fix. Completion checks wait for the async result to
  publish while retaining their original assertions.
- Version 1.1.1 matches `_version.py`, `pyproject.toml` and `uv.lock`; offline
  lock resolution changed only the root package version. The release changelog
  is `changelog/CHANGELOG-v1.1.1.md`.
- Ruff passed; the ten checked Python files are formatted. Mypy passed for the
  eight checked source modules. The staged diff passed `git diff --cached --check`.

## Implementation checks before the release fix

- The focused regression command documented in PROJECT_ROOMS.md passed:
  **119 passed**, one existing LangSmith deprecation warning, **90.76 seconds**.
  It includes **29 room acceptance cases**, existing Claude/Codex planning,
  worktrees, agent/file/tab mentions and deepagents compatibility, plus three
  NovaApp smoke tests for execution, saving and session navigation.
- The room cases also passed in randomized order:
  `uv run pytest tests/test_project_rooms.py -q --random-order --random-order-seed=27102026 --tb=short`:
  **29 passed**, one deprecation warning, **25.70 seconds**.
- `uv run ruff check novacode_cli/rooms novacode_cli/tui/rooms.py tests/test_project_rooms.py`:
  **All checks passed**.
- `uv run ruff format novacode_cli/rooms novacode_cli/tui/rooms.py tests/test_project_rooms.py --check`:
  **9 files already formatted**.
- `uv run mypy novacode_cli/rooms novacode_cli/tui/rooms.py --follow-imports=silent --incremental`:
  **Success: no issues found in 8 source files**.
- `git diff --check -- novacode_cli/tui/app.py`: passed. The legacy app was not
  globally reformatted or lint-cleaned: whole-file Ruff reports 755 diagnostics
  in both HEAD and the current file. This task adds only the room command,
  shortcut, button and lazy screen launcher there.
- Rendered Textual desktop, board and 55-column views were exported as SVG and
  inspected through headless Chrome. Captures and JUnit output are local artifacts
  under `test-results/rooms-*` and `test-results/project-rooms-final.xml`.
  Automatic approval review rejected deleting the temporary browser profiles
  and `.tmp-rooms-preview.py`; these local preview artifacts were left in place.

Toolchain: Python 3.11.13, pytest 9.1.0, Ruff 0.15.17, mypy 2.1.0,
Textual 8.2.7, MCP 1.30.0, Hypothesis 6.169.0. Existing dependencies were reused.

## Acceptance coverage

`tests/test_project_rooms.py` exercises:

- Recipient deduplication, default Nova delivery, DM routing, emails and file
  mention compatibility; separate native session IDs and private DM context.
- Authored message persistence and canonical linked-worktree discovery; 60
  concurrent message writes and a project-wide two-worker claim limit.
- Owner approval before work, isolated worktrees, base/dirty warnings, separate
  integration, uncommitted/base-change refusal and preservation of worktrees.
  New-file diffs include Unicode filenames and contents without staging them.
- Six-turn structured handoffs, duplicate handoff refusal, preserved exhausted
  handoffs, explicit extension and restart recovery that leaves live runtimes alone.
- Cancellation during execution and between claim and worker creation; app
  shutdown pauses foreground jobs. Transient SQLite contention does not kill
  the scheduler or replay a CLI action.
- Native CLI argv and login environment isolation, resume IDs, usage parsing,
  denied approvals and recovery from ordinary failing checks. A real Python
  subprocess supplies fake JSON streams and large stderr output; cancellation
  kills and reaps it.
- Forged TCP capabilities, attempted owner operations and author/room spoofing.
  A real stdio MCP subprocess is initialized using the installed MCP SDK; its
  tool list contains only participant operations and publishes an attributed
  artifact through the live loopback coordinator.
- Project-wide published board access without cross-DM leakage; verified,
  room-authorized artifacts; bounded shared context; explicit channel-note
  Markdown export that refuses overwrites and DM notes.
- Nova discussion and implementation through the canonical agent loop, using a
  fake model and real private SQLite checkpoints. Discussion lacks edit/test
  tools. An approved task actually writes through Nova's file tool into its
  worktree while leaving the canonical project unchanged.
- Sidebar navigation, persistent drafts, unread indicators, mention suggestions,
  narrow terminals, task Start/review wiring and independent transcript scrolling.
- Twenty generated Unicode message/artifact persistence round trips using
  Hypothesis; CRLF artifact integrity; non-Git discussion with implementation refused.

The task-card UI test waits for an in-flight timer refresh before inspecting its
new card; its original approval, execution and worktree assertions remain intact.

## Broader checks and limits

The full suite was attempted with `uv run pytest tests/ -q --maxfail=5` and
terminated on the repository's Windows pytest timeout in
`test_chat_handler.py::test_decisions_are_bounded`. Before that it hit two
independently reproduced failures in unchanged async-agent code:

- `test_async_general_purpose_graph_keeps_workspace_and_approval_controls`:
  `NovaModelRetryMiddleware` missing from the asserted middleware set.
- `test_every_user_agent_marked_async_is_collected`: `code-simplifier`, `serena`
  and `ultra-mode-agent` not collected as async.

A broader TUI run also encountered the unchanged responsive-layout test double
missing `_apply_interface_style`, and timed out in
`test_tui_trace_log_plan_native`. The selected NovaApp smoke tests passed in the
final run. These attempts do not establish a passing full-suite baseline.

Live Claude Code and Codex login/permission/version smoke tests were **not run**;
the opt-in procedure is documented in PROJECT_ROOMS.md. Real provider usage and
all installed CLI version variants therefore remain unverified. Nova task test
execution currently exposes Python pytest rather than an arbitrary shell.

No changed-line coverage gate, mutation gate or independent security audit was
performed. The evidence above establishes focused behavioral coverage, not a
complete high-assurance certification or an OS security sandbox.
