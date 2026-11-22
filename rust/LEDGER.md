# Parity ledger — Textual TUI → ratatui

The Textual TUI is ~20 000 lines in `novacode_cli/tui/app.py` alone, plus 65
further modules and 54 command-handler modules. "Completely ported" is only
auditable as a table, so this file *is* the audit: one row per module, one state
per row. Nothing may be deleted from `novacode_cli/tui/` until every row is
`ported` or `n/a`, and `--ui ratatui` cannot become the default until then
(ADR-003, ADR-004).

## States

| State | Meaning |
|---|---|
| `not-started` | Behaviour exists only in the Textual UI (or only in the Python process with no ratatui surface). |
| `ported` | The ratatui client reproduces the behaviour, and a recorded-frame or unit check covers it. |
| `server-side` | The behaviour moved into the Python server and is reached through a protocol frame; no Rust code is needed. |
| `n/a` | Not UI behaviour (package markers, re-export shims, pure data). |

## Coverage reality check

* Plan estimate: 73 modules under `novacode_cli/tui/`. Actual: **66** Python
  modules (65 real + `__init__.py`), verified with
  `Get-ChildItem novacode_cli/tui -File -Filter *.py`. The ledger lists the real
  66.
* Command handlers: **54** modules under `novacode_cli/commands/` (plus
  `__init__.py`). `/help`, `/clear`, `/quit` and `/exit` are also special-cased
  directly inside `commands/commands.py` before the registry, so that module has
  its own row.
* Increment 1 ships core chat only. Every rest-of-UI row is `not-started` with
  the wave that owns it; the wave numbers are the program roadmap's, assigned by
  module responsibility and provisional until that wave starts.

## `novacode_cli/tui/` (66 modules)

### Core transcript, input and app shell (wave 1)

| Module | State | Notes |
|---|---|---|
| `novacode_cli/tui/__init__.py` | n/a | Package marker; exports `run_tui`. |
| `novacode_cli/tui/app.py` | not-started | 20 093-line monolith. Increment 1 moves the chat loop to the server; each screen is owned by its wave. |
| `novacode_cli/tui/output_body.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/output_buffer.py` | not-started | The bounded tail moved to `novacode_cli/ui/output_buffer.py` (framework-free) and this module re-exports it, so `ui/stream_preview.py` no longer drags Textual in; the ratatui scrollback budget itself is still wave 5. |
| `novacode_cli/tui/scrollback.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/session_transcript.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/terminal.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/terminal_view.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/widgets.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/icons.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/palette.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/themes.py` | server-side | Its palettes are AST-generated into `novacode_cli/ui/theme_table.py` (`scripts/gen_theme_table.py --check`) and reach the client as a `ui_theme` frame, so `/theme` works without importing Textual. The Textual module keeps serving the Textual UI. |
| `novacode_cli/tui/home.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/home_scene.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/screens.py` | not-started | 40+ modals in one module. Served so far as generic `screen` specs: `/context`, `/settings`, `/statusline`, `/theme`, `/voice`, `/oracle-mode`, `/usage`, `/update`, `/subagents`, plus the sessions/rooms/council/teammates pickers and the council settings forms. The `/ui` layout editor is still Textual-only; `/rewind` is served as list → scope choice with the conversation replayed as blocks; its `USAGE_NOTE` now lives in `novacode_cli/commands/planner_usage.py` so both frontends print one caption. |
| `novacode_cli/tui/reference_screens.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/statusline.py` | server-side | The field table moved to `novacode_cli/ui/statusline_fields.py` (Textual-free) and this module re-exports it, so `/statusline` and the Textual picker read one definition. The picker itself is the next row. |
| `novacode_cli/tui/motion.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/animation_rate.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/animations.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/presence.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/stall_watch.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/update_notice.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/workspace_approval.py` | not-started | Owned by this wave; no ratatui counterpart yet. |

### Sessions, panels and pickers (waves 2-3)

