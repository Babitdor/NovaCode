# Port the entire Textual TUI to the ratatui client

> **How to use this file.** `rust/LEDGER.md` is the per-module audit (66 TUI modules and 54
> command-handler modules, one state each). This file is the plan _and_ the remaining-work
> list: every step carries a status, what already landed, and tick boxes for what has not.
> When a slice lands, update the status here, the ledger row it closes, and re-run the
> evidence command named for it.

## Progress

| #   | Step                                              | State                                      | Evidence command                                                                      |
| --- | ------------------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------- |
| 1   | Server command engine + generated catalogue       | **done**                                   | `uv run python scripts/gen_tui_command_catalog.py --check`                            |
| 2   | Client command surface + generic screen renderer  | **done**                                   | `cargo test -p nova-tui --test screens`                                               |
| 3   | Wave 2 — command screens, pickers, reports        | **7 of 19 ported**                         | `uv run pytest tests/test_tui_settings_command.py tests/test_tui_preferences_command.py tests/test_tui_theme_command.py -q` |
| 4   | Wave 3 — sessions, tabs, background tasks         | **spawn + tabs + per-session state**                  | `cargo test -p nova-tui --test screens`                                               |
| 5   | Wave 4 — agent surface panels                     | **4 of 10 modules**                            | `uv run pytest tests/test_council_view.py -q`                                         |
| 6   | Wave 5 — visual identity, avatar, matrix          | **theme table + repaint done**             | `uv run python scripts/gen_theme_table.py --check`                                    |
| 7   | Wave 6 — shell, PTY, platform, providers          | **not started**                            | `uv run pytest tests/test_tui_rust_client.py -q`                                      |
| 8   | Remaining command handlers (bulk flip)            | **77 of 105 served**, no-Textual gate live | `uv run pytest tests/test_tui_no_textual_in_server.py -q`                             |
| 9   | Parity harness: corpora, gates, ledger automation | **6 of 7 corpora**                         | `uv run python scripts/verify_tui.py`                                                 |
| 10  | Default flip + release                            | **blocked by 3–9**                         | `uv run pytest tests/test_ledger_gate.py -q`                                          |

Command surface right now: **105 TUI table entries — 94 served** (42 through the shared
handler, 3 client actions, 20 port screens, 29 port commands) and **11 waiting**
(8 app-side behaviour, 0 modals, 3 background tasks). `make` is not available on this workstation: the `make
rust-test` / `make rust-frames` targets map to the cargo commands written out below.

## Approach (unchanged)

**Native port, but never re-implement a command.** Two seams carry almost all of the work:

1. **Server-side command engine (ADR-006, extended).** `ServerPort` runs the real
   `handle_command` under `console.capture()` and converts output to blocks. A _catalogue_
   (`novacode_cli/server/catalog.py`, generated from `TUI_COMMANDS` with `ast`, never
   importing Textual) classifies every table entry as `passthrough`, `screen` or `client`,
   so most commands need zero Rust code and the rest are visible in one table.
2. **A declarative screen protocol.** 32 `push_screen(` call sites and ~57 modal classes use
   a small vocabulary: list, table, detail, form, markdown, progress, confirm. One Rust
   renderer for that vocabulary plus pure Python builders (`novacode_cli/ui/screen_specs.py`)
   shared by the Textual screens and the socket port covers waves 2–4 without forking
   behaviour. The irreducible screens (council view, PTY terminal, avatar/matrix) stay bespoke.

Two rules hold throughout: the port never renders (it emits blocks/specs, per
`novacode_cli/ui/port.py`), and every unported path fails loudly (`NOT_PORTED_MESSAGE`).

**Rejected:** a server-rendered ANSI cell grid (contradicts ADR-003/ADR-006, no native
scroll, resize or key model). **Rejected:** a bespoke Rust widget per screen (57 widgets for
data that is already structured in Python).

---

