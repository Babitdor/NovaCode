# NovaCode 1.2.83

## TUI: responsive with background tasks running

- The Textual UI boots ~1.7s faster: the `tui` package no longer imports the
  whole app on any submodule import, and the Prime panel, Oracle panel and the
  agent stream chain (LangChain + LangGraph + keychain) load on first use
  instead of at startup.
- The subagents dock no longer reads the Windows keychain per row: a fresh
  NovaConfig was built for every row of every repaint (~10ms each on the UI
  thread); the model resolution is now memoized and invalidated when the model
  screen saves.
- The Background Tasks panel is event-driven: it reacts to registry events
  (throttled to 4 refreshes/s), its runtime clock pauses while the panel is
  empty, and a job finishing updates its row in place instead of rebuilding
  the whole list and resetting the selection.
- Two async subagent previews can be open at once: the second preview no
  longer cancels the first one's poller (it used to freeze at its last
  snapshot).

## Fixes

- A degenerate panel width (<= 0) no longer hangs the output wrap loop
  forever; flushes now wrap a batch of lines in one call instead of one call
  per line.
- The subagent row cache trims its oldest entries instead of clearing
  wholesale, so a dock near the cap stops re-rendering every row.

## Tooling

- `scripts/bench_ui_responsiveness.py`, `scripts/profile_ui_storms.py` and
  `scripts/bench_worst_case_cliffs.py`: reproducible frame-timing, stall and
  worst-case measurements against the real app under background-task load.