| Module | State | Notes |
|---|---|---|
| `novacode_cli/tui/session_tab.py` | not-started | Tab bar, per-tab state, attach, `ctrl+n` create and `ctrl+w` close are done. The Textual clickable cross is not reproduced (closing is a key or `/tabs close`), so the row stays not-started by the letter of the audit. |
| `novacode_cli/tui/session_pane.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/background_tasks.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/resume_picker.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/history_tree_screen.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/session_import.py` | server-side | `/import` and `/compare` go through `novacode_cli/session/imported_context.py`, which now owns the parser, the loader, the token budget and `comparison_prompt`; the Textual module keeps `/sessions`, `/context imported` and its screens. Evidence: `tests/test_session_import.py`, `tests/test_tui_import_commands.py`. |
| `novacode_cli/tui/handoff_screen.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/pickers.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/command_center.py` | server-side | The row model, ordering, ages and filters moved to `novacode_cli/ui/command_center.py`; the modal keeps only the Textual widgets and `pane_activity`. |
| `novacode_cli/tui/command_reports.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/statusline_screen.py` | server-side | Served as a toggle `form` screen: `screen_specs.statusline_spec` + `server_port._statusline_screen`, answered by `rust/nova-tui/src/ui/screen.rs` and persisted to the same `statusline_fields` config key. |
| `novacode_cli/tui/decision_settings.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/diff_preview.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/code_mode_panel.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/harness.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/gc_tuning.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/loop_commands.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/ralph.py` | server-side | `/ralph` starts the shared `handle_ralph_command` as a task the port owns and turns its progress lines into transcript blocks; `/ralph --status` collects `handle_ralph_status`. Evidence: `tests/test_tui_background_commands.py`. |

### Agent surface panels (wave 4)

| Module | State | Notes |
|---|---|---|
| `novacode_cli/tui/oracle_panel.py` | not-started | `/oracle-mode status` renders the live run's reviewers and scores as a detail screen. The animated panel, per-reviewer threads and owner controls are not reproduced. Evidence: `tests/test_tui_preferences_command.py`. |
| `novacode_cli/tui/prime_panel.py` | server-side | `/prime` toggles via the shared handler, opens the saved-work browser and report screens, and forgets with a confirm. The docked live panel is not reproduced. Evidence: `tests/test_tui_prime_command.py`. |
| `novacode_cli/tui/council_view.py` | server-side | Stored-run reading/approval is served; `/council <task>` runs the planner as a background task, streams phase `StatusUpdate`s and opens the selected plans as a detail screen. The council card is reproduced in `rust/nova-tui/src/ui/council.rs` with a tick-driven spinner and plan actions; side-by-side markdown panes are a generic `split` screen; transition animation is not. Evidence: `tests/test_tui_council_view_command.py`. |
| `/rooms` owner actions: read/send, message actions, board read, settings, create | `ported` | `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_rooms_command.py` |
| `/teammates` management (create/edit/delete/assign/decide/pause/stop/preview/auto/history) | `ported` | `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_teammates_command.py` | `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_teammates_command.py` |
| Council progress stream: `CouncilUpdate` → one card with phases/seats/counters | `ported` | `novacode_cli/ui_events.py`, `rust/nova-tui/src/ui/council.rs`, `rust/nova-tui/tests/frames_replay.rs`, `rust/nova-tui/tests/fixtures/council.jsonl` |
| Generic `split` screen: two markdown panes plus selectable actions | `ported` | `rust/nova-tui/src/ui/screen.rs`, `rust/nova-tui/src/state.rs`, `rust/nova-tui/tests/screens.rs` |
| Council plan actions: Approve #N / Reject / Close on the plans screen | `ported` | `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_council_view_command.py` |
| `/council stored runs`: history, view, approve→prompt, reject, cancel | `ported` | `novacode_cli/ui/port_server.py`, `novacode_cli/ui/screen_specs.py`, `tests/test_tui_council_view_command.py` |
| `novacode_cli/tui/council_settings_screen.py` | server-side | `/council settings` edits the same draft through a seat list plus one form per seat, applies personality templates, supports Reset, and validates with `parse_settings` before saving. Evidence: `tests/test_tui_council_settings_command.py`. |
| `novacode_cli/tui/council_view.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/proactive.py` | server-side | `/proactive` lists, edits, runs, reports and removes agents over the SQLite store; `ProactiveRuntime` moved to `novacode_cli/proactive_runtime.py` and is hosted by `ServerPort`. Evidence: `tests/test_tui_proactive_command.py`, `tests/test_proactive.py`. |
| `novacode_cli/tui/teammates.py` | server-side | `/teammates` manages the roster through the worker API: create/edit/delete, assign, approve/deny, pause/resume/stop, preview URL, auto-approve and history. The live tabbed panel is not reproduced; the same fields are the action menu/detail screens. Evidence: `tests/test_tui_teammates_command.py`. |
| `novacode_cli/tui/teammate_updates.py` | not-started | `/teammates` renders the same public task/result fields on demand; the live collapsible feed is not reproduced. |
| `novacode_cli/tui/rooms.py` | server-side | `/rooms` lists channels, reads/sends messages, shows the task board and edits channel settings over `RoomStore`. Message view/edit/delete/react/pin are reproduced; starting task work and live room events are not. Evidence: `tests/test_tui_rooms_command.py`. |
| `novacode_cli/tui/room_message.py` | not-started | Messages render as markdown in `/rooms`; reaction/edit/action controls are not reproduced. |
| `novacode_cli/tui/room_settings.py` | not-started | Owned by this wave; no ratatui counterpart yet. |