## 1. Server command engine + generated catalogue — **done**

Landed: `scripts/gen_tui_command_catalog.py` (AST parse, `--check`, emits
`novacode_cli/server/catalog.py` + `rust/PARITY_COMMANDS.md`); `CommandResult.prompt` and
`.screen`; catalogue-driven routing in `ui/port_server.py`; `not_ported()` for every
unserved row; `prompt` starts a turn in `server/host.py`; `screen`/`screen_result`/
`catalogue` frames and their transport branches; `sandbox_id`/`sandbox_type` forwarded.

Evidence: `uv run python scripts/gen_tui_command_catalog.py --check` (105 rows, zero drift),
`uv run pytest tests/test_tui_command_catalog.py tests/test_tui_command_port.py -q`.

## 2. Client command surface + generic screen renderer — **done**

Landed: `Inbound::{Catalogue, Screen, CommandResult.screen}`; `/` palette with
`ui/palette.rs`; one renderer for `List | Table | Detail | Form | Markdown | Progress |
Confirm` in `ui/screen.rs` with a JSON answer matching the Python spec; `Esc` closes an
overlay before cancelling; `Effect::Answer`; `tests/screens.rs` + `tests/fixtures/screens.jsonl`.

## 3. Wave 2 — command screens, pickers and reports

**Landed (7 of 19):**

- `/context` — report blocks + breakdown **table**; builder `screen_specs.context_spec`.
- `/theme` — **list** of the 28 generated themes, marks the current one, applies and
  persists the answer, then repaints the client through a `ui_theme` frame.
- `/statusline` — **toggle form**: one checkbox per status-bar field, `Space` flips,
  `Tab` walks, `Enter` saves; the answer is a field-keyed map of booleans persisted to the
  same `statusline_fields` slot the Textual picker writes. This is the multi-select answer
  shape the rest of wave 2 reuses.
- `/settings` — **full form**: every section the Textual screen edits (AGENT, TOOL
  OUTPUT, LEARNING, VOICE, INPUT, PERFORMANCE, AUTOMATION, EXPERIMENTAL) as toggles and
  fixed choices. Values are read and written through the same `NovaConfig` accessors, so
  both frontends see one config. A field the client omits keeps its current value, and
  the swarm gate is re-derived from teams so an impossible state cannot be written.
- `/voice` — the VOICE fields as their own form; bare `/voice` and `/voice settings`
  open it, while `test`, `download`, `doctor`, `status`, `on|off`, `mode` and `speak`
  keep the shared handler's output because they are actions, not settings.
- `/oracle-mode` — `on|off|status` toggles/reports the session flag, and
  `settings` opens a validated form (target, rounds, reviewers, scope, default-enabled)
  persisted through `OracleSettings`, so an out-of-range answer is refused rather than
  written.

**Modal commands — all served.** Each one is a builder in `novacode_cli/ui/screen_specs.py`
(pure, no Textual), a row in `SCREEN_BUILDERS` in `ui/port_server.py`, and a catalogue flip.
What is left of the surface is app-side behaviour and the background trio:

- [x] `command-center` — saved conversations overlaid with live tabs, status-count filter bar as counts on the list, one row per conversation (the row model moved to `novacode_cli/ui/command_center.py`); choosing a live tab answers `attach`, a saved one resumes in a new tab
- [x] `resume` — saved-session picker; the port opens a tab that continues it (`--continue <id>`, saved project folder) instead of rewriting the live session, and passes the id through `create_session`'s additive `resume` field
- [x] `rewind` — checkpoint list → what to restore → rewritten checkpoint; the transcript is replayed as blocks under a `clear` action because the protocol has no restore frame (`novacode_cli/ui/transcript.py`)
- [x] `tree` — the session's own message tree as a flat picker in conversation order (bookmarks marked `★`); the choice rewrites the checkpoint and the transcript is replayed as blocks, like `/rewind`
- [x] `tabs` — list/switch over the registry; choosing a row answers `attach`, which the client sends as the existing frame. `new|close|launch|resume` still refuse explicitly.
- [x] `voice` — VOICE section form; subcommands still run through the shared handler
- [x] `usage` — provider bars plus the session report in one `detail` screen; `USAGE_NOTE` moved to `commands/planner_usage.py` so both frontends print one caption
- [x] `share` — with an argument the tunnel sub-commands run through their handler; with none the port publishes the same page `/export` writes as a secret gist through `gh`, after an explicit confirmation
- [x] `update` — the check and its three outcomes; installing stays `nova update` because the server cannot replace its own process
- [x] `subagents` — list/show/create/delete over the same one-file agents; creation writes the same frontmatter
- [x] `/teammates` management: create/edit/delete, assign, approve/deny, pause/resume/stop, preview URL, auto-approve, history. The live collapsible feed remains.
- [x] `oracle-mode` — on/off/status + a validated settings form
- [x] `proactive` — list → action list → validated form, run queue, history report, remove confirm; the run/tag runtime moved to `novacode_cli/proactive_runtime.py` so the server actually executes queued runs.
- [x] `/rooms` owner actions: read/send, message view/edit/delete/react/pin, task board read, channel settings and create. Starting task work and live room events remain.
- [x] `ui` — deliberately `n/a`: the editor patches Textual widgets, so `/ui` names the layout profile it would edit and says so

Also in this wave: extend the `FileOp` frame with optional `before`/`after` text in
`server/frames.event_frame` so `diff_preview` has real content (ADR-008 gap).

Evidence when a row lands: `uv run pytest tests/test_tui_pickers.py tests/test_command_reports.py
tests/test_tui_scrollback.py -q` (Textual stays green) plus a corpus frame in
`tests/fixtures/commands.jsonl`.

## 4. Wave 3 — sessions, tabs, background tasks (server-owned)

**Landed (server half):** `novacode_cli/server/sessions.py` with a `SessionRegistry`
(one capacity number from `sessions.supervisor.MAX_SESSIONS`); every host registers on
start and leaves on close; `sessions` lists every session with `status`/`model` and marks
`active`; `attach` now switches the connection for real — `notice`, then `sessions`, with
context frames fanning out to every attached viewer; a full server refuses the next
session with `error`/`code:"capacity"` before `ready`. Disconnecting a viewer never
closes someone else's session. Evidence: `tests/test_tui_multi_session.py` (5 tests,
two real loopback clients).

**Remaining:**

- [x] Spawn/close child sessions: `create_session`/`close_session` over `SessionSupervisor.spawn` via `server/children.py` (`ChildHost` + manager). Scope: child tabs share the parent cwd and are chat sessions; child slash commands are limited to `/stop`, `/status`, `/clear`, `/quit`.
- [ ] `s.` session tag routing in `PROTOCOL.md` §3.3 (reserved, still unused)
- [x] Rust `src/ui/tabs.rs`: chips with `alt+<n>`, running marker, active highlight,
      hidden until there is more than one session; `tabs::attach_for` decides the key
- [x] Per-session `AppState`: each tab keeps its transcript, streaming text, todos, composer draft, scroll, running flag and last error; switching restores it and clears a stale modal/screen. History before this client attached is still not replayed (no transcript frame yet).
- [x] Close/create keys (`ctrl+n`, `ctrl+w`) and `/tabs new|close`; the Textual clickable cross is not reproduced.
- [ ] `background_tasks` panel — the socket worker does not carry the app-level agent jobs yet.
- [x] `scripts/record_tui_session.py --scenario tabs` + replay snapshot (`tests/fixtures/tabs.jsonl`, `frames_replay.rs`)

## 5. Wave 4 — agent surface panels — **not started**

