# NovaCode 1.2.56

## Prompt and agent behaviour

- The agent now **researches before guessing**. A new `<research>` block (in both main
  prompts) directs it to check the codebase and installed package source first, then use
  `web_search`/`fetch_url` for official documentation of the exact version in use, and to
  never invent API signatures or option names from memory. Citations are required when an
  outside fact drives a consequential decision.
- Root-cause fixes over symptom patches, `git log`/`git blame` for history context,
  checking neighbouring files and manifests before picking a library, and leaving
  pre-existing broken tests alone (reported, not silently fixed) are now explicit
  workflow rules. Code findings reference `file_path:line_number`, and large files the
  agent just wrote are referenced by path instead of pasted.

## Memory that is actually used

- **Proactive recording.** The write-side instructions no longer wait for "when useful":
  concrete triggers (a stated preference, a correction, a decision with rationale, a
  verified fix, an environment gotcha, a project convention) plus an end-of-session gate —
  never end with durable, learned-but-unrecorded facts. The `write_memory` tool
  description carries the same triggers and the correct scoping (user preferences →
  agent.md, repo facts → project memory, lessons → topic pages).
- **Proactive recall.** Every non-trivial task now starts with one orienting
  `memory_search`, and a new **associative pre-turn recall** injects a short
  `<relevant_memories>` block of auto-matched topic excerpts into each model call —
  after the Anthropic cache breakpoint, so prefix caching is unaffected. Excerpts are
  labelled discovery-not-proof; full pages still come from explicit reads. Recall is
  cached per user message, invalidated by memory writes, and skipped for trivial input.

## Verification is mandatory

- The post-edit verify gate is always on: a turn that ends after a file edit with no
  later observed check is sent back once to run one (and a failed check sends it back to
  fix and rerun). The `NOVA_VERIFY_AFTER_EDIT` opt-out is gone.
- The detector now recognises custom check scripts (`uv run python scratch_test.py`,
  `bash tests/check_env.sh`, `./scripts/verify_all.sh`) and shell file writes
  (redirection, `sed -i`/`-i.bak`, `tee`, `cp`/`mv`, `patch`, `git apply`, PowerShell
  write cmdlets) as edits, while ignoring `/dev/null`, `2>&1` and comparison operators.
  Builds and typechecks (`cargo build/check`, `go vet`, `tsc`, `npm run build`, `make`)
  count as verification.

## TUI: ctrl+o expands output

- `ctrl+o` toggles expansion of every tool surface for the session — the condensed tool
  group, per-call cards and history tool cards — in the Textual client and the Rust
  client alike. Collapsed tool output advertises `[ctrl+o to expand]`, and with the
  toggle on, live streams render up to 2,000 lines / 200k chars instead of the usual
  200 / 20k tail. Collapsing never re-closes the group while a tool is streaming.

## Remote port: 105 of 105 commands served

- The socket port gained the last six app-side commands — `/claude-plan`, `/codex-plan`,
  `/claude-review`, `/codex-review`, `/loop` and `/remote` — so the port command surface
  is complete (105/105, 0 waiting).
- `/claude-plan` and `/codex-plan` run the other agent's CLI headless in its own process
  and return its plan as blocks plus a hand-off prompt for Nova; the `/claude-review` and
  `/codex-review` variants run read-only and ask Nova to check each finding. A missing
  CLI reports a targeted "not installed" failure; the external agent never enters Nova's
  conversation.
- `/loop` works from the port: bounded, session-owned recurrence (`30s <prompt>`,
  `<n>m <prompt>`, `"<cron>" <prompt>`, `list`/`status`/`stop [id]`) with a 1-second
  poll that expires overdue jobs and claims at most one due iteration — headless, so it
  runs even with no client attached. `instruction()` and `iteration_prompt()` moved to
  `novacode_cli/ui/loops.py` so both frontends share the exact wording.
- `/remote` delegates to the shared bridge handler; bare `/remote` reports status.

## Real terminal in the remote client

- A prompt starting with `!!` spawns a real pseudo-terminal instead of a queued shell
  command: one terminal at a time, output streams to the client as new `pty` /
  `pty_exit` frames (plain, colour-stripped lines with the command echoed for
  late-attaching clients), and keystrokes flow back via `pty_input` / `pty_close`.
  The Rust client's new terminal pane captures the whole keyboard (Esc closes the pane).
  Cursor-addressed programs still render imperfectly — a cell-grid terminal emulator is
  the documented next step.

## Rust client parity (avatar, home, brand art)

- `/nova` now ships the full sprite sheet once over the port (24 frames, glyph rows +
  colour palettes in the new `command_result.avatar` field) and the Rust client plays it
  with the same rules as Textual: reaction mood, talking/thinking while busy, sleep after
  60 s idle, an idle cycle every 8 s — drawn as an overlay above the composer, hidden
  under modals and on small terminals.
- The `ready` frame carries a `home` layout payload, and the Rust client draws the home
  banner (portrait/wordmark/version lockup) with the matrix code rain behind it — a
  dependency-free xorshift64* port of `matrix3.py`.
- `scripts/gen_brand_art.py` generates `rust/nova-tui/src/ui/brand_art.rs` from
  `novacode_cli/brand.py`, so both frontends render identical startup art; `--check`
  fails CI on drift. `avatar_frames.py` and `pty_shell.py` moved to `novacode_cli/ui/`
  to stay importable without Textual.
- All wire changes are additive; `PROTOCOL_VERSION` stays at 1.

## TUI performance

- Non-transcript notices queue for half a second and flush as one toast ("+N more" for
  older ones) instead of stacking overlapping bubbles during background-task bursts.
- `OutputLog.tail_matches()` skips the clear-and-rebuild of up to 200k chars when the
  live stream already painted the identical tail; chat bodies reuse a repaint-only path
  when their wrap-aware dimensions are unchanged; hidden-pane event buffers are capped at
  400 entries; log-pane heights only change when they actually change.