### Visual identity, media and shell (waves 5-6)

| Module | State | Notes |
|---|---|---|
| `novacode_cli/tui/avatar.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/avatar_frames.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/avatar_images.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/matrix3.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/pty_shell.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/shell_output.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/browser_settings_screen.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/caveman_screen.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/voice_downloads.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/windows_input.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/auth_display.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/auth_screens.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/provider_signin.py` | not-started | Owned by this wave; no ratatui counterpart yet. |
| `novacode_cli/tui/mcp_tools_screen.py` | not-started | Owned by this wave; no ratatui counterpart yet. |

## `novacode_cli/commands/` (54 modules)

Command names are the literal `registry.register("<name>", ...)` values found in
that module; `—` means the module is a helper, events module or special-cased
path with no registry entry of its own.

| Module | Commands | State | Notes |
|---|---|---|---|
| `novacode_cli/commands/agents_commands.py` | `agents` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/auth_handler.py` | `auth`, `connect` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/browser_use_handler.py` | `browser-use` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/cache_handler.py` | `cache` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/chat_handler.py` | `council` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/ci_fix.py` | — | not-started | No registry entry; reached from `commands.py`, the TUI, or imported as a helper. |
| `novacode_cli/commands/claude_plan.py` | — | not-started | No registry entry; reached from `commands.py`, the TUI, or imported as a helper. |
| `novacode_cli/commands/codex_plan.py` | — | not-started | No registry entry; reached from `commands.py`, the TUI, or imported as a helper. |
| `novacode_cli/commands/commands.py` | — | server-side | Special-cases `/help`, `/clear`, `/quit`, `/exit` (and 15 more) before the registry; `/help`, `/clear`, `/quit` are routed through `UIPort` in increment 1. |
| `novacode_cli/commands/create_handler.py` | `create` | server-side | `/create` awaits `handle_create_command` under console capture and reports its URL or stop text. Evidence: `tests/test_tui_background_commands.py`. |
| `novacode_cli/commands/create_server.py` | — | server-side | Started by `/create` through `handle_create_command`; the browser opens on the machine running the server. Evidence: `tests/test_tui_background_commands.py`. |
| `novacode_cli/commands/cron_handler.py` | `cron` | server-side | Dispatched inline by `commands/commands.py` rather than a registry entry, so the port delegates with `_passthrough`; the handler prints what it always printed. Evidence: `tests/test_tui_app_commands.py`. |
| `novacode_cli/commands/dream_handler.py` | `dream` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/effort_handler.py` | `effort` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/evolution_handler.py` | `evolution` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/file_commands.py` | `files`, `images`, `restore` | server-side | All three are served by the port: `/images` (list/remove/clear), `/files` (tracker report), `/restore` (index/path/picker). Evidence: `tests/test_tui_session_commands.py`. |
| `novacode_cli/commands/guardrails_handler.py` | `guardrails` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/handoff_handler.py` | `passing` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/hooks_handler.py` | `hooks` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/init_handler.py` | `init` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/learning_handler.py` | `learning`, `learn` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/lite_mode_handler.py` | `lite-mode` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/log_commands.py` | `log` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/mcp_handler.py` | `mcp` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/model_handler.py` | `model` | server-side | `/model` (list + set by argument) routed through `UIPort` in increment 1. |
| `novacode_cli/commands/notifications_handler.py` | `notifications` | server-side | Served by the port (list/dismiss/approve/clear) with the same `SessionState` calls the Textual handler makes. Evidence: `tests/test_tui_session_commands.py`. |
| `novacode_cli/commands/onboard_handler.py` | `onboard` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/plan_handler.py` | `plan` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/planner_usage.py` | — | server-side | Owns `usage_text` and now `USAGE_NOTE`, which both frontends print under the bars; `/usage` composes them with the session report. Evidence: `tests/test_tui_report_screens.py`. |
| `novacode_cli/commands/plugin_install_handler.py` | `plugin` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/plugins_handler.py` | `middleware` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/prime_handler.py` | `prime` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/prompt_handler.py` | `prompt` | server-side | Dispatched inline by `commands/commands.py` rather than a registry entry, so the port delegates with `_passthrough`; the handler prints what it always printed. Evidence: `tests/test_tui_app_commands.py`. |
| `novacode_cli/commands/provider_usage.py` | — | server-side | `all_usage()` is read off the event loop by `/usage`; each provider read is best-effort, so a failure becomes a message, not a dead session. Evidence: `tests/test_tui_report_screens.py`. |
| `novacode_cli/commands/ralph_events.py` | — | not-started | No registry entry; reached from `commands.py`, the TUI, or imported as a helper. |
| `novacode_cli/commands/ralph_handler.py` | `ralph` | server-side | `/ralph` runs `handle_ralph_command` as a port-owned task; `emit` becomes transcript blocks and `--status` collects `handle_ralph_status`. Evidence: `tests/test_tui_background_commands.py`. |
| `novacode_cli/commands/refine_handler.py` | `refine` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/research_handler.py` | `research` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/rootapp_remote.py` | — | not-started | No registry entry; reached from `commands.py`, the TUI, or imported as a helper. |
| `novacode_cli/commands/router_handler.py` | `router` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/server_commands.py` | `servers`, `tests`, `kill` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/session_commands.py` | `sessions`, `save`, `compact` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/share_handler.py` | `share` | server-side | The tunnel sub-command runs through its handler; the session gist reuses the export page. |
| `novacode_cli/commands/side_commands.py` | — | not-started | No registry entry; reached from `commands.py`, the TUI, or imported as a helper. |
| `novacode_cli/commands/skill_invoke.py` | — | not-started | No registry entry; reached from `commands.py`, the TUI, or imported as a helper. |
| `novacode_cli/commands/skills_commands.py` | `skills` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/trace_handler.py` | `trace` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/trello_handler.py` | `trello` | server-side | `/trello` starts `TrelloServer` and runs `trello_watch_loop` as a port-owned task with the headless `execute_task`; `stop`/`status` print through their handlers. Evidence: `tests/test_tui_background_commands.py`. |
| `novacode_cli/commands/trello_remote.py` | — | not-started | No registry entry; reached from `commands.py`, the TUI, or imported as a helper. |
| `novacode_cli/commands/trello_server.py` | — | server-side | Started by `/trello` in the port; the board is served over HTTP and the port keeps the process alive. Evidence: `tests/test_tui_background_commands.py`. |
| `novacode_cli/commands/vision_handler.py` | `vision` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |
| `novacode_cli/commands/voice_handler.py` | — | not-started | No registry entry; reached from `commands.py`, the TUI, or imported as a helper. |
| `novacode_cli/commands/webhook_handler.py` | `webhook` | server-side | Dispatched inline by `commands/commands.py` rather than a registry entry, so the port delegates with `_passthrough`; the handler prints what it always printed. Evidence: `tests/test_tui_app_commands.py`. |
| `novacode_cli/commands/wiki_commands.py` | `ingest`, `ask`, `file`, `wiki` | not-started | Picked by wave 2 (command handlers + pickers) unless its UI is screen-only. |