`oracle_panel`, `prime_panel`, `council_settings_screen`, `council_view`, `proactive`,
`teammates`, `teammate_updates`, `rooms`, `room_message`, `room_settings` (10 rows).

- [x] `council_settings_screen`: seat list + per-seat form, validated by `parse_settings`; `/council settings` served, other council verbs refuse explicitly.
- [ ] Builders for the remaining list/form panels
- [ ] New `ui_events` dataclasses `RoomMessage`, `TeammateUpdate` (streams)
- [x] Stored council runs: `/council history|view|approve|reject|cancel` over the artifact store; approve returns the implementation handoff as a prompt.
- [x] `/council <task>` starts the planner in a background task, streams phase status, and opens the selected plans as a detail screen.
- [x] `src/ui/council.rs`: a transcript council card with phase stepper, seats, failures, counters and disagreement.
- [x] Plan actions: the plans detail screen offers Approve #N / Reject / Close.
- [x] Council animation: the active phase and working seats use a tick-driven spinner.
- [x] Side-by-side plan panes: a generic `split` screen with two markdown panes and actions.
- [ ] Animated card transitions (the remaining bespoke surface)
- [ ] `--scenario council` fixture asserting both panes and approve/reject answers

## 6. Wave 5 — visual identity, animation, avatar, matrix

**Landed:** `scripts/gen_theme_table.py` → `novacode_cli/ui/theme_table.py` (28 themes,
generated from `tui/themes.py` + Textual built-ins, `--check`ed); `frames.ui_theme`;
`Theme::from_palette` in `rust/nova-tui/src/theme.rs` with hex parsing that fails soft; the
status bar names the theme in force. `/theme` now repaints the client for real.

**Remaining (26 rows):**

- [ ] `palette`, `themes`, `icons`, `home`, `home_scene`, `statusline`, `motion`,
      `animation_rate`, `animations`, `presence`, `stall_watch`, `update_notice`,
      `workspace_approval` — the remaining visual-identity modules
- [ ] `matrix3` → `src/ui/matrix.rs`; `avatar*` → half-block sprites in `src/ui/avatar.rs`
- [ ] `widgets`, `output_body`, `output_buffer`, `scrollback`, `session_transcript` — check
      against `Scrollback` behaviour (line budget, compaction rows) with explicit Rust tests
- [ ] `terminal`, `terminal_view`, `screens`, `reference_screens`
- [ ] `--scenario theme` fixture asserting each theme renders distinct accent cells

## 7. Wave 6 — shell, PTY, platform and provider surfaces — **not started**

`pty_shell`, `shell_output`, `auth_display`, `auth_screens`, `provider_signin`,
`mcp_tools_screen`, `browser_settings_screen`, `caveman_screen`, `voice_downloads`,
`windows_input` (10 rows).

- [ ] `pty` frames (`pty`, `pty_input`, `pty_resize`) and a cell-grid paint path
- [ ] `Form` screens over `provider_signin.py` data (`/auth`, `/mcp`, `/browser-use`)
- [ ] `windows_input` semantics on `Event::Paste` with a `cfg(windows)` test
- [ ] PTY integration: `/computer` → `pwd` → echoed path

## 8. Remaining command handlers (bulk flip)

**Now:** 53 served (42 shared-handler, 3 client actions, 4 port screens, 4 port commands).
**Waiting:** 52 — 33 app-side, 15 modal (step 3), 4 background (`/council`, `/create`,
`/ralph`, `/trello`).

- [x] `/auto-approve` — the first app-side command served natively: `PORT_COMMANDS` in the
      port, evidence `port-command`, wording identical to the Textual handler, and the
      choice is never written to config (asserted)
- [x] `/goal` — delegates to the shared `handle_goal_command`; a `kickoff` prompt rides
      `prompt`, so setting or resuming a goal starts the run the way `/init` does
- [x] `/verbose` — session toggle, Textual wording, no persistence
- [x] `/stop` — answers `action: "cancel"`; the client turns it into the same `cancel`
      frame Esc sends (the server cannot cancel its own host task)
