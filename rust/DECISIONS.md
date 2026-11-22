# Decisions — Nova ratatui frontend

Short ADRs. Each records the decision, the alternative that was rejected and why,
so a later session does not have to re-litigate it. Numbers are stable; append,
never renumber.

---

## ADR-001 — Transport: plain TCP + newline-delimited JSON on loopback

**Decision.** The server binds `127.0.0.1:0` with `asyncio.start_server` and speaks
one JSON object per line, using the encoders already in
`novacode_cli/sessions/protocol.py`.

**Rejected.**

- _FastAPI + WebSocket._ `fastapi`/`uvicorn` live in the `[server]` extra
  (`pyproject.toml`), so every install would grow two dependencies to serve a
  local UI. The Cowork app keeps them and is untouched.
- _stdio child._ Simplest framing, but the backend stops being a server: exactly
  one frontend could ever attach, and remote/multi-frontend work would need the
  transport rewritten. It also collides with stdio MCP servers that close
  `sys.stdout` (`sessions/worker.py` documents that failure).
- _Embedding Python in the Rust process._ Doubles process hops and Rust build
  complexity for no behavioural gain.

**Consequences.** We must own framing limits and auth ourselves — both are
specified in `rust/PROTOCOL.md` (8 MiB line cap, `secrets.compare_digest`,
nothing accepted before `ready`).

---

## ADR-002 — Process model: `nova` supervises the server and the client

**Decision.** The Python process is long-lived and owns sessions. `nova --ui
ratatui` binds the socket, then spawns the `nova-tui` binary with
`--host/--port/--token` and inherited stdio; the client owns the terminal.
When the client exits, the server is torn down and `nova` exits with the client's
code. `nova --serve --serve-port N` exposes the same server standalone.

**Rejected.** _Client spawns the server._ Then a client crash leaves an orphan
holding the agent's file handles and the socket, and the token would have to
travel through the client's own argv anyway.

**Consequences.** Binding happens _before_ the spawn, so the client never races
the listener. Both ends are killed in `finally`/`atexit`; the pattern is copied
from `sessions/supervisor.py` because `main.py` ends in `os._exit()`, which runs
no `finally` blocks.

---

## ADR-003 — Parity bar: normalised content, not pixels

**Decision.** A screen is "ported" when a replayed event corpus produces the same
_content facts_ in the ratatui client as the recorded Textual assertions:
message order and text, tool-output last line present, scroll extent > 0,
composer value, modal occupying the screen. Implemented in
`rust/nova-tui/tests/frames_replay.rs` against
`tests/fixtures/core_chat.jsonl` + committed `.frames.txt` snapshots.

**Rejected.** _Pixel/snapshot equality with Textual._ Two different widget
toolkits cannot agree on padding, spinner phase or colour quantisation; a bar
that can never be met gets ignored, which is worse than a weaker bar that is
enforced.

**Consequences.** Raw appearance is _not_ verified by CI. Visual work is caught by
the manual smoke step and by the ledger, not by the frame corpus.

---

## ADR-004 — Fallback policy: Textual stays default until the ledger is clean

**Decision.** Increment 1 ships `--ui {textual,ratatui}` with `textual` as the
default. `--ui ratatui` (or `NOVA_UI=ratatui`) resolves the binary from
`NOVA_TUI_BIN` → next to the package → `~/.nova/bin/nova-tui` → `PATH`. If it is
missing, print one actionable line and fall back to Textual.

**Rejected.** _Ship ratatui as default now._ Most installs have no Rust toolchain
and no published binary yet; a hard failure would be the first thing a new user
sees.

**Consequences.** `--ui ratatui` becomes default only in wave 7, after every
`LEDGER.md` row is `ported`/`server-side`/`n/a`.

---

## ADR-005 — Increment-1 session scope

**Decision.** One `SessionHost` serves one in-process session per connection; the
host keeps a registry so `sessions`/`attach`/`create` are implemented, but
creating a _second_ session needs a factory the launcher does not provide yet, so
it answers with an explicit `notice` + `command_result(ok=false)` when at
capacity. The cap is `sessions.supervisor.MAX_SESSIONS` (4), reused rather than
redefined. Fan-out to child processes (`SessionSupervisor.spawn`) is wave 3,
together with session tabs.

Standalone `nova --serve` builds its session from a default factory: it does not
run `main()`'s sandbox resolution. That limit is documented in
`novacode_cli/server/host_main.py` and is not reachable from
`nova --ui ratatui`, which passes the fully configured agent.

**Rejected.** _Spawn a child Nova process per session now._ It is the right end
state (a child is a full Nova, bound to its own worktree), but it needs worktree
lifecycle, resume and tab UX — the wave-3 package.

---

## ADR-006 — Command seam via `UIPort`, not re-implemented handlers

**Decision.** `novacode_cli/ui/port.py` defines `UIPort`; `port_server.py`
implements `ServerPort`, which captures Rich console output with
`console.capture()` — the same mechanism `sessions/worker.py` and the Textual
`_passthrough_command` use — and turns it into `command_result` blocks. Ported in
increment 1: `/help`, `/status`, `/model`, `/clear`, `/quit`. Every other command
answers `ok=false`, `"<name> is not ported to the ratatui UI yet"`.

**Rejected.** _Re-implement commands against the protocol._ 54 handler modules
and 55 commands call `console.print` and app methods directly; re-implementing
them would fork behaviour and drift.

**Consequences.** A command whose handler pushes a screen cannot be ported by the
port alone (it needs `push_screen` on the client). Those rows stay
`not-started` until waves 2–4 add the screens.

---

## ADR-007 — Protocol evolution: additive frames, version constant

**Decision.** `frames.PROTOCOL_VERSION = 1`. New behaviour arrives as _new_ `t`
values or optional fields. `hello` carries the version; a mismatched major is
reported in `notice` and the connection continues, because both decoders skip
unknown frames (`decode_event` returns `None` for an unknown `c`).

**Rejected.** _A single version-gated schema._ Nova is installed from PyPI and a
user may run an older wheel against a newer binary; skipping unknown frames is
what already keeps `worker.py` and the supervisor compatible.

---

## ADR-008 — What increment 1 deliberately left out, and why

Three plan details changed while implementing, each for a concrete reason:

1. **No `similar` dependency, no file-op diff rendering.** A `FileOp` event carries a
   `FileOperationRecord` (path, operation, metrics) and the tool's raw
   `full_output` — there is no before/after text on the wire, so a _diff_ cannot be
   computed client-side without inventing one. `FileOp` cards therefore render
   operation + path, and the diff view moves to the file-op wave that can also
   carry both sides. Adding an unused dependency just to claim parity would be
   worse than the honest gap, and `LEDGER.md` records it.
2. **The transcript follows the tail.** `scroll` counts lines back from the end
   instead of forward from the start, so the newest output is visible while
   streaming and "the final line of a tool result is visible" needs no scrolling.
   This is the behaviour a chat needs; it is asserted directly in
   `frames_replay.rs` rather than left to a snapshot.
3. **The standalone `--serve` path reuses `main()`'s bootstrap.** Instead of a
   second agent bootstrap inside the server (model, sandbox, session manager all
   rebuilt), `main()` builds the session once and hands a `SessionSpec` to either
   the launcher or `serve_main`. The cost is that `--serve` pays the normal agent
   warm-up before it listens; the benefit is one bootstrap to maintain.