## What the port actually delivers so far

Increment 1 is core chat; increment 2 adds the command catalogue and the screen
protocol. Everything below is checked by the test named in its row.

| Capability | State | Evidence |
|---|---|---|
| Wire protocol + framing | `server-side` | `rust/PROTOCOL.md`, `novacode_cli/server/frames.py` |
| Loopback token server, session registry, real `attach` | `server-side` | `novacode_cli/server/transport.py`, `novacode_cli/server/sessions.py`, `tests/test_tui_multi_session.py` |
| Session host: prompt/cancel/interrupt/command | `server-side` | `novacode_cli/server/host.py`, `tests/test_tui_server_protocol.py` |
| Command catalogue: all 105 `TUI_COMMANDS` rows, generated, drift-checked | `server-side` | `novacode_cli/server/catalog.py`, `tests/test_tui_command_catalog.py` |
| Command routing by catalogue (99 served, the rest answer explicitly) | `server-side` | `novacode_cli/ui/port_server.py`, `tests/test_tui_command_port.py` |
| First wave-2 screen: `/context` report + breakdown table | `ported` | `novacode_cli/ui/screen_specs.py`, `tests/test_tui_screen_specs.py`, `rust/nova-tui/src/ui/screen.rs` |
| `/theme`: generated table, picker, persisted choice, live repaint | `ported` | `novacode_cli/ui/theme_table.py`, `tests/test_tui_theme_command.py`, `rust/nova-tui/src/theme.rs` |
| `/statusline`: toggle form, persisted field list, boolean answers | `ported` | `novacode_cli/ui/statusline_fields.py`, `tests/test_tui_statusline_command.py`, `rust/nova-tui/tests/screens.rs` |
| `/settings`: every section as toggles/choices, persisted through one config | `ported` | `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_preferences_command.py`, `rust/nova-tui/tests/screens.rs` |
| `/voice`: VOICE section form; action subcommands keep the shared handler | `ported` | `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_preferences_command.py` |
| `/oracle-mode`: on/off/status + validated settings form | `ported` | `novacode_cli/ui/port_server.py`, `tests/test_tui_preferences_command.py`, `rust/nova-tui/src/ui/screen.rs` |
| Long forms scroll to the focused field (`↑↓`, `PgUp/PgDn`, `Home/End`) | `ported` | `rust/nova-tui/src/ui/screen.rs`, `rust/nova-tui/tests/screens.rs` |
| Session tabs keep separate transcripts; switching restores entries/todos/composer/scroll | `ported` | `rust/nova-tui/src/state.rs`, `rust/nova-tui/tests/frames_replay.rs`, `rust/nova-tui/tests/fixtures/tabs.jsonl` |
| Recorded `tabs` corpus proves an attach round trip and transcript isolation | `ported` | `rust/nova-tui/tests/frames_replay.rs`, `rust/nova-tui/tests/fixtures/tabs.jsonl` |
| `/proactive`: agent list → actions → validated form/run/history/remove | `ported` | `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `novacode_cli/proactive_runtime.py`, `tests/test_tui_proactive_command.py` |
| `/council settings`: seat list → per-seat form → validated save | `ported` | `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_council_settings_command.py` |
| `/tabs new|close`: server-spawned child sessions, ownership-checked close | `ported` | `novacode_cli/server/children.py`, `novacode_cli/server/transport.py`, `tests/test_tui_child_sessions.py`, `tests/test_tui_server_transport.py` |
| `/tabs`: session picker over the registry; choice returns an `attach` action | `ported` | `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_tabs_command.py`, `rust/nova-tui/tests/screens.rs` |
| `/usage`: provider bars, this session's report and the caption, as one `detail` screen | `ported` | `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `novacode_cli/commands/planner_usage.py`, `tests/test_tui_report_screens.py` |
| `/update`: the same check the Textual modal runs; installing stays `nova update` | `ported` | `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_report_screens.py` |
| `/subagents`: list, `show`, `create` (form, optional AI prompt) and `delete` (confirm) over the same one-file agents | `ported` | `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_report_screens.py` |
| `/ui`: layout editor is Textual-only (`n/a`); the port names the profile it would edit | `n/a` | `novacode_cli/ui/layout_profile.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_report_screens.py` |
| `/tree`: this session's message tree as a picker → rewritten checkpoint, transcript replayed | `ported` | `novacode_cli/ui/transcript.py`, `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_tree_resume_commands.py` |
| `/resume`: saved-session picker → tab that continues it (`--continue <id>`) in its own folder | `ported` | `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `novacode_cli/server/children.py`, `novacode_cli/server/transport.py`, `tests/test_tui_tree_resume_commands.py`, `tests/test_tui_child_sessions.py`, `rust/nova-tui/tests/screens.rs` |
| `/command-center`: saved conversations overlaid with live tabs, status filter, attach or resume | `ported` | `novacode_cli/ui/command_center.py`, `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_command_center_share.py` |
| `/share`: tunnel sub-commands through their handler; no argument publishes the export page as a secret gist | `ported` | `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `novacode_cli/commands/share_handler.py`, `tests/test_tui_command_center_share.py` |
| `/import <provider> <id|path> [--mode]`: one bounded reference message, imported context stored beside the session | `ported` | `novacode_cli/session/imported_context.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_import_commands.py` |
| `/compare <provider>:<id> <provider>:<id>`: one bounded prompt returned for the host to run | `ported` | `novacode_cli/session/imported_context.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_import_commands.py` |
| `/ralph`: autonomous loop as a port-owned task; lines arrive as blocks, `--status` reports | `ported` | `novacode_cli/commands/ralph_handler.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_background_commands.py` |
| `/trello`: board server + watcher as a port-owned task; cards run through the headless executor | `ported` | `novacode_cli/commands/trello_handler.py`, `novacode_cli/commands/trello_server.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_background_commands.py` |
| `/create`: Skills & Agents browser through its handler; `stop` closes it | `ported` | `novacode_cli/commands/create_handler.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_background_commands.py` |
| `/rewind`: checkpoint list → what to restore → rewritten checkpoint, transcript replayed | `ported` | `novacode_cli/ui/transcript.py`, `novacode_cli/ui/screen_specs.py`, `novacode_cli/ui/port_server.py`, `tests/test_tui_rewind_command.py`, `rust/nova-tui/tests/frames_replay.rs` |
| App-side commands answered directly: `/computer`, `/artifacts`, `/cowork`, `/reload-plugins`, `/cron`, `/webhook`, `/prompt`, `/ponytail-*` | `ported` | `novacode_cli/ui/port_server.py`, `tests/test_tui_app_commands.py` |
| `/auto-approve`, `/verbose`, `/stop`: session toggles + a client `cancel` action | `ported` | `novacode_cli/ui/port_server.py`, `tests/test_tui_port_commands.py`, `tests/test_tui_auto_approve_command.py`, `rust/nova-tui/src/state.rs` |
| `/goal`: shared parser, `kickoff` returned as `prompt` | `ported` | `novacode_cli/ui/port_server.py`, `tests/test_tui_goal_command.py` |
| Command parity worklist, one row per command (generated) | `server-side` | `rust/PARITY_COMMANDS.md`, `tests/test_tui_command_catalog.py` |
| `screen` / `screen_result` round trip, fail-closed after `SCREEN_TIMEOUT` | `server-side` | `novacode_cli/server/host.py`, `tests/test_tui_server_protocol.py` |
| `catalogue` frame drives the client's `/` autocomplete | `ported` | `rust/nova-tui/src/ui/palette.rs`, `rust/nova-tui/tests/screens.rs` |
| Declarative screen renderer (list, table, detail, form, markdown, progress, confirm) | `ported` | `rust/nova-tui/src/ui/screen.rs`, `rust/nova-tui/tests/fixtures/screens.jsonl` |
| Transcript, composer, status bar, approval modal | `ported` | `rust/nova-tui/src/ui/`, `rust/nova-tui/tests/frames_replay.rs` |
| Golden-frame corpora (`core_chat`, `screens`, `commands`) | `ported` | `rust/nova-tui/tests/fixtures/core_chat.jsonl`, `rust/nova-tui/tests/fixtures/screens.jsonl`, `rust/nova-tui/tests/fixtures/commands.jsonl` |
| Question and plan modals | `not-started` | The approval modal is ported; the other two interrupts render but their reply payloads are provisional (ADR-008). |
| File-op **diff** view | `not-started` | `FileOp` cards show operation + path; the wire carries no before/after text, so the diff lands with the file-op wave (ADR-008). |
| Cross-language handshake | `ported` | `tests/test_tui_rust_client.py` (real binary against the real server) |
| Everything else | `not-started` | rows above |