- [x] `/tokens` + `/cost` (detail screen), `/copy`/`/copy all` (clipboard action),
      `/steer` (add/list/remove/clear), `/redirect` (cancel + prompt), `/nova` (avatar action).
- [x] `/export` (HTML write), `/worktree` (list/create/remove with confirm).
- [x] `/agent-server` (status/logs/start/stop/restart), `/ci-fix` (fetch → prompt).
- [x] `/btw` — side question as a background task, answer logged to the transcript.
- [x] `/reindex` — background rebuild with status events and a log block.
- [x] `/notifications` (list/dismiss/approve/clear), `/images` (list/remove/clear),
      `/research` (usage text or swarm prompt), `/tasks` (empty-state table),
      `/files` (tracker report), `/restore` (index/path/picker with confirm).
- [ ] Serve the remaining app-side handlers (`/tasks`, `/computer`…) one at a time, each with a test
- [ ] Keep `git grep -n "NovaApp" novacode_cli/commands/` at zero (the gate asserts it)
- [ ] Long work (`/voice download`, `/tests`, `/reindex`) → host task + `notice` progress
- [ ] Blocking handlers (`/trello`, `/create`, `/ralph`, `/council`) → host task, never the
      command coroutine
- [x] `tests/test_tui_no_textual_in_server.py`: AST + subprocess gate over
      `server/`, `commands/`, `ui/`; it caught `ui/stream_preview.py` reaching into
      `novacode_cli.tui.output_buffer` (now `ui/output_buffer.py`, re-exported)
- [x] `novacode_cli/commands/**` never references `NovaApp` (asserted by the same test)

## 9. Parity harness: corpora, gates, ledger automation

**Landed:** the recorder (`scripts/record_tui_session.py`) with deterministic output; six
corpora (`core_chat`, `commands`, `screens`, `theme`, `tabs`, `council`); `tests/frames_replay.rs` applies them
without `BLESS` — the `theme` corpus asserts a recorded palette actually repaints the client
(accent and muted come from the frame, not the baked default); `tests/test_ledger_gate.py`
refuses a flip while `not-started` rows remain; the catalogue and theme generators are
drift-checked.

- [x] `--scenario tabs` — two sessions, attach round trip, separate transcripts
- [x] `--scenario council` — recorded progress events and the plans screen
- [x] `--scenario theme` — recorded, replayed, byte-identical on re-record
- [ ] `--scenario pty` (needs step 7)
- [x] `scripts/verify_tui.py` — every gate in one command with a per-gate verdict
      (`--fast` skips Rust, `--list` prints them); this replaces the plan's `make` targets,
      which are unavailable on the workstation this port was built on
- [ ] Wire `scripts/verify_tui.py` into `.github/workflows/ci.yml` as one `parity` job

## 10. Default flip + release — **blocked**

- [ ] Flip `--ui` to ratatui by default (the ledger gate must pass first)
- [ ] Ship `novacode_cli/bin/nova-tui` in the wheel and assert it in `tests/test_tui_rust_client.py`
- [ ] Changelog + PyPI checklist per `.nova/NOVA.md`

## Next slice (recommended order)

1. `ui` — extract `UISpec`/`PanelSpec` out of `tui/harness.py` into a Textual-free module,
   then the same form mechanism can inspect/patch/preview/commit a layout.
2. Rust tab bar over the registry step 4 now provides: `sessions` gives the rows and
   `attach` switches, so `src/ui/tabs.rs` is drawing plus `alt+<n>`, not new protocol.
3. `command-center` and `rooms` — both read the same `sessions` rows.
4. Same-directory resume **in place** (adopting another session's conversation, model,
   todos and artifact bindings in the live process) — Textual does that inside `_run_resume`;
   the port opens a tab instead. Extracting that branch is still the work.
